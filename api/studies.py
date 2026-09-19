"""api.studies — salvar (nomear), listar e reabrir estudos.

A regra fica em `data_engine.studies`; aqui só há validação HTTP.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import data_engine

from .results import invalidate_run_cache
from .state import RUNS, RUNS_LOCK, db_lock, get_connection

router = APIRouter()


class SaveStudyRequest(BaseModel):
    name: str


@router.get("/studies")
def list_studies() -> dict:
    conn = get_connection()
    with db_lock():
        return {"studies": data_engine.list_studies(conn)}


@router.get("/studies/{run_id}")
def get_study(run_id: str) -> dict:
    conn = get_connection()
    with db_lock():
        try:
            return data_engine.get_study(conn, run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/studies/{run_id}/name")
def save_study(run_id: str, req: SaveStudyRequest) -> dict:
    conn = get_connection()
    with db_lock():
        try:
            return data_engine.save_study_name(conn, run_id, req.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/studies/{run_id}")
def delete_study(run_id: str) -> dict:
    """Exclusão definitiva; só rodadas concluídas (nunca uma em andamento)."""
    conn = get_connection()
    with db_lock():
        try:
            removed = data_engine.delete_study(conn, run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    with RUNS_LOCK:
        RUNS.pop(run_id, None)
    invalidate_run_cache(run_id)
    return {"run_id": run_id, "deleted": removed}
