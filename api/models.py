"""api.models — lista de métodos disponíveis para o seletor da tela (S2.6/S2.9).

Só leitura; as listas em si (`CORE_ALIASES`/`ADVANCED_ALIASES`) vivem em
`contracts.py`, para não duplicar a regra de negócio no front. `ADVANCED_ALIASES`
passou a ser exposta a partir de S2.9 (pedido explícito do usuário: "cadê os
demais métodos?"), revertendo a restrição original da regra 5 do plano — ver
ocorrência na seção 9 de `20260918_ajustes.md`. `LightGBM`/`XGBoost` continuam
condicionados a `_ml_available()`: sem o complemento de ML instalado, o produto
não os oferece (mesma regra que já valia no laboratório Streamlit).

`AutoARIMA_X` fica de fora da lista (ver ocorrência S2.9-rodada-4): só entra
como candidato no motor quando há regressoras configuradas
(`forecast_engine/models.py::build_candidates`, `config.regressor_ids`), e esta
tela ainda não tem UI de regressoras (isso é S3.x). Oferecê-lo no seletor
fazia o usuário marcar um método que nunca chegava a rodar, sem nenhum aviso.

`slow`/`slow_hint`: usados pelo seletor para mostrar um alerta ao passar o
mouse sobre métodos sabidamente lentos em bases grandes (AutoARIMA/AutoTBATS
fazem busca de parâmetros por série; LightGBM/XGBoost treinam um modelo global
e preveem passo a passo o horizonte inteiro) — achado ao investigar uma
rodada real de ~11-12 min com 523 séries (ver seção 9).
"""

from __future__ import annotations

from fastapi import APIRouter

import forecast_engine
from contracts import ADVANCED_ALIASES, CORE_ALIASES

router = APIRouter()

_ML_ONLY_ALIASES = {"LightGBM", "XGBoost"}
_NO_UI_ALIASES = {"AutoARIMA_X"}  # precisa de regressoras; sem UI nesta tela
_SLOW_ALIASES = {"AutoARIMA", "AutoTBATS", "LightGBM", "XGBoost"}
_SLOW_HINT = (
    "Método mais lento em bases grandes: ajusta cada série individualmente "
    "(ou treina um modelo global e prevê passo a passo o horizonte inteiro), "
    "em vez do cálculo direto dos métodos simples. Em medições internas, "
    "AutoARIMA levou ~0,5-1s por série — em centenas de séries isso soma "
    "minutos. Prefira poucos métodos lentos por rodada se a velocidade importar."
)


@router.get("/models")
def get_models() -> dict:
    advanced = [a for a in ADVANCED_ALIASES if a not in _NO_UI_ALIASES]
    if not forecast_engine._ml_available():
        advanced = [a for a in advanced if a not in _ML_ONLY_ALIASES]
    slow = [a for a in advanced if a in _SLOW_ALIASES]
    return {
        "core": list(CORE_ALIASES),
        "advanced": advanced,
        "slow": slow,
        "slow_hint": _SLOW_HINT,
    }
