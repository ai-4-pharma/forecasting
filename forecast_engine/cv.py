"""Janelas de validacao temporal e avaliacao vetorizada de candidatos.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

import datetime as dt
import numpy as np
import polars as pl

from contracts import (
    ForecastBatch,
    ForecastConfig,
    SourceFrequency,
    CVWindow,
    ValidationIssue,
    MIN_CV_FIRST_TRAIN,
    MIN_INTERMITTENT_POSITIVES,
)

from .dates import _add_period, _freq_from_windows, _freq_str, _season_length
from .metrics import _apply_floor, _empty_cv, _empty_pred, _empty_scores, _empty_select, _mse_mae, _score_from_evals, _select_winner
from .models import _build_sf_model, _extract_yhat, _sf_n_jobs
from .regressors import _run_regressor_folds


def build_cv_windows(data, study, config: ForecastConfig) -> list[CVWindow]:
    """Corta séries de forma temporal (treino expansivo, testes não sobrepostos)."""
    h = config.cv_horizon or _default_cv_horizon(study, config.horizon_periods)
    if isinstance(data, pl.DataFrame):
        dates = sorted(set(data["ds"].to_list()))
    else:
        dates = sorted(set(data))
    if not dates:
        return []
    n = len(dates)
    # maior K <= cv_windows com primeiro treino com pelo menos 3 observações
    k = 0
    for candidate_k in range(min(config.cv_windows, n // h), 0, -1):
        first_cutoff_idx = (
            n - candidate_k * h
        )  # índice da 1ª observação do teste do fold 0
        if first_cutoff_idx >= 3:
            k = candidate_k
            break
    if k < MIN_CV_FIRST_TRAIN and n // h < MIN_CV_FIRST_TRAIN:
        k = max(1, k) if n > h + 3 else 0
    if k == 0:
        return []
    out: list[CVWindow] = []
    for j in range(k):
        # fold j: treino até o índice (n - (k-j)*h + 1) - 1 => corte em n-(k-j)*h
        test_start_idx = n - (k - j) * h
        if test_start_idx <= 0:
            continue
        cutoff = dates[test_start_idx - 1]
        test_start = _add_period(cutoff, study.model_frequency, 1)
        test_end = _add_period(cutoff, study.model_frequency, h)
        eval_dates = [
            _add_period(cutoff, study.model_frequency, i) for i in range(1, h + 1)
        ]
        out.append(
            CVWindow(
                cutoff=cutoff,
                train_start=dates[0],
                train_end=cutoff,
                test_start=test_start,
                test_end=test_end,
                eval_dates=eval_dates,
            )
        )
    return out


def _default_cv_horizon(study, horizon: int) -> int:
    f = study.model_frequency
    if f == SourceFrequency.YEARLY:
        return min(horizon, 1)
    if f == SourceFrequency.QUARTERLY:
        return min(horizon, 2)
    return min(horizon, 3)


# ---------------------------------------------------------------------------
# Avaliação temporal e seleção (P16/P17)
# ---------------------------------------------------------------------------


def _extract_series(df: pl.DataFrame, series_id: str, measure: str) -> pl.DataFrame:
    return df.filter(
        pl.col("series_id") == series_id, pl.col("measure") == measure
    ).sort("ds")


def _add_cutoff_date(series: pl.DataFrame, cutoff: dt.date) -> pl.DataFrame:
    return series.filter(pl.col("ds") <= cutoff)


def evaluate_candidates(
    data,
    policy,
    candidates,
    windows,
    regressors,
    config: ForecastConfig | None = None,
    entities=None,
    ml_cv_preds: dict | None = None,
) -> ForecastBatch:
    """Avalia candidatos por série com folds temporais (sec 9.2).

    O candidato global (LightGBM) é avaliado com previsões pré-computadas
    (`ml_cv_preds`) geradas uma vez sobre toda a população da medida em
    `run_forecast`; somente atinge esta função quando o complemento ML está
    instalado. Sem `ml_cv_preds`, nenhum candidato `need_ml` concorre.
    """
    n_windows = len(windows)
    cv_rows: list[dict] = []
    score_rows: list[dict] = []
    pred_rows: list[dict] = []
    selec_rows: list[dict] = []
    issues: list[ValidationIssue] = []

    if hasattr(data, "prepared"):
        prepared = data.prepared
    else:
        prepared = data

    # previsões dos modelos globais por fold (um treino por corte/medida).
    # ml_cv_preds pode ser {alias: {(node,cutoff,ds): pred}} (novo) ou
    # {(node,cutoff,ds): pred} legado (LightGBM único).
    ml_preds_by_alias: dict[str, dict] = {}
    ml_candidates = [c for c in candidates if c.need_ml]
    if ml_candidates and ml_cv_preds:
        if ml_cv_preds and all(isinstance(k, str) for k in ml_cv_preds.keys()):
            ml_preds_by_alias = ml_cv_preds
        else:
            ml_preds_by_alias = {"LightGBM": ml_cv_preds}

    series_by = {}
    for (series_id, measure), grp in prepared.group_by(["series_id", "measure"]):
        series_by[(series_id, measure)] = grp.sort("ds")

    # Pass 1: elegibilidade por série (mesma regra de antes, sem fits).
    eligible_by_series: dict = {}
    short_series: set = set()
    for (series_id, measure), series in series_by.items():
        n = series.height
        if n < 4:
            short_series.add((series_id, measure))
            continue
        yvals = series["y"].to_numpy()
        positives = (yvals > 0).sum()
        all_zero = bool((yvals == 0).all())
        has_negatives = bool((yvals < 0).any())
        eligible_aliases: list[str] = []
        for c in candidates:
            if c.need_ml:
                continue
            ok = True
            if c.requires_positives and has_negatives:
                ok = False
            if c.intermittent:
                if has_negatives or positives < MIN_INTERMITTENT_POSITIVES:
                    ok = False
            if all_zero:
                ok = False
            if ok and c.alias != "ZeroBaseline":
                eligible_aliases.append(c.alias)
        if all_zero:
            eligible_aliases = ["ZeroBaseline"]
        eligible_by_series[(series_id, measure)] = eligible_aliases

    # Pass 2 vetorizado: 1 StatsForecast por (modelo, fold) com todas as
    # séries elegíveis + n_jobs entre séries (ganho 10-50x vs 1 fit/série).
    freq_cv = _freq_from_windows(windows)
    season_cv = _season_length(freq_cv)
    njobs = _sf_n_jobs(config)
    VECTOR_OK = {
        a
        for a in {c.alias for c in candidates if not c.need_ml}
        if _build_sf_model(a, season_cv) is not None
    }
    panel_evals: dict = {}
    for alias in sorted(VECTOR_OK):
        scope = [
            (sid, m)
            for (sid, m), aliases in eligible_by_series.items()
            if alias in aliases
        ]
        if not scope:
            continue
        try:
            rows = _run_folds_panel(
                prepared, scope, alias, windows, freq_cv, season_cv, njobs
            )
        except Exception:  # noqa: BLE001
            rows = None
        if not rows:
            # fallback por série preserva motivos de falha granulares
            for sid, m in scope:
                series = series_by[(sid, m)]
                _, eval_per_fold = _run_folds(
                    series,
                    alias,
                    candidates,
                    windows,
                    study=None,
                    regressors=regressors,
                    data=data,
                    policy=policy,
                    measure=m,
                    node_id=sid,
                    entities=entities,
                )
                for cv in eval_per_fold:
                    panel_evals.setdefault((sid, m, alias), []).append(cv)
            continue
        for cv in rows:
            panel_evals.setdefault((cv["node_id"], cv["measure"], alias), []).append(cv)

    for series_id, measure in short_series:
        node_id = series_id
        cv_rows.append(
            {
                "node_id": node_id,
                "measure": measure,
                "model_alias": "Naive",
                "cutoff": None,
                "ds": None,
                "y_actual": None,
                "yhat": None,
                "evaluated": False,
                "failure_reason": "insufficient_history",
            }
        )
        score_rows.append(
            {
                "node_id": node_id,
                "measure": measure,
                "model_alias": "Naive",
                "mae": None,
                "rmse": None,
                "wape": None,
                "bias": None,
                "n_eval": 0,
                "n_folds": 0,
                "eligible": False,
                "failure_reason": "insufficient_history",
            }
        )

    # Pass 3: monta scores por série (ML pré-computado + painel + X/Zero locais).
    for (series_id, measure), series in series_by.items():
        node_id = series_id
        if (series_id, measure) in short_series:
            continue
        eligible_aliases = list(eligible_by_series.get((series_id, measure), []))

        scores_raw: dict[str, dict] = {}
        for ml_cand in ml_candidates:
            ml_preds = ml_preds_by_alias.get(ml_cand.alias, {})
            if not ml_preds:
                continue
            ml_evals = [ev for k, ev in ml_preds.items() if k[0] == node_id]
            if not ml_evals:
                continue
            actual_map = {r["ds"]: r["y"] for r in series.to_dicts()}
            ml_eval_rows = [
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": ml_cand.alias,
                    "cutoff": k[1],
                    "ds": k[2],
                    "y_actual": actual_map.get(k[2]),
                    "yhat": _apply_floor(float(ev["yhat"]))
                    if ev.get("yhat") is not None
                    else None,
                    "evaluated": k[2] in actual_map,
                    "failure_reason": "" if k[2] in actual_map else "no_observed_test",
                }
                for k, ev in ml_preds.items()
                if k[0] == node_id
            ]
            for ev in ml_eval_rows:
                cv_rows.append(ev)
            scores_raw[ml_cand.alias] = _score_from_evals(ml_eval_rows, n_windows)
            eligible_aliases.append(ml_cand.alias)
        ml_aliases = {c.alias for c in ml_candidates}
        for alias in eligible_aliases:
            if alias in ml_aliases:
                continue
            eval_per_fold = panel_evals.get((node_id, measure, alias))
            if eval_per_fold is None:
                # não-vetorizável (ARIMAX/Zero): caminho local original
                _, eval_per_fold = _run_folds(
                    series,
                    alias,
                    candidates,
                    windows,
                    study=None,
                    regressors=regressors,
                    data=data,
                    policy=policy,
                    measure=measure,
                    node_id=node_id,
                    entities=entities,
                )
            for cv in eval_per_fold:
                cv_rows.append(cv)
            if not any(ev["evaluated"] for ev in eval_per_fold):
                scores_raw[alias] = None
                continue
            ys = np.array(
                [ev["y_actual"] for ev in eval_per_fold if ev["evaluated"]]
            ).astype(float)
            yh = np.array(
                [_apply_floor(ev["yhat"]) for ev in eval_per_fold if ev["evaluated"]]
            ).astype(float)
            mae, rmse, wape, bias = _mse_mae(ys, yh)
            scores_raw[alias] = {
                "mae": mae,
                "rmse": rmse,
                "wape": wape,
                "bias": bias,
                "n_eval": int(len(ys)),
                "n_folds": n_windows,
                "eligible": True,
            }
        winner, reason, fallback = _select_winner(scores_raw, eligible_aliases)
        for alias in scores_raw:
            s = scores_raw[alias]
            score_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": alias,
                    "mae": s["mae"] if s else None,
                    "rmse": s["rmse"] if s else None,
                    "wape": s["wape"] if s else None,
                    "bias": s["bias"] if s else None,
                    "n_eval": s["n_eval"] if s else 0,
                    "n_folds": s["n_folds"] if s else 0,
                    "eligible": bool(s) if s is not None else False,
                    "failure_reason": "" if s else "no_evaluable_folds",
                }
            )
        selec_rows.append(
            {
                "node_id": node_id,
                "measure": measure,
                "model_alias": winner,
                "selection_reason": reason,
                "cv_horizon": _largest_h(windows),
                "n_folds": n_windows,
                "fallback_used": bool(fallback),
                "clipped_count": 0,
            }
        )

    return ForecastBatch(
        predictions=pl.DataFrame(pred_rows or _empty_pred()),
        cv_predictions=pl.DataFrame(cv_rows or _empty_cv()),
        scores=pl.DataFrame(score_rows or _empty_scores()),
        selection=pl.DataFrame(selec_rows or _empty_select()),
        issues=issues,
    )


def _largest_h(windows: list[CVWindow]) -> int:
    return windows[-1].eval_dates.__len__() if windows else 0


def _run_folds(
    series,
    alias,
    candidates,
    windows,
    study,
    regressors,
    data,
    policy,
    measure: str,
    node_id: str,
    entities=None,
):
    """Roda folds para um candidato/série; devolve (preds, eval) alinhados."""
    if alias == "AutoARIMA_X":
        return _run_regressor_folds(
            series, regressors, entities, windows, node_id, measure
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
        SeasonalWindowAverage,
        RandomWalkWithDrift,
        Holt,
        HoltWinters,
    )

    def _window(w: int):
        return lambda: WindowAverage(window_size=w)

    model_map = {
        "Naive": lambda: Naive(),
        "MediaMovel3": _window(3),
        "MediaMovel6": _window(6),
        "MediaMovel12": _window(12),
        "HistoricAverage": lambda: HistoricAverage(),
        "SeasonalNaive": lambda: SeasonalNaive(season_length=12),
        "RegLinearDrift": lambda: RandomWalkWithDrift(),
        "Holt": lambda: Holt(season_length=1),
        "HoltDamped": lambda: AutoETS(model="AAdN", damped=True),
        "CrostonSBA": lambda: CrostonSBA(),
        "TSB": lambda: TSB(alpha_d=0.2, alpha_p=0.2),
        "AutoETS": lambda: AutoETS(),
        "ETS_Damped": lambda: AutoETS(damped=True),
        "AutoTheta": lambda: AutoTheta(),
        "AutoCES": lambda: AutoCES(),
        "AutoTBATS": lambda: AutoTBATS(season_length=12),
        "AutoARIMA": lambda: AutoARIMA(
            season_length=12,
            max_p=3,
            max_q=3,
            max_P=1,
            max_Q=1,
            max_order=5,
            approximation=True,
        ),
    }
    if alias == "ZeroBaseline":
        return _zero_baseline(series, windows, node_id, measure)
    if alias not in model_map:
        return [], []
    eval_rows: list[dict] = []
    pred_rows: list[dict] = []
    series_id = series["series_id"].first()
    if hasattr(data, "prepared"):
        trained_inputs = data.prepared
    else:
        trained_inputs = data
    freq = _freq_from_windows(windows)
    season_len = _season_length(freq)
    if alias == "SeasonalNaive":
        model_map["SeasonalNaive"] = lambda: SeasonalNaive(season_length=season_len)
    if alias == "AutoARIMA":
        model_map["AutoARIMA"] = lambda: AutoARIMA(
            season_length=season_len,
            max_p=3,
            max_q=3,
            max_P=1,
            max_Q=1,
            max_order=5,
            approximation=True,
        )
    for w in windows:
        train = trained_inputs.filter(
            pl.col("series_id") == series_id,
            pl.col("measure") == measure,
            pl.col("ds") <= w.cutoff,
        ).sort("ds")
        if train.height < 4:
            continue
        mdl = model_map[alias]()
        try:
            sf = StatsForecast(
                models=[mdl],
                freq=_freq_str(freq),
                n_jobs=1,
                fallback_model=Naive(),
            )
            df = train.rename({"ds": "ds", "y": "y", "series_id": "unique_id"})[
                ["unique_id", "ds", "y"]
            ]
            fc = sf.forecast(df=df, h=len(w.eval_dates), level=[80])
        except Exception as e:  # noqa: BLE001
            eval_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": alias,
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
        fc_rows = fc.to_dicts()
        for i, (ds_fc, row) in enumerate(zip(w.eval_dates, fc_rows)):
            yhat = _extract_yhat(row, alias)
            act = test_map.get(ds_fc)
            evaluated = act is not None
            eval_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": alias,
                    "cutoff": w.cutoff,
                    "ds": ds_fc,
                    "y_actual": float(act) if evaluated else None,
                    "yhat": float(_apply_floor(yhat)),
                    "evaluated": evaluated,
                    "failure_reason": "" if evaluated else "no_observed_test",
                }
            )
    return pred_rows, eval_rows


def _run_folds_panel(
    prepared,
    scope: list[tuple[str, str]],
    alias: str,
    windows,
    freq,
    season_len: int,
    n_jobs: int,
) -> list[dict]:
    """CV vetorizado: 1 StatsForecast por (modelo, fold) com todas as séries.

    `scope` = [(node_id=series_id, measure)]. Devolve eval rows no mesmo
    formato do `_run_folds` por série. Em falha do painel, o chamador faz
    fallback por série para preservar motivos de falha granulares.
    """
    from statsforecast import StatsForecast
    from statsforecast.models import Naive as _Naive

    eval_rows: list[dict] = []
    mdl = _build_sf_model(alias, season_len)
    if mdl is None:
        return eval_rows
    freq_str = _freq_str(freq)
    for w in windows:
        h = len(w.eval_dates)
        trains = []
        tests: dict = {}
        for node_id, measure in scope:
            tr = prepared.filter(
                pl.col("series_id") == node_id,
                pl.col("measure") == measure,
                pl.col("ds") <= w.cutoff,
            ).sort("ds")
            if tr.height < 4:
                continue
            trains.append(
                tr.select(
                    pl.col("series_id").alias("unique_id"), pl.col("ds"), pl.col("y")
                )
            )
            te = prepared.filter(
                pl.col("series_id") == node_id,
                pl.col("measure") == measure,
                pl.col("ds") > w.cutoff,
            ).sort("ds")
            tests[(node_id, measure)] = {r["ds"]: r["y"] for r in te.to_dicts()}
        if not trains:
            continue
        panel = pl.concat(trains).sort(["unique_id", "ds"])
        sf = StatsForecast(
            models=[_build_sf_model(alias, season_len)],
            freq=freq_str,
            n_jobs=n_jobs,
            fallback_model=_Naive(),
        )
        fc = sf.forecast(df=panel, h=h, level=[80])
        for row in fc.to_dicts():
            uid = str(row.get("unique_id"))
            ds = row.get("ds")
            try:
                from datetime import date as _date

                if hasattr(ds, "date"):
                    ds = ds.date()
                elif isinstance(ds, str):
                    ds = _date.fromisoformat(ds[:10])
            except Exception:  # noqa: BLE001
                pass
            # recupera (node_id, measure) — series_id já é único por medida
            match = next((k for k in tests if k[0] == uid), None)
            if match is None:
                continue
            node_id, measure = match
            act = tests[match].get(ds)
            evaluated = act is not None
            try:
                yhat = float(_apply_floor(_extract_yhat(row, alias)))
            except Exception:  # noqa: BLE001
                yhat = None
            eval_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": alias,
                    "cutoff": w.cutoff,
                    "ds": ds,
                    "y_actual": float(act) if evaluated else None,
                    "yhat": yhat,
                    "evaluated": evaluated,
                    "failure_reason": "" if evaluated else "no_observed_test",
                }
            )
    return eval_rows


def _zero_baseline(series, windows, node_id, measure):
    eval_rows = []
    pred_rows = []
    for w in windows:
        test = series.filter(pl.col("ds") > w.cutoff).sort("ds")
        test_map = {r["ds"]: r["y"] for r in test.to_dicts()}
        for ds_fc in w.eval_dates:
            act = test_map.get(ds_fc)
            eval_rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "model_alias": "ZeroBaseline",
                    "cutoff": w.cutoff,
                    "ds": ds_fc,
                    "y_actual": act,
                    "yhat": 0.0,
                    "evaluated": act is not None,
                    "failure_reason": "" if act is not None else "no_observed_test",
                }
            )
    return pred_rows, eval_rows

