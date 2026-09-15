"""Regressoras exogenas (ARIMAX): validacao, histórico e futuro.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

import datetime as dt
import math
import polars as pl

from contracts import (
    ForecastConfig,
    ScenarioConfig,
    SourceFrequency,
    RegressorSpec,
    ValidationIssue,
    Severity,
    IntervalMethod,
    ERR_SCENARIO_REG_FAMILY_DUP,
    ERR_REGRESSOR_COLLINEAR,
    ERR_REGRESSOR_UNAVAILABLE_AT_CUTOFF,
    INT_MIN_HISTORY_REGULAR,
    RegressorFillPolicy,
)

from .dates import _add_period, _freq_from_windows, _freq_str, _gen_future_dates, _season_length
from .metrics import _apply_floor
from .models import _extract_yhat


def _regressor_family_conflicts(
    scenario: ScenarioConfig, reg_name_by_id: dict[str, str]
) -> list[ValidationIssue]:
    """Família usada como regressora (override) e multiplicador (regra) bloqueia."""
    issues: list[ValidationIssue] = []
    rule_families = {
        r.family for r in scenario.rules if hasattr(r, "family") and r.family
    }
    for reg_id in scenario.regressor_future_overrides:
        name = reg_name_by_id.get(reg_id, reg_id)
        if name in rule_families:
            issues.append(
                ValidationIssue(
                    code=ERR_SCENARIO_REG_FAMILY_DUP,
                    severity=Severity.ERROR,
                    message=(
                        f"Cenário '{scenario.scenario_id}' usa a família '{name}' "
                        "como regressora (override) e multiplicador (regra); evite "
                        "dupla aplicação (sec 10.2)."
                    ),
                    details={"family": name, "scenario_id": scenario.scenario_id},
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Regressoras (P24)
# ---------------------------------------------------------------------------


def build_future_regressors(entities, periods, specs, scenario) -> pl.DataFrame:
    """Constrói frame future_x com regressoras declaradas e o override de cenário."""
    cols = ["unique_id", "ds"]
    rows = []
    if not specs:
        return pl.DataFrame(
            {"unique_id": [], "ds": []}, schema={"unique_id": pl.String, "ds": pl.Date}
        )
    id_to_name = {s.regressor_id: s.name for s in specs}
    for e in entities.to_dicts():
        for p in periods:
            row = {"unique_id": e["entity_id"], "ds": p}
            for s in specs:
                if not s.enabled:
                    continue
                v = s.future_values.get(p.isoformat())
                if v is None:
                    v = s.history_values.get(p.isoformat())
                row[s.name] = v
            # apply scenario override
            if scenario and scenario.regressor_future_overrides:
                for reg_id, overrides in scenario.regressor_future_overrides.items():
                    name = id_to_name.get(reg_id, reg_id)
                    if p.isoformat() in overrides:
                        row[name] = overrides[p.isoformat()]
            rows.append(row)
    return pl.DataFrame(rows)


def validate_regressors(
    specs: list[RegressorSpec], history: dict, future_periods: list
) -> list[ValidationIssue]:
    """Valida variação, colinearidade e cobertura (sec 10.2)."""
    issues: list[ValidationIssue] = []
    if not specs:
        return issues
    for spec in specs:
        if not spec.enabled:
            continue
        vals = [v for v in spec.history_values.values() if v not in (None, 0.0)]
        if len(set(vals)) < 2:
            issues.append(
                ValidationIssue(
                    code="E_REGRESSOR_FLAT",
                    severity=Severity.WARNING,
                    message=f"Regressora '{spec.name}' não tem variação histórica suficiente.",
                )
            )
        missing_future = [
            p for p in future_periods if p.isoformat() not in spec.future_values
        ]
        if missing_future and spec.known_in_advance:
            issues.append(
                ValidationIssue(
                    code="E_REGRESSOR_INCOMPLETE_FUTURE",
                    severity=Severity.WARNING,
                    message=f"Regressora '{spec.name}' sem cobertura futura para {len(missing_future)} períodos.",
                )
            )
        if not spec.known_in_advance and spec.fill_policy == RegressorFillPolicy.NONE:
            issues.append(
                ValidationIssue(
                    code=ERR_REGRESSOR_UNAVAILABLE_AT_CUTOFF,
                    severity=Severity.ERROR,
                    message=(
                        f"Regressora '{spec.name}' não é conhecida antecipadamente "
                        "e não tem política de hold (explicit_hold); o candidato "
                        "AutoARIMA_X fica indisponível."
                    ),
                )
            )
    # colinearidade perfeita entre pares de regressoras nos mesmos períodos
    enabled = [s for s in specs if s.enabled]
    for i in range(len(enabled)):
        for j in range(i + 1, len(enabled)):
            a, b = enabled[i], enabled[j]
            common = sorted(set(a.history_values) & set(b.history_values))
            pairs = [
                (a.history_values[p], b.history_values[p])
                for p in common
                if a.history_values[p] is not None and b.history_values[p] is not None
            ]
            if len(pairs) < 3:
                continue
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            corr = _pearson(xs, ys)
            if corr is not None and corr > 0.999:
                issues.append(
                    ValidationIssue(
                        code=ERR_REGRESSOR_COLLINEAR,
                        severity=Severity.ERROR,
                        message=(
                            f"Regressoras '{a.name}' e '{b.name}' são quase "
                            f"colineares (correlação {corr:.4f})."
                        ),
                    )
                )
    return issues


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if not xs or len(xs) < 2:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    if den == 0:
        return None
    return num / den


# ---------------------------------------------------------------------------
# Regressoras AutoARIMA_X (P24)
# ---------------------------------------------------------------------------


def _entity_dims(entities, entity_id) -> dict:
    if entities is None or entities.height == 0:
        return {}
    if entity_id is None:
        return {}
    import json

    row = entities.filter(pl.col("entity_id") == str(entity_id))
    if row.height == 0:
        return {}
    try:
        return json.loads(row["dimensions_json"].first())
    except Exception:  # noqa: BLE001
        return {}


def regressor_applies(spec: RegressorSpec, entity_dim: dict) -> bool:
    """Verifica se a regressora se aplica à entidade (escopo, sec 10.2)."""
    if not spec.enabled:
        return False
    for k, vals in spec.scope_filters.items():
        if entity_dim.get(k, "") not in vals:
            return False
    return True


def regressor_x_history(
    series: pl.DataFrame,
    regressors: list[RegressorSpec],
    entities,
    cutoff=None,
) -> pl.DataFrame | None:
    """X_df histórico (unique_id, ds, colunas) da série, até o corte."""
    if not regressors:
        return None
    dim = _entity_dims(entities, series["entity_id"].first())
    dates = series["ds"].unique().to_list()
    if cutoff is not None:
        dates = [d for d in dates if d <= cutoff]
    dates.sort()
    if not dates:
        return None
    sid = str(series["series_id"].first())
    base = pl.DataFrame({"unique_id": [sid] * len(dates), "ds": dates})
    for spec in regressors:
        if not regressor_applies(spec, dim):
            continue
        vals = [spec.history_values.get(d.isoformat()) for d in dates]
        base = base.with_columns(pl.Series(spec.name, vals))
    cols = [c for c in base.columns if c not in ("unique_id", "ds")]
    if not cols:
        return None
    return base


def regressor_x_future(
    series: pl.DataFrame,
    regressors: list[RegressorSpec],
    entities,
    future_dates: list[dt.date],
    overrides: dict | None = None,
) -> pl.DataFrame | None:
    """X_future com política de disponibilidade (sec 10.2) e overrides de cenário.

    `known_in_advance=true` usa future_values; `explicit_hold` repete o último
    valor histórico; sem política, devolve None (futuro indisponível).
    """
    if not regressors:
        return None
    dim = _entity_dims(entities, series["entity_id"].first())
    sid = str(series["series_id"].first())
    last_ds = series["ds"].max()
    base = pl.DataFrame(
        {"unique_id": [sid] * len(future_dates), "ds": list(future_dates)}
    )
    for spec in regressors:
        if not regressor_applies(spec, dim):
            continue
        if spec.known_in_advance:
            vals = [spec.future_values.get(d.isoformat()) for d in future_dates]
        elif spec.fill_policy == RegressorFillPolicy.EXPLICIT_HOLD:
            hold = last_history_value(spec, last_ds)
            vals = [hold] * len(future_dates)
        else:
            return None
        base = base.with_columns(pl.Series(spec.name, vals))
    if overrides:
        id_to_name = {s.regressor_id: s.name for s in regressors}
        for reg_id, ov in overrides.items():
            name = id_to_name.get(reg_id, reg_id)
            if name not in base.columns:
                continue
            for k, v in ov.items():
                base = base.with_columns(
                    pl.when(pl.col("ds").dt.strftime("%Y-%m-%d") == k)
                    .then(pl.lit(v))
                    .otherwise(pl.col(name))
                    .alias(name)
                )
    cols = [c for c in base.columns if c not in ("unique_id", "ds")]
    if not cols:
        return None
    return base


def last_history_value(spec: RegressorSpec, last_ds) -> float | None:
    """Último valor histórico da regressora até a data informada (hold)."""
    best = None
    best_key = ""
    for k, v in spec.history_values.items():
        if v is None:
            continue
        if k <= last_ds.isoformat() and (best is None or k > best_key):
            best = v
            best_key = k
    return best


def _forecast_regressor_final(
    series,
    horizon: int,
    freq: SourceFrequency,
    config: ForecastConfig,
    regressors: list[RegressorSpec],
    entities,
    future_overrides: dict | None = None,
) -> tuple[list[dict], str]:
    """Refit final AutoARIMA_X: treino com exógenas + X_future (com overrides)."""
    from statsforecast import StatsForecast
    from statsforecast.models import AutoARIMA, Naive

    if not regressors:
        return [], "no_regressors"
    x_train = regressor_x_history(series, regressors, entities)
    if x_train is None:
        return [], "regressor_unavailable"
    last = series["ds"].max()
    future_dates = _gen_future_dates(last, freq, horizon)
    x_future = regressor_x_future(
        series, regressors, entities, future_dates, overrides=future_overrides
    )
    if x_future is None:
        return [], "regressor_future_unavailable"
    season_len = _season_length(freq)
    mdl = AutoARIMA(
        season_length=season_len,
        max_p=3,
        max_q=3,
        max_P=1,
        max_Q=1,
        max_order=5,
        approximation=True,
    )
    df = series.rename({"ds": "ds", "y": "y", "series_id": "unique_id"})[
        ["unique_id", "ds", "y"]
    ]
    df = df.join(x_train.drop(["unique_id"]), on="ds", how="left")
    sf = StatsForecast(
        models=[mdl], freq=_freq_str(freq), n_jobs=1, fallback_model=Naive()
    )
    try:
        fc = sf.forecast(df=df, h=horizon, X_df=x_future, level=[config.interval_level])
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:200]
    n_history = series.height
    conformal_ok = n_history >= 2 * horizon + INT_MIN_HISTORY_REGULAR
    rows = []
    for i, row in enumerate(fc.to_dicts(), start=1):
        yhat = float(row.get("AutoARIMA", _extract_yhat(row, "AutoARIMA")))
        lo80 = row.get("AutoARIMA-lo-80")
        hi80 = row.get("AutoARIMA-hi-80")
        if not conformal_ok:
            lo80, hi80 = None, None
        if yhat != yhat:
            continue
        clipped = 1 if yhat < 0 else 0
        if yhat < 0:
            yhat = 0.0
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


def _run_regressor_folds(series, regressors, entities, windows, node_id, measure):
    """Folds de AutoARIMA_X alinhados, com disponibilidade por fold."""
    from statsforecast import StatsForecast
    from statsforecast.models import AutoARIMA, Naive

    freq = _freq_from_windows(windows)
    season_len = _season_length(freq)
    eval_rows: list[dict] = []
    pred_rows: list[dict] = []
    for w in windows:
        x_train = regressor_x_history(series, regressors, entities, cutoff=w.cutoff)
        if x_train is None:
            eval_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": "AutoARIMA_X",
                    "cutoff": w.cutoff,
                    "ds": None,
                    "y_actual": None,
                    "yhat": None,
                    "evaluated": False,
                    "failure_reason": "regressor_unavailable",
                }
            )
            continue
        x_future = regressor_x_future(series, regressors, entities, w.eval_dates)
        if x_future is None:
            eval_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": "AutoARIMA_X",
                    "cutoff": w.cutoff,
                    "ds": None,
                    "y_actual": None,
                    "yhat": None,
                    "evaluated": False,
                    "failure_reason": "regressor_future_unavailable",
                }
            )
            continue
        train = series.filter(pl.col("ds") <= w.cutoff).sort("ds")
        if train.height < 4:
            continue
        mdl = AutoARIMA(
            season_length=season_len,
            max_p=3,
            max_q=3,
            max_P=1,
            max_Q=1,
            max_order=5,
            approximation=True,
        )
        df = train.rename({"ds": "ds", "y": "y", "series_id": "unique_id"})[
            ["unique_id", "ds", "y"]
        ]
        x_tr = x_train.filter(pl.col("ds") <= w.cutoff).drop(["unique_id"])
        try:
            sf = StatsForecast(
                models=[mdl], freq=_freq_str(freq), n_jobs=1, fallback_model=Naive()
            )
            fc = sf.forecast(
                df=df.join(x_tr, on="ds", how="left"),
                h=len(w.eval_dates),
                X_df=x_future,
                level=[80],
            )
        except Exception as e:  # noqa: BLE001
            eval_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": "AutoARIMA_X",
                    "cutoff": w.cutoff,
                    "ds": None,
                    "y_actual": None,
                    "yhat": None,
                    "evaluated": False,
                    "failure_reason": str(e)[:200],
                }
            )
            continue
        test = series.filter(pl.col("ds") > w.cutoff).sort("ds")
        test_map = {r["ds"]: r["y"] for r in test.to_dicts()}
        for ds_fc, row in zip(w.eval_dates, fc.to_dicts()):
            yhat = float(row.get("AutoARIMA", _extract_yhat(row, "AutoARIMA")))
            act = test_map.get(ds_fc)
            eval_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": "AutoARIMA_X",
                    "cutoff": w.cutoff,
                    "ds": ds_fc,
                    "y_actual": float(act) if act is not None else None,
                    "yhat": float(_apply_floor(yhat)),
                    "evaluated": act is not None,
                    "failure_reason": "" if act is not None else "no_observed_test",
                }
            )
    return pred_rows, eval_rows

