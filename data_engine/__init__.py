"""Pacote data_engine (fatiado de data_engine.py single-file).

Importadores (`import data_engine`, `from data_engine import X`)
continuam funcionando: todos os nomes publicos e privados sao
reexportados aqui.
"""

from .dates import (
    _shift_period,
    _period_labels,
    _period_label,
    _parse_period,
    parse_date_main,
    build_time_grid,
)

from .templates import (
    build_template,
    _template_xlsx
)

from .ingest import (
    _detect_delimiter,
    _sniff_encoding,
    _suggest_parse_date,
    suggest_time_settings,
    inspect_file,
    _inspect_csv,
    _inspect_xlsx
)

from .normalize import (
    _read_dataframe,
    _parse_number,
    entity_matrix_stable,
    normalize_file,
    _observations_content_digest,
    _store_entity
)

from .profile import (
    profile_data,
    prepare_data,
    _winsorize
)

from .db import (
    open_database,
    initialize_database,
    _now_ts,
    _tx,
    save_dataset,
    load_dataset,
    save_preparation,
    create_run,
    persist_batch,
    persist_nodes,
    finish_run,
    load_assumptions,
    save_assumptions,
    list_datasets,
    list_runs,
    mark_interrupted_runs
)

from .results import (
    _query_predictions,
    _dim_filtered_node_ids,
    _query_nodes,
    _query_scores,
    _query_issues,
    load_run_study,
    list_run_nodes,
    list_run_measures,
    list_run_scenarios,
    _aggregate_backtest,
    _load_prepared_history,
    forecast_candidate_for_node,
    _empty_candidate_rows,
    _node_entity_map,
    _view_history,
    _view_cards,
    _last_historical_position,
    _mat_view_derived,
    _mat_view_direct,
    query_results
)

from .exports import (
    _dep_versions,
    _run_mapping,
    _export_rf,
    _node_meta,
    _node_coverage,
    _long_columns,
    _empty_long,
    _history_mat_positions,
    _measure_export_part,
    _round_and_recompose,
    _export_long,
    _to_wide,
    _protect_formula_texts,
    _csv_artifact,
    _export_metadata,
    _scores_frame,
    _quality_frame,
    _assumptions_frame,
    _append_frame,
    _write_sheet,
    _write_metadata_sheet,
    _build_xlsx,
    export_results,
    export_metrics_csv,
    export_quality_csv
)
