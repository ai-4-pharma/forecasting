"""data_engine.db — Persistência DuckDB e ciclo de vida de datasets/rodadas.
(extraído de data_engine.py single-file; split 16/09/2026).
"""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from datetime import datetime
import json
import uuid
import duckdb
import polars as pl
from contracts import (
    CanonicalDataset,
    ForecastBatch,
    ForecastConfig,
    MappingConfig,
    PreparedDataset,
    RegressorSpec,
    RunSummary,
    ScenarioConfig,
    StudyConfig,
)


SCHEMA_TABLES = {
    "app_meta": """
        CREATE TABLE IF NOT EXISTS app_meta (
            key VARCHAR PRIMARY KEY,
            value VARCHAR
        )
    """,
    "datasets": """
        CREATE TABLE IF NOT EXISTS datasets (
            dataset_id VARCHAR PRIMARY KEY,
            name VARCHAR,
            filename VARCHAR,
            file_sha256 VARCHAR,
            created_at TIMESTAMP,
            study_json VARCHAR,
            mapping_json VARCHAR,
            fingerprint VARCHAR,
            status VARCHAR
        )
    """,
    "source_rows": """
        CREATE TABLE IF NOT EXISTS source_rows (
            dataset_id VARCHAR,
            source_row_id VARCHAR,
            sheet VARCHAR,
            row_number BIGINT,
            raw_json VARCHAR,
            PRIMARY KEY (dataset_id, source_row_id)
        )
    """,
    "entities": """
        CREATE TABLE IF NOT EXISTS entities (
            dataset_id VARCHAR,
            entity_id VARCHAR,
            key_json VARCHAR,
            dimensions_json VARCHAR,
            attributes_json VARCHAR,
            PRIMARY KEY (dataset_id, entity_id)
        )
    """,
    "observations": """
        CREATE TABLE IF NOT EXISTS observations (
            dataset_id VARCHAR,
            source_row_id VARCHAR,
            entity_id VARCHAR,
            series_id VARCHAR,
            ds DATE,
            measure VARCHAR,
            y_raw DECIMAL(20,2),
            observed BOOLEAN,
            PRIMARY KEY (dataset_id, source_row_id, ds, measure)
        )
    """,
    "preparations": """
        CREATE TABLE IF NOT EXISTS preparations (
            preparation_id VARCHAR PRIMARY KEY,
            dataset_id VARCHAR,
            policy_json VARCHAR,
            profile_json VARCHAR,
            created_at TIMESTAMP,
            fingerprint VARCHAR
        )
    """,
    "prepared_values": """
        CREATE TABLE IF NOT EXISTS prepared_values (
            preparation_id VARCHAR,
            series_id VARCHAR,
            ds DATE,
            entity_id VARCHAR,
            measure VARCHAR,
            y DOUBLE,
            observed BOOLEAN,
            was_adjusted BOOLEAN,
            adjustment_reason VARCHAR,
            PRIMARY KEY (preparation_id, series_id, ds)
        )
    """,
    "assumptions": """
        CREATE TABLE IF NOT EXISTS assumptions (
            dataset_id VARCHAR,
            assumption_id VARCHAR,
            kind VARCHAR,
            payload_json VARCHAR,
            updated_at TIMESTAMP,
            PRIMARY KEY (dataset_id, assumption_id)
        )
    """,
    "runs": """
        CREATE TABLE IF NOT EXISTS runs (
            run_id VARCHAR PRIMARY KEY,
            dataset_id VARCHAR,
            preparation_id VARCHAR,
            config_json VARCHAR,
            assumptions_snapshot_json VARCHAR,
            dependency_versions_json VARCHAR,
            config_hash VARCHAR,
            status VARCHAR,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            summary_json VARCHAR,
            study_name VARCHAR,
            saved_at TIMESTAMP
        )
    """,
    "run_nodes": """
        CREATE TABLE IF NOT EXISTS run_nodes (
            run_id VARCHAR,
            node_id VARCHAR,
            level VARCHAR,
            parent_node_id VARCHAR,
            entity_id VARCHAR,
            dimensions_json VARCHAR,
            coverage_json VARCHAR,
            PRIMARY KEY (run_id, node_id)
        )
    """,
    "forecasts": """
        CREATE TABLE IF NOT EXISTS forecasts (
            run_id VARCHAR,
            node_id VARCHAR,
            measure VARCHAR,
            scenario_id VARCHAR,
            ds DATE,
            yhat DOUBLE,
            lo80 DOUBLE,
            hi80 DOUBLE,
            model_alias VARCHAR,
            interval_method VARCHAR,
            status VARCHAR,
            PRIMARY KEY (run_id, node_id, measure, scenario_id, ds, model_alias)
        )
    """,
    "cv_results": """
        CREATE TABLE IF NOT EXISTS cv_results (
            run_id VARCHAR,
            node_id VARCHAR,
            measure VARCHAR,
            model_alias VARCHAR,
            cutoff DATE,
            ds DATE,
            y_actual DOUBLE,
            yhat DOUBLE,
            evaluated BOOLEAN,
            failure_reason VARCHAR,
            PRIMARY KEY (run_id, node_id, measure, model_alias, cutoff, ds)
        )
    """,
    "model_scores": """
        CREATE TABLE IF NOT EXISTS model_scores (
            run_id VARCHAR,
            node_id VARCHAR,
            measure VARCHAR,
            model_alias VARCHAR,
            mae DOUBLE,
            rmse DOUBLE,
            wape DOUBLE,
            bias DOUBLE,
            n_eval BIGINT,
            n_folds BIGINT,
            eligible BOOLEAN,
            selected BOOLEAN,
            selection_reason VARCHAR,
            fallback_used BOOLEAN,
            params_json VARCHAR,
            PRIMARY KEY (run_id, node_id, measure, model_alias)
        )
    """,
    "run_method_choices": """
        CREATE TABLE IF NOT EXISTS run_method_choices (
            run_id VARCHAR,
            scope_hash VARCHAR,
            scope_json VARCHAR,
            model_alias VARCHAR,
            rationale VARCHAR,
            updated_at TIMESTAMP,
            PRIMARY KEY (run_id, scope_hash)
        )
    """,
    "forecast_candidates_cache": """
        CREATE TABLE IF NOT EXISTS forecast_candidates_cache (
            run_id VARCHAR,
            node_id VARCHAR,
            measure VARCHAR,
            model_alias VARCHAR,
            status VARCHAR,
            payload_json VARCHAR,
            created_at TIMESTAMP,
            PRIMARY KEY (run_id, node_id, measure, model_alias)
        )
    """,
    "issues": """
        CREATE TABLE IF NOT EXISTS issues (
            issue_id VARCHAR,
            dataset_id VARCHAR,
            preparation_id VARCHAR,
            run_id VARCHAR,
            stage VARCHAR,
            code VARCHAR,
            severity VARCHAR,
            entity_id VARCHAR,
            source_row_id VARCHAR,
            details_json VARCHAR,
            created_at TIMESTAMP,
            PRIMARY KEY (issue_id)
        )
    """,
}


