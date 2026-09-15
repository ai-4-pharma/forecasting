"""Constantes do motor: ordem de candidatos, labels PT e minimos de ML.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Registro de candidatos (P15)
# ---------------------------------------------------------------------------

MODEL_ORDER = [
    "ZeroBaseline",
    "Naive",
    "MediaMovel3",
    "MediaMovel6",
    "MediaMovel12",
    "HistoricAverage",
    "SeasonalNaive",
    "RegLinearDrift",
    "Holt",
    "HoltDamped",
    "AutoETS",
    "ETS_Damped",
    "AutoARIMA",
    "AutoARIMA_X",
    "AutoTheta",
    "AutoCES",
    "AutoTBATS",
    "CrostonSBA",
    "TSB",
    "LightGBM",
    "XGBoost",
]
RANK_BY_ALIAS = {name: i for i, name in enumerate(MODEL_ORDER)}

MODEL_LABELS_PT = {
    "ZeroBaseline": "Zero (série zerada)",
    "Naive": "Último valor (Naive)",
    "MediaMovel3": "Média móvel 3M",
    "MediaMovel6": "Média móvel 6M",
    "MediaMovel12": "Média móvel 12M",
    "HistoricAverage": "Média histórica",
    "SeasonalNaive": "Sazonal Naive (mesmo mês ano anterior)",
    "RegLinearDrift": "Regressão / tendência linear (Drift)",
    "Holt": "Suavização Holt (tendência)",
    "HoltDamped": "Suavização Holt amortecida",
    "AutoETS": "Suavização Exponencial (ETS automático)",
    "ETS_Damped": "Suavização Exponencial amortecida (ETS-D)",
    "AutoARIMA": "ARIMA/SARIMA automático",
    "AutoARIMA_X": "ARIMA com regressoras (ARIMAX)",
    "AutoTheta": "Theta (decomposição)",
    "AutoCES": "Suavização Complexa (CES)",
    "AutoTBATS": "TBATS (sazonalidade complexa)",
    "CrostonSBA": "Demanda intermitente (Croston-SBA)",
    "TSB": "Demanda intermitente (TSB)",
    "LightGBM": "ML Global (LightGBM)",
    "XGBoost": "ML Global (XGBoost)",
}
ML_MIN_ENTITIES = 20
ML_MIN_ROWS = 200

