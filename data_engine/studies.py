"""data_engine.studies — estudos salvos: nome, id e data/hora de uma rodada.

Uma rodada concluída já fica persistida em `runs`/`forecasts`/`model_scores`;
"salvar o estudo" é dar-lhe um nome (`runs.study_name`, `runs.saved_at`) para
reabri-la depois. O id do estudo é o próprio `run_id`.
"""

from __future__ import annotations

import json

from contracts import ForecastConfig

from .db import _now_ts, _tx

_FINISHED = ("completed", "partial")
NAME_MAX = 120


def _config_horizon(config_json: str | None) -> int | None:
    try:
        return ForecastConfig.from_json(config_json).horizon_periods
    except Exception:  # noqa: BLE001
        return None


def _iso(ts) -> str | None:
    return ts.isoformat(timespec="seconds") if ts is not None else None


def _studies_query(conn, run_id: str | None) -> list[dict]:
    where = "r.status IN ('completed', 'partial')"
    params: list = []
    if run_id is not None:
        where += " AND r.run_id = ?"
        params.append(run_id)
    rows = conn.execute(
        "SELECT r.run_id, r.study_name, r.saved_at, r.started_at, r.ended_at,"
        " r.status, r.dataset_id, r.preparation_id, r.config_json,"
        " d.filename, d.name FROM runs r LEFT JOIN datasets d"
        f" ON d.dataset_id = r.dataset_id WHERE {where}"
        " ORDER BY r.started_at DESC",
        params,
    ).fetchall()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    marks = ",".join("?" * len(ids))
    n_series = dict(
        conn.execute(
            "SELECT run_id, count(*) FROM run_nodes WHERE level = 'folha'"
            f" AND run_id IN ({marks}) GROUP BY run_id",
            ids,
        ).fetchall()
    )
    models: dict[str, list[str]] = {}
    for rid, alias in conn.execute(
        "SELECT DISTINCT run_id, model_alias FROM forecasts"
        f" WHERE run_id IN ({marks}) ORDER BY 1, 2",
        ids,
    ).fetchall():
        models.setdefault(rid, []).append(alias)
    out = []
    for (rid, name, saved_at, started, ended, status, ds_id, prep_id, cfg,
         filename, ds_name) in rows:
        out.append(
            {
                "run_id": rid,
                "short_id": rid[:8],
                "name": name,
                "saved": name is not None,
                "saved_at": _iso(saved_at),
                "started_at": _iso(started),
                "ended_at": _iso(ended),
                "status": status,
                "dataset_id": ds_id,
                "preparation_id": prep_id,
                "filename": filename or ds_name or "",
                "n_series": int(n_series.get(rid, 0)),
                "horizon": _config_horizon(cfg),
                "models": models.get(rid, []),
            }
        )
    return out


def list_studies(conn) -> list[dict]:
    """Rodadas concluídas (mais recentes primeiro); `saved` diz se têm nome."""
    return _studies_query(conn, None)


def get_study(conn, run_id: str) -> dict:
    rows = _studies_query(conn, run_id)
    if not rows:
        raise KeyError(f"Estudo {run_id} não encontrado ou ainda não concluído")
    return rows[0]


def save_study_name(conn, run_id: str, name: str) -> dict:
    """Dá nome ao estudo (rodada concluída) e registra data/hora do salvamento."""
    clean = " ".join((name or "").split())
    if not clean:
        raise ValueError("Informe um nome para o estudo.")
    if len(clean) > NAME_MAX:
        raise ValueError(f"O nome pode ter no máximo {NAME_MAX} caracteres.")
    get_study(conn, run_id)  # KeyError se inexistente/não concluído
    with _tx(conn):
        conn.execute(
            "UPDATE runs SET study_name = ?, saved_at = ? WHERE run_id = ?",
            [clean, _now_ts(), run_id],
        )
    return get_study(conn, run_id)


def delete_study(conn, run_id: str) -> dict:
    """Exclui DEFINITIVAMENTE uma rodada concluída e tudo que é dela.

    Apaga as linhas de `run_id` em todas as tabelas que têm essa coluna
    (previsões, backtest, métricas, nós, problemas, `runs`). Não mexe no
    arquivo importado nem na preparação (`datasets`/`preparations`), que outras
    rodadas podem usar. Devolve quantas linhas saíram de cada tabela.
    """
    get_study(conn, run_id)  # KeyError se inexistente/não concluída
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE column_name = 'run_id' AND table_schema = 'main'"
        ).fetchall()
    ]
    removed: dict[str, int] = {}
    with _tx(conn):
        for t in tables:
            if t == "runs":
                continue
            n = conn.execute(f"SELECT count(*) FROM {t} WHERE run_id = ?", [run_id]).fetchone()[0]
            if n:
                conn.execute(f"DELETE FROM {t} WHERE run_id = ?", [run_id])
                removed[t] = int(n)
        conn.execute("DELETE FROM runs WHERE run_id = ?", [run_id])
        removed["runs"] = 1
    return removed
