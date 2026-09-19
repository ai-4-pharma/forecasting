"""api.exports — download do XLSX da rodada (S3.2): histórico + uma aba por modelo.

A montagem do arquivo fica em `data_engine.exports`; aqui só valida a rodada
e devolve os bytes como anexo.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import data_engine

from .state import db_lock, get_connection

router = APIRouter()


@router.get("/runs/{run_id}/export.xlsx")
def export_xlsx(run_id: str, scenario: str = "base") -> Response:
    conn = get_connection()
    with db_lock():
        try:
            artifact = data_engine.export_by_model_xlsx(conn, run_id, scenario)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=artifact.bytes_or_path,
        media_type=artifact.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
    )