SCHEMA_VERSION_KEY = "schema_version"


SCHEMA_VERSION_VALUE = "2"

_FORECASTS_DDL = """
    CREATE TABLE forecasts (
        run_id VARCHAR,
        node_id VARCHAR,
        measure VARCHAR,
        scenario_id VARCHAR,
        ds DATE,
        yhat DOUBLE,
        lo80 DOUBLE,
        hi80 DOUBLE,
        model_alias VARCHAR,
        interval_method VARCHAR,
        status VARCHAR,
        PRIMARY KEY (run_id, node_id, measure, scenario_id, ds, model_alias)
    )
"""


def open_database(path: Path) -> duckdb.DuckDBPyConnection:
    """Abre conexao single-use; cada operacao usa conexao propria."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def initialize_database(conn) -> None:
    for sql in SCHEMA_TABLES.values():
        conn.execute(sql)
    # Estudo salvo (nome dado pelo usuário + data/hora do salvamento). Colunas
    # aditivas: bases criadas antes ganham as colunas sem migração de versão.
    conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS study_name VARCHAR")
    conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS saved_at TIMESTAMP")
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", [SCHEMA_VERSION_KEY]
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?)",
            [SCHEMA_VERSION_KEY, SCHEMA_VERSION_VALUE],
        )
    elif row[0] != SCHEMA_VERSION_VALUE:
        _migrate_schema(conn, row[0])
        conn.execute(
            "UPDATE app_meta SET value = ? WHERE key = ?",
            [SCHEMA_VERSION_VALUE, SCHEMA_VERSION_KEY],
        )


def _migrate_schema(conn, from_version: str) -> None:
    """Migra o schema DuckDB local para a versão atual (16/09/2026).

    v1 -> v2: `forecasts` ganha `model_alias` na chave primária — a mesma
    previsão pode existir para vários modelos (a aplicação não elege mais um
    vencedor; executa tudo o que o usuário escolheu). A tabela é recriada
    preservando as linhas existentes.
    """
    if from_version in ("1",):
        _recreate_forecasts(conn)


def _recreate_forecasts(conn) -> None:
    """Reconstrói `forecasts` com a nova PK, preservando as linhas atuais."""
    conn.execute("ALTER TABLE forecasts RENAME TO forecasts_old")
    conn.execute(_FORECASTS_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO forecasts (run_id, node_id, measure, scenario_id,"
        " ds, yhat, lo80, hi80, model_alias, interval_method, status)"
        " SELECT run_id, node_id, measure, scenario_id, ds, yhat, lo80, hi80,"
        " model_alias, interval_method, status FROM forecasts_old"
    )
    conn.execute("DROP TABLE IF EXISTS forecasts_old")


def _now_ts():
    return datetime.now()


@contextmanager
def _tx(conn):
    """Transacao que nao fecha a conexao ao sair (ao contrario de `with conn:`)."""
    conn.begin()
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def save_dataset(
    conn,
    study: StudyConfig,
    mapping: MappingConfig,
    data: CanonicalDataset,
    filename: str = "",
    file_sha256: str = "",
) -> str:
    dataset_id = str(uuid.uuid4())
    with _tx(conn):
        conn.execute(
            "INSERT INTO datasets (dataset_id, name, filename, file_sha256,"
            " created_at, study_json, mapping_json, fingerprint, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                dataset_id,
                study.name,
                filename,
                file_sha256 or str(uuid.uuid4()),
                _now_ts(),
                study.to_json(),
                mapping.to_json(),
                data.fingerprint,
                "ready",
            ],
        )
        if data.source_rows.height:
            src = data.source_rows.with_columns(
                pl.lit(dataset_id).alias("dataset_id")
            ).select("dataset_id", "source_row_id", "sheet", "row_number", "raw_json")
            conn.register("_tmp_source_rows", src)
            conn.execute(
                "INSERT INTO source_rows SELECT dataset_id, source_row_id, sheet,"
                " row_number, raw_json FROM _tmp_source_rows"
            )
            conn.unregister("_tmp_source_rows")
        if data.entities.height:
            ent = data.entities.with_columns(
                pl.lit(dataset_id).alias("dataset_id")
            ).select(
                "dataset_id",
                "entity_id",
                "key_json",
                "dimensions_json",
                "attributes_json",
            )
            conn.register("_tmp_entities", ent)
            conn.execute(
                "INSERT INTO entities SELECT dataset_id, entity_id, key_json,"
                " dimensions_json, attributes_json FROM _tmp_entities"
            )
            conn.unregister("_tmp_entities")
        if data.observations.height:
            obs = data.observations.with_columns(
                pl.lit(dataset_id).alias("dataset_id")
            ).select(
                "dataset_id",
                "source_row_id",
                "entity_id",
                "series_id",
                "ds",
                "measure",
                "y_raw",
                "observed",
            )
            conn.register("_tmp_observations", obs)
            conn.execute(
                "INSERT INTO observations SELECT dataset_id, source_row_id,"
                " entity_id, series_id, ds, measure, y_raw, observed"
                " FROM _tmp_observations"
            )
            conn.unregister("_tmp_observations")
    return dataset_id


def load_dataset(
    conn, dataset_id: str
) -> tuple[StudyConfig, MappingConfig, CanonicalDataset]:
    """Recarrega um estudo salvo (T3.6/B12): inverso de `save_dataset`."""
    row = conn.execute(
        "SELECT study_json, mapping_json, fingerprint FROM datasets"
        " WHERE dataset_id = ?",
        [dataset_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"Dataset {dataset_id} não encontrado")
    study_json, mapping_json, fingerprint = row
    study = StudyConfig.from_json(study_json)
    mapping = MappingConfig.from_json(mapping_json)

    entities = conn.execute(
        "SELECT entity_id, key_json, dimensions_json, attributes_json"
        " FROM entities WHERE dataset_id = ?",
        [dataset_id],
    ).pl()
    observations = conn.execute(
        "SELECT source_row_id, entity_id, series_id, ds, measure, y_raw, observed"
        " FROM observations WHERE dataset_id = ?",
        [dataset_id],
    ).pl()
    source_rows = conn.execute(
        "SELECT source_row_id, sheet, row_number, raw_json FROM source_rows"
        " WHERE dataset_id = ?",
        [dataset_id],
    ).pl()

    data = CanonicalDataset(observations, entities, source_rows, [], fingerprint)
    return study, mapping, data


def save_preparation(conn, dataset_id: str, prepared: PreparedDataset) -> str:
    # preparation_id e um hash do conteudo (B19): reenviar o mesmo arquivo com
    # a mesma politica gera o mesmo id de proposito, entao um id ja existente
    # significa "mesmo conteudo ja preparado antes" - reaproveita em vez de
    # violar a chave primaria (S2.4: descoberto ao reenviar N05A.xlsx 2x).
    existing = conn.execute(
        "SELECT 1 FROM preparations WHERE preparation_id = ?",
        [prepared.preparation_id],
    ).fetchone()
    if existing:
        return prepared.preparation_id
    with _tx(conn):
        conn.execute(
            "INSERT INTO preparations (preparation_id, dataset_id, policy_json,"
            " profile_json, created_at, fingerprint) VALUES (?, ?, ?, ?, ?, ?)",
            [
                prepared.preparation_id,
                dataset_id,
                prepared.policy.to_json(),
                "{}",
                _now_ts(),
                prepared.preparation_id,
            ],
        )
        if prepared.prepared.height:
            pv = prepared.prepared.with_columns(
                pl.lit(prepared.preparation_id).alias("preparation_id")
            ).select(
                "preparation_id",
                "series_id",
                "ds",
                "entity_id",
                "measure",
                "y",
                "observed",
                "was_adjusted",
                "adjustment_reason",
            )
            conn.register("_tmp_prepared_values", pv)
            conn.execute(
                "INSERT INTO prepared_values SELECT preparation_id, series_id,"
                " ds, entity_id, measure, y, observed, was_adjusted,"
                " adjustment_reason FROM _tmp_prepared_values"
            )
            conn.unregister("_tmp_prepared_values")
    return prepared.preparation_id


def create_run(
    conn,
    dataset_id: str,
    preparation_id: str,
    config: ForecastConfig,
    scenarios,
    regressors,
) -> str:
    run_id = str(uuid.uuid4())
    snapshot = {
        "scenarios": [s.to_json() for s in scenarios],
        "regressors": [r.to_json() for r in regressors],
    }
    with _tx(conn):
        conn.execute(
            "INSERT INTO runs (run_id, dataset_id, preparation_id, config_json,"
            " assumptions_snapshot_json, dependency_versions_json, config_hash, status,"
            " started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                dataset_id,
                preparation_id,
                config.to_json(),
                json.dumps(snapshot),
                "{}",
                str(uuid.uuid4()),
                "running",
                _now_ts(),
            ],
        )
    return run_id


def persist_batch(conn, run_id: str, batch_id: str, result: ForecastBatch) -> None:
    """Grava previsões/CV/scores em lote (`register` + `INSERT ... SELECT`).

    Mesmo padrão de `save_dataset`/`save_preparation`: evita 1 `conn.execute`
    por linha, que dominava o tempo de persistência em rodadas grandes (S1.3).
    """
    with _tx(conn):
        if result.predictions.height:
            df = result.predictions.with_columns(
                pl.lit(run_id).alias("run_id")
            ).select(
                "run_id",
                "node_id",
                "measure",
                "scenario_id",
                "ds",
                pl.col("yhat").cast(pl.Float64),
                pl.col("lo80").cast(pl.Float64),
                pl.col("hi80").cast(pl.Float64),
                "model_alias",
                "interval_method",
                "status",
            )
            conn.register("_tmp_forecasts", df)
            conn.execute(
                "INSERT OR REPLACE INTO forecasts SELECT run_id, node_id, measure,"
                " scenario_id, ds, yhat, lo80, hi80, model_alias, interval_method,"
                " status FROM _tmp_forecasts"
            )
            conn.unregister("_tmp_forecasts")

        if result.cv_predictions.height:
            df = result.cv_predictions.with_columns(
                pl.lit(run_id).alias("run_id")
            ).select(
                "run_id",
                "node_id",
                "measure",
                "model_alias",
                "cutoff",
                "ds",
                pl.col("y_actual").cast(pl.Float64),
                pl.col("yhat").cast(pl.Float64),
                pl.col("evaluated").fill_null(True).cast(pl.Boolean),
                pl.col("failure_reason").fill_null(""),
            )
            conn.register("_tmp_cv_results", df)
            conn.execute(
                "INSERT OR REPLACE INTO cv_results SELECT run_id, node_id, measure,"
                " model_alias, cutoff, ds, y_actual, yhat, evaluated, failure_reason"
                " FROM _tmp_cv_results"
            )
            conn.unregister("_tmp_cv_results")

        if result.scores.height:
            has_selection_cols = "selected" in result.scores.columns
            df = result.scores.with_columns(
                pl.lit(run_id).alias("run_id"),
                (
                    pl.col("selected").fill_null(False).cast(pl.Boolean)
                    if has_selection_cols
                    else pl.col("eligible").fill_null(False).cast(pl.Boolean)
                ).alias("selected"),
                (
                    pl.col("selection_reason").fill_null("usuario")
                    if has_selection_cols
                    else pl.lit("usuario")
                ).alias("selection_reason"),
                pl.lit(False).alias("fallback_used"),
                pl.lit("{}").alias("params_json"),
            ).select(
                "run_id",
                "node_id",
                "measure",
                "model_alias",
                pl.col("mae").cast(pl.Float64),
                pl.col("rmse").cast(pl.Float64),
                pl.col("wape").cast(pl.Float64),
                pl.col("bias").cast(pl.Float64),
                pl.col("n_eval").fill_null(0).cast(pl.Int64),
                pl.col("n_folds").fill_null(0).cast(pl.Int64),
                pl.col("eligible").fill_null(False).cast(pl.Boolean),
                "selected",
                "selection_reason",
                "fallback_used",
                "params_json",
            )
            conn.register("_tmp_model_scores", df)
            conn.execute(
                "INSERT OR REPLACE INTO model_scores SELECT run_id, node_id, measure,"
                " model_alias, mae, rmse, wape, bias, n_eval, n_folds, eligible,"
                " selected, selection_reason, fallback_used, params_json"
                " FROM _tmp_model_scores"
            )
            conn.unregister("_tmp_model_scores")


def persist_nodes(conn, run_id: str, nodes: pl.DataFrame) -> None:
    """Persiste os nós da rodada em `run_nodes` (sec 8.1).

    `coverage_json` guarda as entidades-folha sob cada nó; `level` usa o nome
    do nível (`folha` ou `level_name`). Escrita idempotente por (run, node).
    Mesmo padrão `register` + `INSERT ... SELECT` de `persist_batch` (S1.3).
    """
    if not nodes.height:
        return
    with _tx(conn):
        df = nodes.with_columns(pl.lit(run_id).alias("run_id")).select(
            "run_id",
            "node_id",
            pl.col("level").fill_null("folha"),
            "parent_node_id",
            "entity_id",
            pl.col("dimensions_json").fill_null("{}"),
            pl.col("coverage_json").fill_null("{}"),
        )
        conn.register("_tmp_run_nodes", df)
        conn.execute(
            "INSERT OR REPLACE INTO run_nodes SELECT run_id, node_id, level,"
            " parent_node_id, entity_id, dimensions_json, coverage_json"
            " FROM _tmp_run_nodes"
        )
        conn.unregister("_tmp_run_nodes")


def finish_run(conn, summary: RunSummary) -> None:
    with _tx(conn):
        conn.execute(
            "UPDATE runs SET status = ?, ended_at = ?, summary_json = ?"
            " WHERE run_id = ?",
            [
                summary.status.value,
                _now_ts(),
                json.dumps(summary.counts),
                summary.run_id,
            ],
        )
        if summary.warnings:
            for w in summary.warnings:
                conn.execute(
                    "INSERT INTO issues (issue_id, dataset_id, run_id, stage, code,"
                    " severity, details_json, created_at) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)",
                    [
                        str(uuid.uuid4()),
                        summary.run_id,
                        "run",
                        w.code,
                        w.severity.value,
                        json.dumps(w.details),
                        _now_ts(),
                    ],
                )


def load_assumptions(
    conn, dataset_id: str
) -> tuple[list[ScenarioConfig], list[RegressorSpec]]:
    """Restaura rascunhos de cenários/regressoras persistidos por save_assumptions."""
    rows = conn.execute(
        "SELECT assumption_id, kind, payload_json FROM assumptions"
        " WHERE dataset_id = ? ORDER BY updated_at",
        [dataset_id],
    ).fetchall()
    scenarios: list[ScenarioConfig] = []
    regressors: list[RegressorSpec] = []
    seen_scen: set[str] = set()
    seen_reg: set[str] = set()
    for _id, kind, payload in rows:
        if kind == "scenario":
            if _id in seen_scen:
                continue
            seen_scen.add(_id)
            scenarios.append(ScenarioConfig.from_json(payload))
        elif kind == "regressor":
            if _id in seen_reg:
                continue
            seen_reg.add(_id)
            regressors.append(RegressorSpec.from_json(payload))
    return scenarios, regressors


def save_assumptions(conn, dataset_id: str, scenarios: list, regressors: list) -> None:
    with _tx(conn):
        for s in scenarios:
            conn.execute(
                "INSERT OR REPLACE INTO assumptions (dataset_id, assumption_id,"
                " kind, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                [dataset_id, s.scenario_id, "scenario", s.to_json(), _now_ts()],
            )
        for r in regressors:
            conn.execute(
                "INSERT OR REPLACE INTO assumptions (dataset_id, assumption_id,"
                " kind, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                [dataset_id, r.regressor_id, "regressor", r.to_json(), _now_ts()],
            )


def list_datasets(conn) -> pl.DataFrame:
    return conn.execute(
        "SELECT dataset_id, name, status, study_json, created_at FROM datasets"
        " ORDER BY created_at DESC"
    ).pl()


def list_runs(conn, dataset_id: str) -> pl.DataFrame:
    return conn.execute(
        "SELECT run_id, status, started_at, ended_at FROM runs"
        " WHERE dataset_id = ? ORDER BY started_at DESC",
        [dataset_id],
    ).pl()


def mark_interrupted_runs(conn) -> int:
    """Marca runs 'running' órfãos como interrupted (sec 8.2)."""
    cur = conn.execute("SELECT run_id FROM runs WHERE status = 'running'").fetchall()
    n = 0
    with _tx(conn):
        for (rid,) in cur:
            conn.execute(
                "UPDATE runs SET status = 'interrupted', ended_at = ? WHERE run_id = ?",
                [_now_ts(), rid],
            )
            n += 1
    return n
