"""api.datasets — upload com autodetecção do formato largo farma (S2.2).

Regra de negócio (parsing, perfil, preparação) fica em `data_engine`; aqui só
decide 422 (pede mapeamento manual) vs. 200 (autodetectado) e monta a resposta
para a tela única.

Nota (sec 9 do plano): `quality_badges` só traz `zeros_pct`/`gaps` por série —
`ProfileReport.by_series` (data_engine/profile.py) não tem coluna de outliers
hoje, então esse badge foi omitido em vez de inventado; adicionar contagem de
outliers ao profile é fora do escopo desta tarefa (arquivos fechados a
`api/datasets.py` e `data_engine/ingest.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import data_engine
from contracts import MappingConfig, StudyConfig, TreatmentPolicy

from .state import db_lock, get_connection

router = APIRouter()


def _quality_badges(profile) -> dict[str, dict]:
    if profile.by_series.height == 0:
        return {}
    badges: dict[str, dict] = {}
    for row in profile.by_series.select(
        "series_id", "n_periodos", "n_zeros"
    ).to_dicts():
        n = row["n_periodos"] or 0
        zeros_pct = round(row["n_zeros"] / n, 4) if n else 0.0
        badges[row["series_id"]] = {"zeros_pct": zeros_pct, "gaps": 0}
    return badges


def _error_messages(issues) -> list[str]:
    return [i.message for i in issues if i.severity.value == "error"]


def _finish_dataset(conn, study: StudyConfig, mapping: MappingConfig, cd, filename: str) -> dict:
    dataset_id = data_engine.save_dataset(conn, study, mapping, cd, filename=filename)
    profile = data_engine.profile_data(cd, study)
    prep = data_engine.prepare_data(cd, study, TreatmentPolicy())
    preparation_id = data_engine.save_preparation(conn, dataset_id, prep)
    return {
        "dataset_id": dataset_id,
        "preparation_id": preparation_id,
        "n_series": len(prep.eligible_series) + len(prep.excluded_series),
        "n_periods": study.history_periods,
        "dims": list(study.dimension_names),
        "quality_badges": _quality_badges(profile),
    }


@router.post("/datasets")
async def create_dataset(file: UploadFile = File(...)) -> dict:
    content = await file.read()
    insp = data_engine.inspect_file(content, file.filename or "arquivo", MappingConfig())
    detected = data_engine.autodetect_mapping(insp, name=file.filename or "Dataset")
    if detected is None:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Formato não reconhecido automaticamente; mapeie manualmente.",
                "columns": insp.columns,
            },
        )
    study, mapping = detected
    cd = data_engine.normalize_file(content, file.filename or "arquivo", study, mapping)
    errors = _error_messages(cd.issues)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Arquivo detectado, mas com erros de normalização.",
                "issues": errors,
                "columns": insp.columns,
            },
        )
    conn = get_connection()
    with db_lock():
        return _finish_dataset(conn, study, mapping, cd, file.filename or "")


@router.post("/datasets/manual")
async def create_dataset_manual(
    file: UploadFile = File(...),
    study_json: str = Form(...),
    mapping_json: str = Form(...),
) -> dict:
    content = await file.read()
    study = StudyConfig.from_json(study_json)
    mapping = MappingConfig.from_json(mapping_json)
    cd = data_engine.normalize_file(content, file.filename or "arquivo", study, mapping)
    errors = _error_messages(cd.issues)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Erros de normalização com o mapeamento informado.",
                "issues": errors,
            },
        )
    conn = get_connection()
    with db_lock():
        return _finish_dataset(conn, study, mapping, cd, file.filename or "")
