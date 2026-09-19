"""api.results — árvore, série agregada e exceções da tela única (S2.3).

Regra de negócio (agregação, seleção de modelo, WAPE) fica em
`data_engine.results`; aqui só valida a rodada, aplica cache em memória por
`(run_id, params)` e traduz erros em HTTP. O cache é invalidado por
`invalidate_run_cache` quando algo muda a rodada (ex.: override, S3.1).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import data_engine

from .state import db_lock, get_connection

router = APIRouter()

_CACHE: dict[tuple, dict] = {}


def invalidate_run_cache(run_id: str) -> None:
    for key in [k for k in _CACHE if k[0] == run_id]:
        del _CACHE[key]


@router.get("/runs/{run_id}/tree")
def get_tree(run_id: str) -> dict:
    key = ("tree", run_id)
    if key in _CACHE:
        return _CACHE[key]
    conn = get_connection()
    with db_lock():
        try:
            study = data_engine.load_run_study(conn, run_id)
            tree = data_engine.build_run_tree(conn, run_id, study)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    _CACHE[key] = tree
    return tree


@router.get("/runs/{run_id}/series")
def get_series(
    run_id: str,
    dim: str,
    value: str,
    measure: str = "unidades",
    scenario: str = "base",
    model: str | None = None,
) -> dict:
    key = ("series", run_id, dim, value, measure, scenario, model)
    if key in _CACHE:
        return _CACHE[key]
    conn = get_connection()
    with db_lock():
        try:
            result = data_engine.series_view(
                conn, run_id, dim, value, measure, scenario, model_alias=model
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    _CACHE[key] = result
    return result


@router.get("/runs/{run_id}/series/by-model")
def get_series_by_model(
    run_id: str,
    dim: str,
    value: str,
    measure: str = "unidades",
    scenario: str = "base",
) -> dict:
    key = ("series-by-model", run_id, dim, value, measure, scenario)
    if key in _CACHE:
        return _CACHE[key]
    conn = get_connection()
    with db_lock():
        try:
            result = data_engine.series_by_model(conn, run_id, dim, value, measure, scenario)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    _CACHE[key] = result
    return result


@router.get("/runs/{run_id}/exceptions")
def get_exceptions(
    run_id: str, top: int = Query(20, ge=1, le=200), measure: str = "unidades"
) -> dict:
    key = ("exceptions", run_id, top, measure)
    if key in _CACHE:
        return _CACHE[key]
    conn = get_connection()
    with db_lock():
        try:
            result = data_engine.exceptions_ranking(conn, run_id, top, measure)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    _CACHE[key] = result
    return result
