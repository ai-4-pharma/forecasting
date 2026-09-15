"""Orquestrador run_forecast (gerador por lotes) e cancelamento.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

from typing import Iterator
import polars as pl

from contracts import (
    ForecastBatch,
    HierarchyMode,
    PredictionStatus,
    RunEvent,
    RunInputs,
    RunStage,
    ValidationIssue,
)

from .const import ML_MIN_ENTITIES, ML_MIN_ROWS, RANK_BY_ALIAS

from .metrics import _empty_cv, _empty_pred, _empty_scores, _empty_select
from .models import _make_spec, build_candidates
from .ml import _xgb_available, ml_forecast_fold
from .hierarchy import aggregate_history, build_hierarchy_nodes, build_run_nodes, reconcile_bottom_up
from .cv import build_cv_windows, evaluate_candidates
from .final import _forecast_with_fallback
from .scenarios import generate_scenario_predictions



def _pkg_attr(name: str):
    """Lookup tardio no namespace do pacote (honra monkeypatch)."""
    import forecast_engine as _pkg
    return getattr(_pkg, name)

def _cancel_requested(should_cancel) -> bool:
    """Consulta o flag opcional de cancelamento apenas ENTRE lotes (sec 4/8.2).

    Nunca é invocado dentro de uma chamada estatística/cv em andamento, então uma
    rodada não promete cancelamento imediato de um lote já em execução.
    """
    return should_cancel is not None and should_cancel()


def _consolidate_partial(
    batch_out: list[ForecastBatch], preds: list[dict]
) -> ForecastBatch:
    """Monta um ForecastBatch parcial a partir do que já foi produzido.

    Usado quando o usuário cancela a rodada: as previsões finais talvez estejam
    incompletas, mas cv/scores/selection das etapas concluídas são preservados
    para diagnóstico (sec 8.2). Não inventa zeros para o que falta.
    """
    pred_df = pl.DataFrame(preds or _empty_pred())
    cv_df = (
        pl.concat([b.cv_predictions for b in batch_out]) if batch_out else _empty_cv()
    )
    scores_df = (
        pl.concat([b.scores for b in batch_out]) if batch_out else _empty_scores()
    )
    sel_df = (
        pl.concat([b.selection for b in batch_out]) if batch_out else _empty_select()
    )
    issues = [iss for b in batch_out for iss in b.issues]
    return ForecastBatch(pred_df, cv_df, scores_df, sel_df, issues)


def _cancelled_event(
    batch_out: list[ForecastBatch],
    preds: list[dict],
    n_failed: int,
    total: int,
    nodes: pl.DataFrame | None = None,
) -> RunEvent:
    return RunEvent(
        RunStage.CANCELLED,
        len(preds),
        total,
        "Rodada cancelada pelo usuário; resultados parciais de folds foram preservados.",
        payload={
            "batch": _consolidate_partial(batch_out, preds),
            "n_failed": n_failed,
            "n_predictions": len(preds),
            "partial": True,
            "nodes": nodes,
        },
    )


def run_forecast(inputs: RunInputs) -> Iterator[RunEvent]:
    """Fluxo de eventos em lotes: profile -> cv -> forecast -> scenario -> reconcile."""
    config = inputs.config
    study = inputs.study
    prepared = inputs.prepared
    hierarchy = config.hierarchy
    node_meta: dict[str, dict] | None = None
    node_mode = ""
    # hierarquia (P25/P26): monta nós e, em independente agregado, agrega o treino
    working = prepared
    if (
        hierarchy
        and hierarchy.ordered_levels
        and inputs.entities is not None
        and inputs.entities.height
    ):
        _node_rows, node_meta = build_hierarchy_nodes(inputs.entities, hierarchy)
        if hierarchy.mode == HierarchyMode.INDEPENDENT:
            level_names = [tuple(lv) for lv in hierarchy.ordered_levels]
            fc = (
                tuple(hierarchy.forecast_level)
                if hierarchy.forecast_level is not None
                else level_names[-1]
            )
            if fc in level_names:
                lvl_idx = level_names.index(fc)
                if lvl_idx < len(level_names) - 1:
                    working = aggregate_history(
                        prepared, inputs.entities, hierarchy, lvl_idx
                    )
                    node_mode = "independent_aggregate"
                else:
                    node_mode = "leaf"
            else:
                node_mode = "leaf"
        elif hierarchy.mode == HierarchyMode.MINTRACE:
            node_mode = "mintrace"
        else:
            node_mode = "bottom_up"
    all_series = working["series_id"].unique().to_list()
    total = len(all_series)
    nodes_frame = build_run_nodes(prepared, inputs.entities, hierarchy, node_meta)

    yield RunEvent(RunStage.STARTED, 0, total, "Rodada iniciada.")
    candidates = build_candidates(None, study, config)
    wanted_ml = set(getattr(config, "candidate_aliases", []) or [])
    if (
        config.enable_ml
        and _pkg_attr("_ml_available")()
        and (not wanted_ml or "LightGBM" in wanted_ml)
    ):
        candidates.append(
            _make_spec(
                "LightGBM",
                need_ml=True,
                global_model=True,
                min_train=ML_MIN_ROWS,
                rank=RANK_BY_ALIAS["LightGBM"],
            )
        )
    if (
        config.enable_ml
        and _xgb_available()
        and (not wanted_ml or "XGBoost" in wanted_ml)
    ):
        candidates.append(
            _make_spec(
                "XGBoost",
                need_ml=True,
                global_model=True,
                min_train=ML_MIN_ROWS,
                rank=RANK_BY_ALIAS["XGBoost"],
            )
        )

    windows = build_cv_windows(working, study, config)
    yield RunEvent(
        RunStage.PROFILE, 0, total, f"Validação temporal com {len(windows)} folds."
    )

    # Pré-computa previsões dos candidatos globais por medida/corte.
    ml_cv_preds: dict[str, dict] = {}
    ml_aliases_to_train = [c.alias for c in candidates if c.need_ml]
    if config.enable_ml and ml_aliases_to_train:
        gdf = working.rename({"series_id": "unique_id"})[
            ["unique_id", "measure", "ds", "y"]
        ].sort(["unique_id", "ds"])
        for ml_alias in ml_aliases_to_train:
            alias_preds: dict[tuple, dict] = {}
            for measure in sorted(set(gdf["measure"].to_list())):
                n_series_measure = gdf.filter(pl.col("measure") == measure)[
                    "unique_id"
                ].n_unique()
                if n_series_measure < ML_MIN_ENTITIES:
                    continue
                for w in windows:
                    try:
                        out, status = ml_forecast_fold(
                            gdf,
                            gdf["unique_id"].unique().to_list(),
                            w.cutoff,
                            study.model_frequency,
                            measure,
                            len(w.eval_dates),
                            seed=config.seed,
                            n_jobs=config.n_jobs,
                            model_name=ml_alias,
                        )
                        if status == "ok" and out is not None:
                            for r in out.to_dicts():
                                key = (str(r["unique_id"]), w.cutoff, r["ds"])
                                alias_preds[key] = r
                    except Exception:  # noqa: BLE001
                        continue
            if alias_preds:
                ml_cv_preds[ml_alias] = alias_preds

    preds: list[dict] = []
    n_failed = 0
    batch_out: list[ForecastBatch] = []
    for i in range(0, len(all_series), config.batch_size):
        if _cancel_requested(inputs.should_cancel):
            yield _cancelled_event(batch_out, preds, n_failed, total, nodes=nodes_frame)
            return
        batch_ids = all_series[i : i + config.batch_size]
        batch = working.filter(pl.col("series_id").is_in(batch_ids))
        fb = evaluate_candidates(
            batch,
            inputs.policy,
            candidates,
            windows,
            inputs.regressors,
            config=config,
            entities=inputs.entities,
            ml_cv_preds=ml_cv_preds or None,
        )
        batch_out.append(fb)
        yield RunEvent(
            RunStage.CV,
            min(i + config.batch_size, total),
            total,
            f"Folds avaliados para lote {i // config.batch_size + 1}.",
            batch_id=f"cv-{i // config.batch_size}",
        )

    # previsões finais por série vencedora
    winner_map: dict[str, str] = {}
    if batch_out:
        sel = pl.concat([b.selection for b in batch_out])
        for r in sel.to_dicts():
            winner_map[r["node_id"]] = r["model_alias"]

    # frame global para o candidato LightGBM (uma linha por série em cada data)
    df_all = (
        working.rename({"series_id": "unique_id"})[
            ["unique_id", "measure", "ds", "y"]
        ].sort(["unique_id", "ds"])
        if config.enable_ml
        else None
    )

    # preds/n_failed, inicializados antes do loop de CV para o cancelamento
    # parcial reutilizá-los; aqui não são redefinidos (sec 8.2).
    for series_id in all_series:
        if _cancel_requested(inputs.should_cancel):
            yield _cancelled_event(batch_out, preds, n_failed, total, nodes=nodes_frame)
            return
        series = working.filter(pl.col("series_id") == series_id).sort("ds")
        measure = series["measure"].first()
        alias = winner_map.get(series_id, "Naive")
        rows, used_alias, status, fallback_used = _forecast_with_fallback(
            series,
            alias,
            candidates,
            config,
            study,
            inputs.regressors,
            df_all,
            entities=inputs.entities,
        )
        if not rows:
            n_failed += 1
            yield RunEvent(
                RunStage.FORECAST,
                all_series.index(series_id) + 1,
                total,
                f"Série sem previsão ({measure}): {status}.",
            )
            continue
        pred_status = (
            PredictionStatus.FALLBACK.value
            if fallback_used
            else PredictionStatus.OK.value
        )
        for r in rows:
            preds.append(
                {
                    "node_id": series_id,
                    "entity_id": series["entity_id"].first(),
                    "level": "folha",
                    "measure": measure,
                    "scenario_id": "base",
                    "ds": r["ds"],
                    "yhat": r["yhat"],
                    "lo80": r["lo80"],
                    "hi80": r["hi80"],
                    "model_alias": used_alias,
                    "interval_method": r["interval_method"],
                    "status": pred_status,
                }
            )
        yield RunEvent(
            RunStage.FORECAST,
            all_series.index(series_id) + 1,
            total,
            f"Previsão final para {measure}.",
        )

    if _cancel_requested(inputs.should_cancel):
        yield _cancelled_event(batch_out, preds, n_failed, total, nodes=nodes_frame)
        return

    yield RunEvent(
        RunStage.RECONCILE,
        total,
        total,
        "Conciliação concluída.",
        payload={
            "n_predictions": len(preds),
            "n_failed": n_failed,
        },
    )

    # build ForecastBatch consolidado
    pred_df = pl.DataFrame(preds or _empty_pred())
    cv_df = pl.concat(
        [b.cv_predictions for b in batch_out] if batch_out else [_empty_cv()]
    )
    scores_df = pl.concat(
        [b.scores for b in batch_out] if batch_out else [_empty_scores()]
    )
    sel_df = pl.concat(
        [b.selection for b in batch_out] if batch_out else [_empty_select()]
    )
    # cenários determinísticos sobre a previsão base (P22/P24)
    scenario_issues: list[ValidationIssue] = []
    scenario_factors = pl.DataFrame()
    n_total_predictions = len(preds)
    if preds:
        reg_specs = {str(c.alias): c for c in candidates if c.alias == "AutoARIMA_X"}

        def _recompute_base(sc):
            """Reconstrói a base com X_future alterado (AutoARIMA_X) (P24)."""
            if not getattr(sc, "regressor_future_overrides", None):
                return pred_df
            rows_re = []
            for sid in all_series:
                base_rows = pred_df.filter(pl.col("node_id") == sid)
                alias = winner_map.get(sid, "Naive")
                supports = alias == "AutoARIMA_X" and bool(
                    inputs.regressors and reg_specs
                )
                if not supports:
                    rows_re.extend(base_rows.to_dicts())
                    continue
                series = working.filter(pl.col("series_id") == sid).sort("ds")
                r, used, status, fb_used = _forecast_with_fallback(
                    series,
                    alias,
                    candidates,
                    config,
                    study,
                    inputs.regressors,
                    df_all,
                    entities=inputs.entities,
                    override=sc.regressor_future_overrides,
                )
                if not r:
                    rows_re.extend(base_rows.to_dicts())
                    continue
                pstatus = (
                    PredictionStatus.FALLBACK.value
                    if fb_used
                    else PredictionStatus.OK.value
                )
                for rr in r:
                    rows_re.append(
                        {
                            "node_id": sid,
                            "entity_id": series["entity_id"].first(),
                            "level": "folha",
                            "measure": series["measure"].first(),
                            "scenario_id": "base",
                            "ds": rr["ds"],
                            "yhat": rr["yhat"],
                            "lo80": rr["lo80"],
                            "hi80": rr["hi80"],
                            "model_alias": used,
                            "interval_method": rr["interval_method"],
                            "status": pstatus,
                        }
                    )
            return pl.DataFrame(rows_re, schema=pred_df.schema) if rows_re else pred_df

        scenario_df, scenario_issues, scenario_factors = generate_scenario_predictions(
            pred_df,
            inputs.scenarios,
            inputs.entities,
            recompute_base=_recompute_base,
            regressors=inputs.regressors,
            node_meta=node_meta if node_mode == "independent_aggregate" else None,
        )
        if scenario_df.height:
            pred_df = pl.concat([pred_df, scenario_df])
            n_total_predictions += scenario_df.height
        yield RunEvent(
            RunStage.SCENARIO,
            total,
            total,
            f"Cenários aplicados: {scenario_df.height} previsões com fatores.",
            payload={"n_scenario_predictions": scenario_df.height},
        )
    if node_mode in ("bottom_up", "mintrace") and node_meta:
        pred_df = reconcile_bottom_up(pred_df, hierarchy, node_meta)
        if node_mode == "mintrace":
            pred_df = pred_df.with_columns(
                pl.when(pl.col("model_alias") == "BottomUp")
                .then(pl.lit("MinT"))
                .otherwise(pl.col("model_alias"))
                .alias("model_alias")
            )
        n_total_predictions = pred_df.height
    all_issues = [b.issues for b in batch_out]
    if scenario_issues:
        all_issues = all_issues + [scenario_issues]
    fb = ForecastBatch(
        pred_df,
        cv_df,
        scores_df,
        sel_df,
        [i for grp in all_issues for i in grp],
        scenario_factors=scenario_factors,
    )
    yield RunEvent(
        RunStage.COMPLETED,
        total,
        total,
        "Rodada concluída.",
        payload={
            "batch": fb,
            "n_failed": n_failed,
            "n_predictions": n_total_predictions,
            "nodes": nodes_frame,
        },
    )

