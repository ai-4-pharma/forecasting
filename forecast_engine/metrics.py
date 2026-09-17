"""Metricas, piso nao-negativo, scores, selecao e frames vazios.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

import datetime as dt
import math
import numpy as np


# ---------------------------------------------------------------------------
# Métricas (P17)
# ---------------------------------------------------------------------------


def _mse_mae(y_actual: np.ndarray, yhat: np.ndarray):
    e = yhat - y_actual
    n = len(e)
    if n == 0:
        return None, None, None, None
    mae = np.mean(np.abs(e))
    rmse = math.sqrt(np.mean(e**2))
    wape = (
        np.sum(np.abs(e)) / np.sum(np.abs(y_actual))
        if np.sum(np.abs(y_actual)) > 0
        else None
    )
    bias = float(np.mean(e))
    return (
        float(mae),
        float(rmse),
        (float(wape) if wape is not None else None),
        float(bias),
    )


def _apply_floor(yhat: float) -> float:
    """Piso zero obrigatório (secs 9.1 e 15)."""
    if np.isnan(yhat) or np.isinf(yhat):
        return yhat
    return max(0.0, yhat)


def _score_from_evals(eval_rows: list[dict], n_windows: int) -> dict:
    """Calcula métricas a partir de eval rows (usado para o modelo global)."""
    evaluated = [e for e in eval_rows if e.get("evaluated")]
    if not evaluated:
        return {
            "mae": None,
            "rmse": None,
            "wape": None,
            "bias": None,
            "n_eval": 0,
            "n_folds": n_windows,
            "eligible": False,
        }
    ys = np.array([e["y_actual"] for e in evaluated]).astype(float)
    yh = np.array([_apply_floor(e["yhat"]) for e in evaluated]).astype(float)
    mae, rmse, wape, bias = _mse_mae(ys, yh)
    return {
        "mae": mae,
        "rmse": rmse,
        "wape": wape,
        "bias": bias,
        "n_eval": int(len(ys)),
        "n_folds": n_windows,
        "eligible": True,
    }


# ---------------------------------------------------------------------------
# Lotes e rodada (P19)
# ---------------------------------------------------------------------------


def _empty_pred() -> list[dict]:
    return [
        {
            "node_id": "",
            "entity_id": "",
            "level": "",
            "measure": "",
            "scenario_id": "base",
            "ds": dt.date(2000, 1, 1),
            "yhat": 0.0,
            "lo80": None,
            "hi80": None,
            "model_alias": "",
            "interval_method": "",
            "status": "",
        }
    ]


def _empty_cv() -> list[dict]:
    return [
        {
            "node_id": "",
            "measure": "",
            "model_alias": "",
            "cutoff": dt.date(2000, 1, 1),
            "ds": dt.date(2000, 1, 1),
            "y_actual": None,
            "yhat": None,
            "evaluated": False,
            "failure_reason": "",
        }
    ]


def _empty_scores() -> list[dict]:
    return [
        {
            "node_id": "",
            "measure": "",
            "model_alias": "",
            "mae": None,
            "rmse": None,
            "wape": None,
            "bias": None,
            "n_eval": 0,
            "n_folds": 0,
            "eligible": False,
            "failure_reason": "",
        }
    ]


def _empty_select() -> list[dict]:
    return [
        {
            "node_id": "",
            "measure": "",
            "model_alias": "",
            "selection_reason": "",
            "cv_horizon": 0,
            "n_folds": 0,
            "fallback_used": False,
            "clipped_count": 0,
        }
    ]
