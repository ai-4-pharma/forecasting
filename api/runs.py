"""api.runs — criar rodada, consultar progresso e cancelar (S2.1).

Cada rodada roda em `threading.Thread` própria consumindo o gerador
`forecast_engine.run_forecast` (sec 4/8.2 de `_fable.md`); os eventos com
`partial_batch` são persistidos a cada passo (S1.4) para que `GET .../progress`
reflita dados já visíveis em `forecasts` antes da rodada terminar.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import data_engine
import forecast_engine
from contracts import (
    ForecastConfig,
    ForecastMode,
    RunInputs,
    RunStatus,
    RunSummary,
    TreatmentPolicy,
)

from .state import RUNS, RUNS_LOCK, db_lock, get_connection

router = APIRouter()


class CreateRunRequest(BaseModel):
    dataset_id: str
    preparation_id: str
    horizon: int = 12
    mode: str = "fast"
    candidate_aliases: list[str] = []


def _set_progress(run_id: str, **fields: Any) -> None:
    with RUNS_LOCK:
        state = RUNS.get(run_id)
        if state is not None:
            state["progress"].update(fields)


def _load_policy(conn, preparation_id: str) -> TreatmentPolicy:
    row = conn.execute(
        "SELECT policy_json FROM preparations WHERE preparation_id = ?",
        [preparation_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"Preparação {preparation_id} não encontrada")
    return TreatmentPolicy.from_json(row[0])


def _start_run(req: CreateRunRequest) -> str:
    conn = get_connection()
    with db_lock():
        study, mapping, data = data_engine.load_dataset(conn, req.dataset_id)
        policy = _load_policy(conn, req.preparation_id)
        scenarios, regressors = data_engine.load_assumptions(conn, req.dataset_id)
    # prepare_data e pura (sem I/O): reconstrói prepared/raw_series fora do lock.
    prep = data_engine.prepare_data(data, study, policy)
    # mode=ADVANCED e enable_ml=True sempre (S2.9): desde que o seletor de
    # métodos passou a oferecer o catálogo avançado (ver 20260918_ajustes.md),
    # `candidate_aliases` é quem decide sozinho quais métodos concorrem —
    # `build_candidates` (forecast_engine/models.py) só adiciona
    # AutoCES/AutoARIMA/AutoTBATS quando mode=ADVANCED, e o runner só treina
    # LightGBM/XGBoost quando enable_ml=True; manter esses 2 campos presos ao
    # valor antigo do wizard fazia o backend ignorar silenciosamente os
    # métodos avançados escolhidos na tela (só `AutoETS` chegava a rodar).
    cfg = ForecastConfig(
        horizon_periods=req.horizon,
        mode=ForecastMode.ADVANCED,
        candidate_aliases=list(req.candidate_aliases),
        enable_ml=True,
    )
    if scenarios:
        cfg.scenario_ids = [s.scenario_id for s in scenarios]
    cfg.regressor_ids = [r.regressor_id for r in regressors if r.enabled]
    cfg_errors = cfg.validate()
    if cfg_errors:
        raise ValueError("; ".join(cfg_errors))

    with db_lock():
        run_id = data_engine.create_run(
            conn, req.dataset_id, req.preparation_id, cfg, scenarios, regressors
        )

    with RUNS_LOCK:
        RUNS[run_id] = {
            "status": "running",
            "cancel": False,
            "progress": {
                "stage": "started",
                "completed": 0,
                "total": 0,
                "message": "Iniciando…",
            },
        }

    inputs = RunInputs(
        run_id=run_id,
        dataset_id=req.dataset_id,
        preparation_id=req.preparation_id,
        study=study,
        mapping=mapping,
        policy=policy,
        config=cfg,
        scenarios=scenarios,
        regressors=regressors,
        prepared=prep.prepared,
        raw_series=prep.raw_series,
        entities=data.entities,
        should_cancel=lambda: RUNS.get(run_id, {}).get("cancel", False),
    )
    thread = threading.Thread(target=_drive, args=(run_id, inputs), daemon=True)
    with RUNS_LOCK:
        RUNS[run_id]["thread"] = thread
    thread.start()
    return run_id


def _drive(run_id: str, inputs: RunInputs) -> None:
    conn = get_connection()
    last = None
    try:
        for ev in forecast_engine.run_forecast(inputs):
            last = ev
            _set_progress(
                run_id,
                stage=ev.stage.value,
                completed=ev.completed,
                total=ev.total,
                message=ev.message,
            )
            payload = ev.payload or {}
            partial = payload.get("partial_batch")
            if partial is not None:
                with db_lock():
                    data_engine.persist_batch(
                        conn, run_id, ev.batch_id or ev.stage.value, partial
                    )
        _finalize(conn, run_id, last)
    except Exception as exc:  # progresso da thread não deve derrubar a API
        with db_lock():
            data_engine.finish_run(
                conn, RunSummary(run_id, RunStatus.FAILED, {}, {}, {}, [], "")
            )
        with RUNS_LOCK:
            RUNS[run_id]["status"] = RunStatus.FAILED.value
            RUNS[run_id]["error"] = str(exc)


def _finalize(conn, run_id: str, last) -> None:
    payload = last.payload if last is not None and last.payload is not None else {}
    batch = payload.get("batch")
    n_failed = payload.get("n_failed", 0) or 0
    with db_lock():
        if batch is not None and batch.predictions.height:
            data_engine.persist_batch(conn, run_id, "final", batch)
        nodes = payload.get("nodes")
        if nodes is not None and nodes.height:
            data_engine.persist_nodes(conn, run_id, nodes)
    n_preds = payload.get("n_predictions")
    if n_preds is None:
        n_preds = batch.predictions.height if batch is not None else 0
    cancelled = last is not None and last.stage.value == "cancelled"
    status = (
        RunStatus.CANCELLED
        if cancelled
        else (RunStatus.PARTIAL if n_failed else RunStatus.COMPLETED)
    )
    with db_lock():
        data_engine.finish_run(
            conn,
            RunSummary(
                run_id,
                status,
                {"predictions": n_preds, "failed": n_failed},
                {},
                {},
                [],
                "",
            ),
        )
    with RUNS_LOCK:
        RUNS[run_id]["status"] = status.value
        RUNS[run_id]["progress"] = {
            "stage": last.stage.value if last is not None else "completed",
            "completed": n_preds,
            "total": n_preds,
            "message": last.message if last is not None else "Concluído.",
        }


@router.post("/runs")
def create_run(req: CreateRunRequest) -> dict:
    try:
        run_id = _start_run(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"run_id": run_id}


@router.get("/runs/{run_id}/progress")
def get_progress(run_id: str) -> dict:
    with RUNS_LOCK:
        state = RUNS.get(run_id)
        if state is None:
            raise HTTPException(
                status_code=404, detail=f"Rodada {run_id} não encontrada"
            )
        return {"status": state.get("status", "running"), **state["progress"]}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict:
    with RUNS_LOCK:
        state = RUNS.get(run_id)
        if state is None:
            raise HTTPException(
                status_code=404, detail=f"Rodada {run_id} não encontrada"
            )
        state["cancel"] = True
    return {"run_id": run_id, "cancel_requested": True}
