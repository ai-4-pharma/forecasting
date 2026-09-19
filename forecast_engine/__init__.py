"""Pacote forecast_engine (fatiado de forecast_engine.py single-file).

Importadores (`import forecast_engine`, `from forecast_engine import X`)
continuam funcionando: todos os nomes publicos e privados sao
reexportados aqui.
"""

from contracts import stable_id

from ._quiet import install_warning_filters, quiet_native

install_warning_filters()

from .const import (
    MODEL_ORDER,
    RANK_BY_ALIAS,
    MODEL_LABELS_PT,
    ML_MIN_ENTITIES,
    ML_MIN_ROWS,
)

from .dates import (
    _freq_str,
    _pd_freq_str,
    _season_length,
    _effective_season_length,
    _freq_from_windows,
    _add_period,
    _gen_future_dates,
)

from .metrics import (
    _mse_mae,
    _apply_floor,
    _score_from_evals,
    _empty_pred,
    _empty_cv,
    _empty_scores,
    _empty_select,
)

from .models import (
    model_label,
    _make_spec,
    build_candidates,
    _extract_yhat,
    _sf_n_jobs,
    _build_sf_model,
)

from .regressors import (
    _regressor_family_conflicts,
    validate_regressors,
    _pearson,
    _entity_dims,
    regressor_applies,
    regressor_x_history,
    regressor_x_future,
    last_history_value,
    _forecast_regressor_final,
    _run_regressor_folds,
)

from .ml import (
    _ml_available,
    _xgb_available,
    _ml_features,
    train_global_model,
    ml_forecast_fold,
    ml_final,
)

from .hierarchy import (
    validate_hierarchy,
    build_hierarchy_nodes,
    _entity_to_leaf,
    _entity_dimensions_json,
    build_run_nodes,
    aggregate_history,
    reconcile_bottom_up,
)

from .cv import (
    build_cv_windows,
    _default_cv_horizon,
    evaluate_candidates,
    _largest_h,
    _run_folds,
    _run_folds_panel,
    _zero_baseline,
)

from .final import (
    forecast_final,
    forecast_final_panel,
    forecast_candidate,
    _forecast_with_fallback,
)

from .scenarios import (
    _rule_applies,
    compile_assumptions,
    scenario_conflicts,
    _annual_occurrences,
    _factor_for,
    _entity_matches,
    apply_scenario,
    apply_scenario_nodes,
    generate_scenario_predictions,
    derive_mat_derived,
)

from .runner import (
    _cancel_requested,
    _consolidate_partial,
    _cancelled_event,
    run_forecast,
)
