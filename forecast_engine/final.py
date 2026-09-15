"""Previsao final (reajuste no historico completo) e comparador.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

import polars as pl

from contracts import (
    ForecastConfig,
    SourceFrequency,
    RegressorSpec,
    IntervalMethod,
    INT_MIN_HISTORY_REGULAR,
)

from .dates import _add_period, _freq_str, _gen_future_dates, _season_length
from .metrics import _apply_floor
from .models import _extract_yhat
from .regressors import _forecast_regressor_final
from .ml import ml_final



def _pkg_attr(name: str):
    """Lookup tardio no namespace do pacote (honra monkeypatch)."""
    import forecast_engine as _pkg
    return getattr(_pkg, name)

def forecast_final(
    series: pl.DataFrame,
    alias: str,
    horizon: int,
    freq: SourceFrequency,
    config: ForecastConfig,
    regressors: list[RegressorSpec] = None,
    df_all: pl.DataFrame | None = None,
    entities=None,
    future_overrides: dict | None = None,
) -> tuple[list[dict], str]:
    """Reajusta vencedor sobre todo o histórico elegível e projeta h passos."""
    if alias in ("LightGBM", "XGBoost"):
        if df_all is None:
            return [], "ml_needs_global_frame"
        return ml_final(
            df_all,
            str(series["series_id"].first()),
            str(series["measure"].first()),
            freq,
            horizon,
            seed=config.seed,
            n_jobs=config.n_jobs,
            model_name=alias,
        )
    if alias == "AutoARIMA_X":
        return _forecast_regressor_final(
            series,
            horizon,
            freq,
            config,
            regressors or [],
            entities,
            future_overrides=future_overrides,
        )

    from statsforecast import StatsForecast
    from statsforecast.models import (
        Naive,
        HistoricAverage,
        SeasonalNaive,
        AutoETS,
        AutoTheta,
        CrostonSBA,
        TSB,
        AutoCES,
        AutoARIMA,
        AutoTBATS,
        WindowAverage,
        RandomWalkWithDrift,
        Holt,
    )

    season_len = _season_length(freq)
    model_map = {
        "Naive": lambda: Naive(),
        "MediaMovel3": lambda: WindowAverage(window_size=3),
        "MediaMovel6": lambda: WindowAverage(window_size=6),
        "MediaMovel12": lambda: WindowAverage(window_size=12),
        "HistoricAverage": lambda: HistoricAverage(),
        "SeasonalNaive": lambda: SeasonalNaive(season_length=season_len),
        "RegLinearDrift": lambda: RandomWalkWithDrift(),
        "Holt": lambda: Holt(season_length=1),
        "HoltDamped": lambda: AutoETS(model="AAdN", damped=True),
        "CrostonSBA": lambda: CrostonSBA(),
        "TSB": lambda: TSB(alpha_d=0.2, alpha_p=0.2),
        "AutoETS": lambda: AutoETS(),
        "ETS_Damped": lambda: AutoETS(damped=True),
        "AutoTheta": lambda: AutoTheta(),
        "AutoCES": lambda: AutoCES(),
        "AutoTBATS": lambda: AutoTBATS(season_length=season_len),
        "AutoARIMA": lambda: AutoARIMA(
            season_length=season_len,
            max_p=3,
            max_q=3,
            max_P=1,
            max_Q=1,
            max_order=5,
            approximation=True,
        ),
    }
    if alias == "ZeroBaseline":
        last = series["ds"].max()
        rows = []
        for d in _gen_future_dates(last, freq, horizon):
            rows.append(
                {
                    "ds": d,
                    "yhat": 0.0,
                    "lo80": None,
                    "hi80": None,
                    "interval_method": IntervalMethod.NULL.value,
                    "clipped": 0,
                }
            )
        return rows, "ok"
    if alias not in model_map:
        return [], "unknown_model"
    freq_str = _freq_str(freq)
    try:
        mdl = model_map[alias]()
        df = series.rename({"ds": "ds", "y": "y", "series_id": "unique_id"})[
            ["unique_id", "ds", "y"]
        ]
        sf = StatsForecast(
            models=[mdl], freq=freq_str, n_jobs=1, fallback_model=Naive()
        )
        fc = sf.forecast(df=df, h=horizon, level=[80])
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:200]
    last = series["ds"].max()
    # Guarda conformal (sec 9.4): só há intervalo quando o histórico comporta duas
    # janelas de calibração do horizonte solicitado + treino mínimo residual.
    n_history = series.height
    conformal_ok = n_history >= 2 * horizon + INT_MIN_HISTORY_REGULAR
    rows = []
    for i, row in enumerate(fc.to_dicts(), start=1):
        yhat = float(row.get(alias, _extract_yhat(row, alias)))
        lo80 = row.get(f"{alias}-lo-80")
        hi80 = row.get(f"{alias}-hi-80")
        if lo80 is None:
            lo80 = row.get("lo-80")
        if hi80 is None:
            hi80 = row.get("hi-80")
        if not conformal_ok:
            lo80, hi80 = None, None
        clipped = 0
        if yhat != yhat:  # NaN
            continue
        if yhat < 0:
            yhat = 0.0
            clipped = 1
        lo80 = _apply_floor(float(lo80)) if lo80 is not None else None
        hi80 = _apply_floor(float(hi80)) if hi80 is not None else None
        if lo80 is not None and hi80 is not None and lo80 > hi80:
            lo80, hi80 = None, None
        im = (
            IntervalMethod.NATIVE.value
            if lo80 is not None
            else (
                IntervalMethod.UNAVAILABLE_INSUFFICIENT_HISTORY.value
                if not conformal_ok
                else IntervalMethod.NULL.value
            )
        )
        rows.append(
            {
                "ds": _add_period(last, freq, i),
                "yhat": float(yhat),
                "lo80": lo80,
                "hi80": hi80,
                "interval_method": im,
                "clipped": clipped,
            }
        )
    return rows, "ok"


def forecast_candidate(
    series: pl.DataFrame, alias: str, study, config: ForecastConfig
) -> tuple[list[dict], str]:
    """Previsão futura de UM candidato específico, sob demanda (T6.4).

    Reusa `forecast_final` sem a cadeia de fallback: se o candidato pedido
    falhar, devolve `status != "ok"` em vez de silenciosamente trocar de
    modelo — o comparador de modelos do dashboard precisa saber que ESSE
    modelo não funcionou nesta série, não ver a saída de outro modelo.
    """
    return _pkg_attr("forecast_final")(
        series, alias, config.horizon_periods, study.model_frequency, config
    )


def _forecast_with_fallback(
    series,
    alias: str,
    candidates,
    config: ForecastConfig,
    study,
    regressors,
    df_all,
    entities=None,
    override=None,
):
    """Tenta o vencedor; em falha, segue a ordem de ranking e termina em Naive.

    Devolve (rows, used_alias, status, fallback_used). A saída finita já tem
    piso zero aplicado por forecast_final. Nenhum stub aqui: se nenhum modelo
    produzir previsão, retorna ([], alias, status, True) para a rodada contar
    a série como falha e marcar run como parcial.
    """
    preference = [alias] + [
        c.alias for c in sorted(candidates, key=lambda c: c.rank) if c.alias != alias
    ]
    if "Naive" not in preference:
        preference.append("Naive")
    for cand in preference:
        rows, status = _pkg_attr("forecast_final")(
            series,
            cand,
            config.horizon_periods,
            study.model_frequency,
            config,
            regressors,
            df_all=df_all,
            entities=entities,
            future_overrides=override if cand == "AutoARIMA_X" else None,
        )
        if rows and status == "ok":
            return rows, cand, status, cand != alias
    rows, status = _pkg_attr("forecast_final")(
        series,
        "Naive",
        config.horizon_periods,
        study.model_frequency,
        config,
        regressors,
        df_all=df_all,
        entities=entities,
    )
    if rows and status == "ok":
        return rows, "Naive", status, "Naive" != alias
    return [], alias, status, True

