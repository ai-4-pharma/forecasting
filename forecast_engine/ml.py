"""Modelos globais (LightGBM/XGBoost) via MLForecast.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

import polars as pl

from contracts import (
    SourceFrequency,
    IntervalMethod,
)

from .const import ML_MIN_ENTITIES, ML_MIN_ROWS

from .dates import _pd_freq_str, _season_length
from .metrics import _apply_floor


def _pkg_attr(name: str):
    """Lookup tardio no namespace do pacote (honra monkeypatch)."""
    import forecast_engine as _pkg

    return getattr(_pkg, name)


# ---------------------------------------------------------------------------
# Aprendizado global (P28/P29)
# ---------------------------------------------------------------------------


def _ml_available() -> bool:
    try:
        import lightgbm  # noqa: F401
        import mlforecast  # noqa: F401

        return True
    except ImportError:
        return False


def _xgb_available() -> bool:
    try:
        import xgboost  # noqa: F401
        import mlforecast  # noqa: F401

        return True
    except ImportError:
        return False


def _ml_features(freq: SourceFrequency) -> tuple[list[int], list[str]]:
    """Features causais do modelo global (sec 9.5), por frequência.

    Lags [1,2,3] + lag sazonal quando elegível; o MLForecast também aplica a
    média móvel causal de três observações via `RollingMean`; calendário por
    frequência. As colunas são geradas no treino; a lista aqui documenta o
    conjunto configurado em `train_global_model`.
    """
    season_len = _season_length(freq)
    if freq in (SourceFrequency.MONTHLY, SourceFrequency.MAT):
        lags = [1, 2, 3, season_len]
        date_features = ["month", "year"]
    elif freq == SourceFrequency.QUARTERLY:
        lags = [1, 2, 3, season_len]
        date_features = ["quarter", "year"]
    else:
        lags = [1, 2, 3]
        date_features = ["year"]
    return sorted(set(lags)), date_features


def train_global_model(
    df_all: pl.DataFrame,
    cutoff,
    freq: SourceFrequency,
    measure: str,
    seed: int = 42,
    n_jobs: int = 1,
    model_name: str = "LightGBM",
):
    """Treina MLForecast global (LightGBM/XGBoost), um modelo por medida."""
    if model_name == "XGBoost":
        if not _xgb_available():
            return None, "ml_not_installed"
    elif not _pkg_attr("_ml_available")():
        return None, "ml_not_installed"
    from ._quiet import install_warning_filters

    install_warning_filters()
    from mlforecast import MLForecast
    from mlforecast.lag_transforms import RollingMean

    train = (
        df_all.filter(pl.col("measure") == measure, pl.col("ds") <= cutoff)
        .select(["unique_id", "ds", "y"])
        .sort(["unique_id", "ds"])
    )
    n_entities = train["unique_id"].n_unique()
    if n_entities < ML_MIN_ENTITIES or train.height < ML_MIN_ROWS:
        return None, "below_minimum"
    lags, date_features = _ml_features(freq)
    if model_name == "XGBoost":
        from xgboost import XGBRegressor

        reg = XGBRegressor(
            n_estimators=200,
            learning_rate=0.05,
            max_depth=6,
            random_state=seed,
            n_jobs=n_jobs,
            verbosity=0,
        )
    else:
        from lightgbm import LGBMRegressor

        reg = LGBMRegressor(
            n_estimators=200,
            learning_rate=0.05,
            num_leaves=31,
            random_state=seed,
            n_jobs=n_jobs,
            verbose=-1,
        )
    mlf = MLForecast(
        models={model_name: reg},
        freq=_pd_freq_str(freq),
        lags=lags,
        lag_transforms={1: [RollingMean(window_size=3)]},
        date_features=date_features,
    )
    train_pd = train.to_pandas()
    mlf.fit(train_pd[["unique_id", "ds", "y"]])
    return mlf, "ok"


def ml_forecast_fold(
    df_all: pl.DataFrame,
    series_ids: list[str],
    cutoff,
    freq,
    measure: str,
    horizon: int,
    seed: int = 42,
    n_jobs: int = 1,
    model_name: str = "LightGBM",
):
    """Previsão global recursiva para as séries dadas (sem ler y do teste)."""
    mlf, status = train_global_model(
        df_all, cutoff, freq, measure, seed=seed, n_jobs=n_jobs, model_name=model_name
    )
    if status != "ok" or mlf is None:
        return None, status
    future = mlf.predict(h=horizon)
    out = pl.from_pandas(future)
    if model_name in out.columns and "yhat" not in out.columns:
        out = out.rename({model_name: "yhat"})
    out = out.with_columns(pl.col("ds").dt.date().alias("ds"))
    out = out.filter(pl.col("unique_id").is_in(series_ids))
    return out, "ok"


def ml_final(
    df_all: pl.DataFrame,
    node_id: str,
    measure: str,
    freq: SourceFrequency,
    horizon: int,
    seed: int = 42,
    n_jobs: int = 1,
    model_name: str = "LightGBM",
):
    """Reajusta o modelo global da medida com todo o histórico e projeta h passos."""
    mlf, status = train_global_model(
        df_all,
        df_all["ds"].max(),
        freq,
        measure,
        seed=seed,
        n_jobs=n_jobs,
        model_name=model_name,
    )
    if status != "ok" or mlf is None:
        return [], status
    future = mlf.predict(h=horizon)
    out = pl.from_pandas(future)
    if model_name in out.columns:
        out = out.rename({model_name: "yhat"})
    out = out.with_columns(pl.col("ds").dt.date().alias("ds"))
    out = out.filter(pl.col("unique_id") == node_id)
    rows = []
    for r in out.to_dicts():
        y = float(r["yhat"]) if r.get("yhat") is not None else 0.0
        rows.append(
            {
                "ds": r["ds"],
                "yhat": _apply_floor(y),
                "lo80": None,
                "hi80": None,
                "interval_method": IntervalMethod.UNAVAILABLE_INSUFFICIENT_HISTORY.value,
                "clipped": 1 if y < 0 else 0,
            }
        )
    return rows, "ok"
