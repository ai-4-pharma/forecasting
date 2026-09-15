"""Forecast Community - camada de dados: templates, ingestao, qualidade,
persistencia DuckDB e exportacao.

Regra de arquitetura (secao 3.2): este modulo nao importa streamlit nem
forecast_engine. Usa polars, duckdb, openpyxl e numpy diretamente.
"""

from __future__ import annotations

import io
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import openpyxl
import polars as pl
import polars.selectors as cs

from contracts import (
    MAX_BATCH_SIZE,
    MAX_CANONICAL_OBSERVATIONS,
    MAX_ENTITIES,
    MAX_FILE_BYTES,
    MAX_PHYSICAL_ROWS,
    AssumptionRule,
    CanonicalDataset,
    CVWindow,
    DashboardData,
    DuplicateAction,
    EffectType,
    ExportArtifact,
    ExportConfig,
    ExportFormat,
    FileInspection,
    ForecastBatch,
    ForecastConfig,
    HierarchyConfig,
    HierarchyMode,
    Layout,
    MappingConfig,
    MatMode,
    Measure,
    MissingAction,
    NegativeAction,
    OutlierAction,
    PreparedDataset,
    ProfileReport,
    ResultFilter,
    RegressorSpec,
    RunStatus,
    RunSummary,
    ScenarioConfig,
    Severity,
    SourceFrequency,
    StudyConfig,
    TemporalView,
    TreatmentPolicy,
    ValidationIssue,
    empty_long,
    stable_id,
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
            summary_json VARCHAR
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
            PRIMARY KEY (run_id, node_id, measure, scenario_id, ds)
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
SCHEMA_VERSION_VALUE = "1"


def open_database(path: Path) -> duckdb.DuckDBPyConnection:
    """Abre conexao single-use; cada operacao usa conexao propria."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def initialize_database(conn) -> None:
    for sql in SCHEMA_TABLES.values():
        conn.execute(sql)
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", [SCHEMA_VERSION_KEY]
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?)",
            [SCHEMA_VERSION_KEY, SCHEMA_VERSION_VALUE],
        )


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


# ---------------------------------------------------------------------------
# Templates (P06)
# ---------------------------------------------------------------------------


def _shift_period(d: date, freq: SourceFrequency, k: int) -> date:
    year = d.year
    month = d.month
    if freq == SourceFrequency.MONTHLY or freq == SourceFrequency.MAT:
        total = year * 12 + (month - 1) + k
        return date(total // 12, total % 12 + 1, 1)
    if freq == SourceFrequency.QUARTERLY:
        q = (month - 1) // 3
        total = year * 4 + q + k
        return date(total // 4, (total % 4) * 3 + 1, 1)
    return date(year + k, 1, 1)


def _period_labels(study: StudyConfig, n: int) -> list[str]:
    """Rótulos de período no formato de origem (ex.: 2026-07)."""
    end = study.history_end or date(2026, 8, 1)
    out: list[str] = []
    for i in range(n):
        d = _shift_period(end, study.source_frequency, -i)
        out.append(_period_label(d, study.source_frequency))
    out.reverse()
    return out


def _period_label(d: date, freq: SourceFrequency) -> str:
    if freq == SourceFrequency.MONTHLY or freq == SourceFrequency.MAT:
        return f"{d.year:04d}-{d.month:02d}"
    if freq == SourceFrequency.QUARTERLY:
        return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"
    return f"{d.year:04d}"


def _parse_period(text: str, fmt: str, freq: SourceFrequency) -> date | None:
    """Converte rotulo de periodo em data do primeiro dia do periodo."""
    text = str(text).strip()
    if fmt == "YYYY-MM" or fmt is None:
        try:
            return date(int(text[:4]), int(text[5:7]), 1)
        except (ValueError, IndexError):
            return None
    if fmt == "YYYYMM":
        try:
            return date(int(text[:4]), int(text[4:6]), 1)
        except (ValueError, IndexError):
            return None
    if fmt == "YYYY-Qn":
        try:
            return date(int(text[:4]), (int(text[6]) - 1) * 3 + 1, 1)
        except (ValueError, IndexError):
            return None
    if fmt == "YYYY":
        try:
            return date(int(text), 1, 1)
        except ValueError:
            return None
    return None


def build_template(config: StudyConfig, file_format: str) -> ExportArtifact:
    """Gera template CSV ou XLSX com exemplos sinteticos e instrucoes."""
    freq = config.source_frequency
    n = 10
    dims = [d for d in config.dimension_names]
    periods = _period_labels(config, n)

    if config.layout == Layout.LONG:
        rows = []
        for idx, (d1, d2) in enumerate(
            [("Exemplo A", "Produto X"), ("Exemplo B", "Produto Y")]
        ):
            vals = {"classe": d1, "molecula": "", "marca": d2, "ean": ""}
            for j in range(n):
                row = {
                    d: (d1 if d == "classe" else d2 if d == "marca" else "")
                    for d in dims
                }
                row["periodo"] = periods[j]
                if "unidades" in config.measures:
                    row["unidades"] = f"{10 * (idx + 1) + j}"
                if "valor" in config.measures:
                    row["valor"] = f"{(10 * (idx + 1) + j) * 1.5:.2f}"
                rows.append(row)
        cols = (
            dims
            + ["periodo"]
            + ([c for c in ("unidades", "valor") if c in config.measures])
        )
        df = pl.DataFrame(rows, schema={c: pl.String for c in cols})
        header = ";".join(cols)
        lines = [header] + [";".join(str(r[c]) for c in cols) for r in rows]
        content = "\n".join(lines)
        meta = {
            "formato": "A-longo",
            "instrucoes": "Preencha uma linha por entidade/período/medida.",
            "periodos": periods,
        }
        if file_format == "xlsx":
            return _template_xlsx(config, df, "Template longo (Formato A)", periods)
        return ExportArtifact(
            f"template_{config.name}.csv",
            "text/csv",
            content.encode("utf-8-sig"),
            10,
            meta,
        )

    # Formato B - wide
    cols = dims + ["medida"] + periods
    rows = []
    for d1, d2 in [("Exemplo A", "Produto X"), ("Exemplo B", "Produto Y")]:
        for m in config.measures:
            row = {
                d: (d1 if d == "classe" else d2 if d == "marca" else "") for d in dims
            }
            row["medida"] = m
            for j in range(n):
                row[periods[j]] = (
                    f"{10 + j}" if m == "unidades" else f"{(10 + j) * 1.5:.2f}"
                )
            rows.append(row)
    df = pl.DataFrame(rows, schema={c: pl.String for c in cols})
    meta = {
        "formato": "B-largo",
        "instrucoes": "Se houver duas medidas, uma linha por entidade/medida."
        "O formato padrão 'Mês k' também é aceito.",
        "periodos": periods,
    }
    if file_format == "xlsx":
        return _template_xlsx(config, df, "Template largo (Formato B)", periods)
    lines = [";".join(cols)] + [";".join(str(r[c]) for c in cols) for r in rows]
    return ExportArtifact(
        f"template_{config.name}.csv",
        "text/csv",
        "\n".join(lines).encode("utf-8-sig"),
        len(rows),
        meta,
    )


def _template_xlsx(
    config: StudyConfig, df: pl.DataFrame, title: str, periods: list[str]
) -> ExportArtifact:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Template"
    header = list(df.columns)
    ws.append(
        [
            f"{title}. Frequência {config.source_frequency.value}. Períodos exemplo: "
            + " ".join(periods)
        ]
    )
    ws.append(header)
    for row in df.rows():
        ws.append([str(v) for v in row])
    buf = io.BytesIO()
    wb.save(buf)
    return ExportArtifact(
        f"template_{config.name}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        buf.getvalue(),
        df.height + 2,
        {"formato": "B-largo" if config.layout == Layout.WIDE else "A-longo"},
    )


# ---------------------------------------------------------------------------
# Inspecao de arquivo (P07)
# ---------------------------------------------------------------------------


def _detect_delimiter(sample: str) -> str:
    for cand in [";", ",", "\t"]:
        lines = [l for l in sample.splitlines() if l.strip()]
        if not lines:
            continue
        if cand in lines[0]:
            return cand
    return ";"


def _sniff_encoding(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            raw.decode(enc)
            return enc if enc != "utf-8" else "utf-8"
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _suggest_parse_date(text: str) -> date | None:
    """Parser mínimo de período para `suggest_time_settings` (T3.4/B14):
    aceita ISO completo, `YYYYMM`, `YYYY-MM`/`YYYY/MM` e `YYYY`."""
    c = str(text).strip()
    m = re.match(r"^(\d{4})-(\d{2})-\d{2}([ T].*)?$", c)
    if m:
        y, mth = int(m.group(1)), int(m.group(2))
        if 1 <= mth <= 12:
            return date(y, mth, 1)
    m = re.match(r"^(\d{4})(\d{2})(\.0)?$", c)
    if m:
        y, mth = int(m.group(1)), int(m.group(2))
        if 1 <= mth <= 12:
            return date(y, mth, 1)
    m = re.match(r"^(\d{4})[-/](\d{2})$", c)
    if m:
        y, mth = int(m.group(1)), int(m.group(2))
        if 1 <= mth <= 12:
            return date(y, mth, 1)
    if re.match(r"^\d{4}$", c):
        return date(int(c), 1, 1)
    return None


def suggest_time_settings(
    sample: pl.DataFrame, period_col: str | None, period_headers: list[str]
) -> dict:
    """Sugere último período fechado, quantidade de períodos e frequência
    (T3.4/B14), inferindo pelo menor espaçamento entre períodos distintos —
    substitui o default fixo `2026-08-01` da UI. `period_headers` (formato
    Largo) já vem com datas ISO (ex.: `auto_period_map` do app); `period_col`
    (formato Longo) é lido a partir da amostra de `inspect_file`.
    """
    dates: list[date] = []
    if period_headers:
        for h in period_headers:
            d = _suggest_parse_date(h)
            if d is not None:
                dates.append(d)
    elif period_col and period_col in sample.columns:
        for v in sample[period_col].drop_nulls().unique().to_list():
            d = _suggest_parse_date(str(v))
            if d is not None:
                dates.append(d)

    dates = sorted(set(dates))
    if len(dates) < 2:
        return {
            "history_end": dates[-1] if dates else None,
            "n_periods": len(dates),
            "frequency": None,
        }
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    min_gap = min(gaps)
    if min_gap <= 31:
        frequency = "monthly"
    elif min_gap <= 100:
        frequency = "quarterly"
    else:
        frequency = "yearly"
    return {"history_end": dates[-1], "n_periods": len(dates), "frequency": frequency}


def inspect_file(
    content: bytes, filename: str, options: MappingConfig
) -> FileInspection:
    """Inspeciona arquivo CSV/XLSX e devolve amostra e opcoes de leitura."""
    warnings: list[ValidationIssue] = []
    if len(content) > MAX_FILE_BYTES:
        warnings.append(
            ValidationIssue(
                code="E_FILE_TOO_BIG",
                severity=Severity.ERROR,
                message="Arquivo excede o limite de 100 MiB.",
            )
        )
    name = filename.lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return _inspect_xlsx(content, filename, options, warnings)
    if name.endswith(".xls"):
        warnings.append(
            ValidationIssue(
                code="E_XLSX_FORMULA_NOT_CACHED",
                severity=Severity.ERROR,
                message="Formato .xls antigo não é suportado; salve como .xlsx.",
            )
        )
        return FileInspection([], [], empty_long("col", 0), {}, warnings, {})
    if name.endswith(".csv") or name.endswith(".txt"):
        return _inspect_csv(content, filename, options, warnings)
    warnings.append(
        ValidationIssue(
            code="E_ROW_INVALID",
            severity=Severity.ERROR,
            message="Formato de arquivo não reconhecido. Use CSV ou XLSX.",
        )
    )
    return FileInspection([], [], empty_long("col", 0), {}, warnings, {})


def _inspect_csv(
    content: bytes,
    filename: str,
    options: MappingConfig,
    warnings: list[ValidationIssue],
) -> FileInspection:
    raw = content.decode(options.encoding or _sniff_encoding(content), errors="replace")
    del_ = options.delimiter or _detect_delimiter(raw)
    sample = "\n".join(raw.splitlines()[:100])
    try:
        df = pl.read_csv(
            io.StringIO(raw),
            separator=del_,
            n_rows=100,
            infer_schema_length=100,
            has_header=True,
            truncate_ragged_lines=True,
            ignore_errors=False,
        )
    except Exception as e:  # noqa: BLE001
        warnings.append(
            ValidationIssue(
                code="E_ROWS_INVALID",
                severity=Severity.ERROR,
                message=f"Falha ao ler CSV: {e}",
            )
        )
        return FileInspection(
            [], [], empty_long("col", 0), {}, warnings, {"delimiter": del_}
        )
    physical = len(raw.splitlines())
    if physical > MAX_PHYSICAL_ROWS:
        warnings.append(
            ValidationIssue(
                code="E_ROWS_LIMIT",
                severity=Severity.ERROR,
                message=f"CSV tem {physical} linhas físicas, acima do limite de "
                f"{MAX_PHYSICAL_ROWS}.",
            )
        )
    types = {c: str(df.schema[c]) for c in df.columns}
    return FileInspection(
        [],
        list(df.columns),
        df,
        types,
        warnings,
        {"delimiter": del_, "encoding": options.encoding, "n_lines_physical": physical},
    )


def _inspect_xlsx(
    content: bytes,
    filename: str,
    options: MappingConfig,
    warnings: list[ValidationIssue],
) -> FileInspection:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        warnings.append(
            ValidationIssue(
                code="E_ROW_INVALID",
                severity=Severity.ERROR,
                message=f"Falha ao abrir XLSX: {e}",
            )
        )
        return FileInspection([], [], empty_long("col", 0), {}, warnings, {})
    sheets = wb.sheetnames
    if options.sheet_name and options.sheet_name not in sheets:
        warnings.append(
            ValidationIssue(
                code="E_ROW_INVALID",
                severity=Severity.ERROR,
                message=f"Aba '{options.sheet_name}' não existe.",
            )
        )
    sheet = options.sheet_name or sheets[0]
    ws = wb[sheet]
    if ws.max_row is None or ws.max_column is None:
        warnings.append(
            ValidationIssue(
                code="E_ROWS_INVALID",
                severity=Severity.ERROR,
                message="Planilha vazia.",
            )
        )
        return FileInspection(sheets, [], empty_long("col", 0), {}, warnings, {})
    if ws.max_row > MAX_PHYSICAL_ROWS:
        warnings.append(
            ValidationIssue(
                code="E_ROWS_LIMIT",
                severity=Severity.ERROR,
                message=f"XLSX tem {ws.max_row} linhas na aba '{sheet}', acima do limite.",
            )
        )
    rows: list[list] = []
    for r in ws.iter_rows(
        min_row=1,
        max_row=min(101, ws.max_row),
        max_col=min(ws.max_column, 200),
        values_only=True,
    ):
        rows.append(list(r))
    if not rows:
        return FileInspection(sheets, [], empty_long("col", 0), {}, warnings, {})
    header = ["" if v is None else str(v) for v in rows[0]]
    pre_merged = bool(ws.merged_cells.ranges) if hasattr(ws, "merged_cells") else False
    if pre_merged:
        warnings.append(
            ValidationIssue(
                code="E_XLSX_MERGED_CELL",
                severity=Severity.ERROR,
                message="Células mescladas encontradas na tabela; separe-as antes de importar.",
            )
        )
    for row in rows[1:6]:
        for col_idx, v in enumerate(row[: len(header)]):
            if isinstance(v, str) and v.startswith("="):
                warnings.append(
                    ValidationIssue(
                        code="E_XLSX_FORMULA_NOT_CACHED",
                        severity=Severity.ERROR,
                        message="Há fórmulas sem valor calculado; o Excel recalcula e "
                        "o app não executa fórmulas.",
                        field_name=header[col_idx] if col_idx < len(header) else None,
                    )
                )
    seen: dict[str, int] = {}
    dedup_header: list[str] = []
    for h in header:
        seen[h] = seen.get(h, 0) + 1
        dedup_header.append(h if seen[h] == 1 else f"{h}_{seen[h]}")
    sam = pl.DataFrame(
        [row[: len(header)] for row in rows[1:6]],
        schema={name: pl.Utf8 for name in dedup_header},
        orient="row",
    )
    types = {}
    return FileInspection(
        sheets,
        header,
        sam,
        types,
        warnings,
        {"n_rows_declared": ws.max_row, "n_cols": ws.max_column, "sheet": sheet},
    )


# ---------------------------------------------------------------------------
# Normalizacao temporal e canonica (P08, P09)
# ---------------------------------------------------------------------------


def _read_dataframe(
    content: bytes, filename: str, mapping: MappingConfig, issues: list[ValidationIssue]
) -> pl.DataFrame:
    name = filename.lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = mapping.sheet_name or wb.sheetnames[0]
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            issues.append(
                ValidationIssue(
                    code="E_ROWS_INVALID",
                    severity=Severity.ERROR,
                    message="Planilha vazia.",
                )
            )
            return empty_long("col", 1)
        col_names = [("" if v is None else str(v)) for v in rows[0]]
        data_rows = rows[1:]
        return pl.DataFrame(
            data_rows, schema={c: pl.Utf8 for c in col_names}, orient="row"
        ).with_columns(cs.all().cast(pl.Utf8).fill_null(""))

    enc = mapping.encoding or _sniff_encoding(content)
    raw = content.decode(enc, errors="replace")
    del_ = mapping.delimiter or _detect_delimiter(raw)
    try:
        df = pl.read_csv(
            io.StringIO(raw),
            separator=del_,
            has_header=True,
            infer_schema_length=0,
            truncate_ragged_lines=True,
            ignore_errors=False,
        )
    except Exception as e:  # noqa: BLE001
        issues.append(
            ValidationIssue(
                code="E_ROWS_INVALID",
                severity=Severity.ERROR,
                message=f"Falha ao ler CSV: {e}",
            )
        )
        return empty_long("col", 1)
    # força string em todas as colunas para decisões de parser seguras
    return df.with_columns(cs.all().cast(pl.Utf8).fill_null(""))


def _parse_number(text: str, mapping: MappingConfig) -> float | None:
    s = str(text).strip()
    if s in ("", "-", "--", "n/a", "N/A"):
        return None
    if mapping.thousands_separator and mapping.thousands_separator in s:
        s = s.replace(mapping.thousands_separator, "")
    s = s.replace("R$", "").replace(" ", "").replace("\\xa0", "")
    if mapping.decimal_separator == ",":
        if "." in s and "," in s:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def entity_matrix_stable(mapping: MappingConfig, row: dict, key_cols: list[str]) -> str:
    """Hash SHA-256 estavel da chave (doc 5.2)."""
    pairs = [(c, str(row.get(c, "") or "")) for c in mapping.key_json_order or key_cols]
    return stable_id(*pairs)


def meet_key(mapping: MappingConfig, columns: list[str]) -> bool:
    missing = [c for c in mapping.key_columns if c not in columns]
    return not missing


def _columns_in(source: pl.DataFrame, names: list[str]) -> list[str]:
    return [n for n in names if n in source.columns]


def normalize_file(
    content: bytes, filename: str, study: StudyConfig, mapping: MappingConfig
) -> CanonicalDataset:
    """Normaliza CSV/XLSX para observacoes canonicas (sec 7.3)."""
    issues: list[ValidationIssue] = []
    df = _read_dataframe(content, filename, mapping, issues)
    if df.height == 0 or any(
        i.severity == Severity.ERROR for i in issues if i.code == "E_ROWS_INVALID"
    ):
        return CanonicalDataset(
            empty_long("source_row_id", 0),
            empty_long("entity_id", 0),
            empty_long("source_row_id", 0),
            issues,
            stable_id("empty"),
        )

    # conferir colunas essenciais
    missing_key = [c for c in mapping.key_columns if c not in df.columns]
    if missing_key:
        issues.append(
            ValidationIssue(
                code="E_KEY_MISSING",
                severity=Severity.ERROR,
                message=f"Colunas da chave ausentes: {missing_key}",
            )
        )
    if study.layout == Layout.LONG:
        if not mapping.period_column or mapping.period_column not in df.columns:
            issues.append(
                ValidationIssue(
                    code="E_PERIOD_COLUMN_MISSING",
                    severity=Severity.ERROR,
                    message="Coluna de período não mapeada.",
                )
            )
        for measure, col in mapping.measure_columns.items():
            if col not in df.columns:
                issues.append(
                    ValidationIssue(
                        code="E_MEASURE_MISSING",
                        severity=Severity.ERROR,
                        message=f"Coluna de medida '{col}' ausente.",
                    )
                )
    elif not mapping.wide_period_map:
        issues.append(
            ValidationIssue(
                code="E_WIDE_PERIOD_INCOMPLETE",
                severity=Severity.ERROR,
                message="Nenhum período mapeado para o formato largo.",
            )
        )
    if any(i.severity == Severity.ERROR for i in issues):
        return CanonicalDataset(
            empty_long("source_row_id", 0),
            empty_long("entity_id", 0),
            empty_long("source_row_id", 0),
            issues,
            stable_id("nok"),
        )

    rows = df.to_dicts()
    observed_rows: list[dict] = []
    entities: dict[str, dict] = {}
    obs_rows: list[dict] = []
    source_rows: list[dict] = []
    entity_keys: dict[str, tuple] = {}
    dim_pool: dict[str, set] = {}

    if study.layout == Layout.LONG:
        period_col = mapping.period_column
        for i, row in enumerate(rows):
            src_id = f"r{i}"
            source_rows.append(
                {
                    "dataset_id": "",  # preenchido no save
                    "source_row_id": src_id,
                    "sheet": mapping.sheet_name or filename,
                    "row_number": i + 2,
                    "raw_json": json.dumps(
                        {k: v for k, v in row.items()}, ensure_ascii=False
                    ),
                }
            )
            period_col = mapping.period_column or ""
            period_text = row.get(period_col, "")
            ds = parse_date_main(
                period_text, mapping.date_format, study.source_frequency
            )
            key_vals = tuple(row.get(c, "") or "" for c in mapping.key_columns)
            if not any(key_vals):
                issues.append(
                    ValidationIssue(
                        code="E_KEY_EMPTY",
                        severity=Severity.ERROR,
                        message="Linha sem valor na chave.",
                        source_row_ids=[src_id],
                    )
                )
                continue
            if ds is None:
                issues.append(
                    ValidationIssue(
                        code="E_DATE_AMBIGUOUS",
                        severity=Severity.ERROR,
                        message=f"Data não interpretada: '{period_text}'",
                        source_row_ids=[src_id],
                    )
                )
                continue
            entity_id = entity_matrix_stable(mapping, row, mapping.key_columns)
            dims: dict[str, str] = {}
            for logical, col in mapping.dimension_columns.items():
                dims[logical] = row.get(col, "") or ""
            attrs: dict[str, str] = {}
            for logical, col in mapping.attribute_columns.items():
                attrs[logical] = row.get(col, "") or ""
            _store_entity(
                entities,
                entity_keys,
                dim_pool,
                entity_id,
                key_vals,
                dims,
                attrs,
                src_id,
                issues,
                mapping,
                study,
            )
            for measure, col in mapping.measure_columns.items():
                num = _parse_number(row.get(col, ""), mapping)
                if num is None:
                    issues.append(
                        ValidationIssue(
                            code="E_ROW_INVALID",
                            severity=Severity.WARNING,
                            message="Valor de medida não numérico.",
                            source_row_ids=[src_id],
                        )
                    )
                    continue
                if measure == "unidades" and not float(num).is_integer():
                    issues.append(
                        ValidationIssue(
                            code="E_UNITS_FRACTION",
                            severity=Severity.ERROR,
                            message="Unidades devem ser inteiras.",
                            source_row_ids=[src_id],
                        )
                    )
                    continue
                series_id = stable_id(entity_id, measure)
                obs_rows.append(
                    {
                        "source_row_id": src_id,
                        "entity_id": entity_id,
                        "series_id": series_id,
                        "ds": ds,
                        "measure": measure,
                        "y_raw": num,
                        "observed": True,
                    }
                )
    else:
        # formato largo
        period_map = mapping.wide_period_map  # origem -> ISO
        measures = mapping.measure_columns
        for i, row in enumerate(rows):
            src_id = f"r{i}"
            source_rows.append(
                {
                    "dataset_id": "",
                    "source_row_id": src_id,
                    "sheet": mapping.sheet_name or filename,
                    "row_number": i + 2,
                    "raw_json": json.dumps(dict(row), ensure_ascii=False),
                }
            )
            key_vals = tuple(row.get(c, "") or "" for c in mapping.key_columns)
            if not any(key_vals):
                issues.append(
                    ValidationIssue(
                        code="E_KEY_EMPTY",
                        severity=Severity.ERROR,
                        message="Linha sem chave.",
                        source_row_ids=[src_id],
                    )
                )
                continue
            entity_id = entity_matrix_stable(mapping, row, mapping.key_columns)
            dims = {
                logical: row.get(col, "") or ""
                for logical, col in mapping.dimension_columns.items()
            }
            attrs = {
                logical: row.get(col, "") or ""
                for logical, col in mapping.attribute_columns.items()
            }
            _store_entity(
                entities,
                entity_keys,
                dim_pool,
                entity_id,
                key_vals,
                dims,
                attrs,
                src_id,
                issues,
                mapping,
                study,
            )

            # medida da linha: rótulo ou medida única (sem coluna de medida)
            line_measure: str | None = None
            if (
                mapping.measure_label_column
                and mapping.measure_label_column in df.columns
            ):
                raw_m = (row.get(mapping.measure_label_column) or "").strip().lower()
                valid = {
                    "unidades": "unidades",
                    "unidades": "unidades",
                    "un": "unidades",
                    "unidade": "unidades",
                    "valor": "valor",
                    "venda": "valor",
                    "value": "valor",
                    "val": "valor",
                }
                line_measure = valid.get(raw_m, None)
                if line_measure is None:
                    issues.append(
                        ValidationIssue(
                            code="E_MEASURE_UNKNOWN",
                            severity=Severity.ERROR,
                            message=f"Rótulo de medida indefinido: {raw_m}",
                            source_row_ids=[src_id],
                        )
                    )
                    continue
            elif len(mapping.measure_columns) == 1:
                line_measure = next(iter(mapping.measure_columns))
            else:
                issues.append(
                    ValidationIssue(
                        code="E_MEASURE_UNKNOWN",
                        severity=Severity.ERROR,
                        message="Formato largo com 2 medidas exige "
                        "coluna de rótulo da medida.",
                        source_row_ids=[src_id],
                    )
                )
                continue

            for src_col, ds_iso in period_map.items():
                ds = date.fromisoformat(ds_iso)
                raw_v = row.get(src_col, "")
                num = _parse_number(raw_v, mapping) if raw_v not in (None, "") else None
                if num is None:
                    issues.append(
                        ValidationIssue(
                            code="E_ROW_INVALID",
                            severity=Severity.WARNING,
                            message="Valor não numérico no período.",
                            source_row_ids=[src_id],
                        )
                    )
                    continue
                if line_measure == "unidades" and not float(num).is_integer():
                    issues.append(
                        ValidationIssue(
                            code="E_UNITS_FRACTION",
                            severity=Severity.ERROR,
                            message="Unidades devem ser inteiras.",
                            source_row_ids=[src_id],
                        )
                    )
                    continue
                series_id = stable_id(entity_id, line_measure)
                obs_rows.append(
                    {
                        "source_row_id": src_id,
                        "entity_id": entity_id,
                        "series_id": series_id,
                        "ds": ds,
                        "measure": line_measure,
                        "y_raw": float(num),
                        "observed": True,
                    }
                )

    if len(entities) > MAX_ENTITIES:
        issues.append(
            ValidationIssue(
                code="E_ENTITY_LIMIT",
                severity=Severity.ERROR,
                message=f"{len(entities)} entidades, acima do limite "
                f"de {MAX_ENTITIES}.",
            )
        )
    if len(obs_rows) > MAX_CANONICAL_OBSERVATIONS:
        issues.append(
            ValidationIssue(
                code="E_OBS_LIMIT",
                severity=Severity.ERROR,
                message=f"{len(obs_rows)} observações, acima do limite "
                f"de {MAX_CANONICAL_OBSERVATIONS}.",
            )
        )

    obs_df = pl.DataFrame(
        obs_rows
        or [
            {
                "source_row_id": "x",
                "entity_id": "x",
                "series_id": "x",
                "ds": date(2020, 1, 1),
                "measure": "unidades",
                "y_raw": 0.0,
                "observed": True,
            }
        ]
    )
    obs_df = (
        obs_df.with_columns(
            pl.col("y_raw")
            .cast(pl.Float64)
            .map_elements(lambda v: round(v, 2), return_dtype=pl.Float64)
            .alias("y_raw_dec")
        )
        .drop("y_raw")
        .rename({"y_raw_dec": "y_raw"})
        .with_columns(pl.col("y_raw").cast(pl.Decimal(20, 2)))
    )
    ent_df = pl.DataFrame(
        [
            {
                "entity_id": eid,
                "key_json": json.dumps(dict(zip(mapping.key_columns, keys))),
                "dimensions_json": json.dumps(dims, ensure_ascii=False),
                "attributes_json": json.dumps(attrs, ensure_ascii=False),
            }
            for eid, (keys, dims, attrs) in entities.items()
        ]
    )
    src_df = pl.DataFrame(
        [
            {k: (v if not isinstance(v, dict) else json.dumps(v)) for k, v in r.items()}
            for r in source_rows
        ]
    )
    content_digest = _observations_content_digest(obs_df)
    fingerprint = stable_id(
        filename,
        str(len(obs_rows)),
        str(len(entities)),
        mapping.to_json(),
        study.to_json(),
        content_digest,
    )
    return CanonicalDataset(obs_df, ent_df, src_df, issues, fingerprint)


def _observations_content_digest(obs_df: pl.DataFrame) -> str:
    """Resumo do conteúdo real das observações (B19): o `fingerprint` do
    dataset não pode depender só da forma (nome/contagens/mapeamento), senão
    dois arquivos homônimos com dados diferentes colidem em `preparation_id`.
    """
    if obs_df.height == 0:
        return stable_id("empty_obs")
    ordered = obs_df.select("series_id", "ds", "y_raw").sort(["series_id", "ds"])
    values = [
        f"{r['series_id']}|{r['ds']}|{r['y_raw']}" for r in ordered.to_dicts()
    ]
    return stable_id(*values)


def _store_entity(
    entities: dict,
    entity_keys: dict,
    dim_pool: dict,
    entity_id: str,
    key_vals: tuple,
    dims: dict,
    attrs: dict,
    src_id: str,
    issues: list[ValidationIssue],
    mapping: MappingConfig,
    study: StudyConfig,
) -> None:
    if entity_id in entities:
        prev_dims = entities[entity_id][1]
        for k, v in dims.items():
            if prev_dims.get(k, "") != v and prev_dims.get(k, ""):
                issues.append(
                    ValidationIssue(
                        code="E_DIMENSION_CONFLICT",
                        severity=Severity.ERROR,
                        message=f"Dimensão '{k}' muda entre linhas "
                        f"'{prev_dims.get(k)}' vs '{v}'.",
                        source_row_ids=[src_id],
                        entity_ids=[entity_id],
                    )
                )
    else:
        entities[entity_id] = (key_vals, dict(dims), dict(attrs))


# ---------------------------------------------------------------------------
# Datas e grid temporal (P08)
# ---------------------------------------------------------------------------


def parse_date_main(text: str, fmt: str, freq: SourceFrequency) -> date | None:
    return _parse_period(text, fmt, freq)


def build_time_grid(study: StudyConfig) -> list[date]:
    """Grade temporal de períodos completos até history_end."""
    n = study.history_periods
    end = study.history_end or date(2026, 8, 1)
    grid: list[date] = []
    for k in range(n, 0, -1):
        grid.append(_shift_period(end, study.source_frequency, -(k - 1)))
    return grid


def aggregate_monthly_to(df: pl.DataFrame, freq: SourceFrequency) -> pl.DataFrame:
    """Agrega mensal para trimestral/anual por soma de períodos completos."""
    if freq == SourceFrequency.MONTHLY:
        return df
    if freq == SourceFrequency.QUARTERLY:
        # início do trimestre calendário: (year, month//3+1)
        q = (pl.col("ds").dt.year() * 4 + (pl.col("ds").dt.month() - 1) // 3).alias(
            "qk"
        )
        agg = (
            df.with_columns(q)
            .group_by(["unique_id", "qk"])
            .agg(pl.col("y").sum())
            .with_columns(
                (pl.col("qk") // 4).alias("yy"),
                (((pl.col("qk") % 4) * 3) + 1).alias("mm"),
            )
            .with_columns(pl.date(pl.col("yy"), pl.col("mm"), 1).alias("ds"))
            .drop(["qk", "mm", "yy"])
            .sort(["unique_id", "ds"])
        )
        return agg
    agg = (
        df.with_columns(pl.col("ds").dt.year().alias("yy"))
        .group_by(["unique_id", "yy"])
        .agg(pl.col("y").sum())
        .with_columns(pl.date(pl.col("yy"), 1, 1).alias("ds"))
        .drop("yy")
        .sort(["unique_id", "ds"])
    )
    return agg


# ---------------------------------------------------------------------------
# Profiling e preparacao (P11, P12)
# ---------------------------------------------------------------------------


def profile_data(data: CanonicalDataset, study: StudyConfig) -> ProfileReport:
    """Diagnostico por série (sec 6)."""
    obs = data.observations
    if obs.height == 0:
        return ProfileReport(
            {"n_series": 0, "blocking_errors": 0},
            empty_long("series_id", 0),
            data.issues,
            False,
            {},
        )
    byte_pairs = (
        obs.with_columns(pl.col("y_raw").cast(pl.Float64).alias("y_num"))
        .group_by(["series_id", "measure", "entity_id"])
        .agg(
            [
                pl.col("ds").min().alias("inicio"),
                pl.col("ds").max().alias("fim"),
                pl.col("ds").count().alias("n_periodos"),
                pl.col("y_num").count().alias("n_observados"),
                pl.count().alias("n_linhas"),
                (pl.col("y_num").is_null()).sum().alias("n_nulos"),
                (pl.col("y_num") < 0).sum().alias("n_negativos"),
                (pl.col("y_num") == 0).sum().alias("n_zeros"),
                (pl.col("y_num") > 0).count().alias("n_positivos"),
                pl.col("y_num").std().alias("std"),
                pl.col("y_num").mean().alias("mean"),
            ]
        )
        .sort("series_id")
    )
    # ADI / CV2
    by = byte_pairs.with_columns(
        (pl.col("n_periodos") / pl.col("n_positivos").clip(lower_bound=1)).alias("adi"),
        (pl.col("std") / pl.col("mean")).alias("cv"),
    ).with_columns(
        pl.when(pl.col("n_negativos") == 0)
        .then((pl.col("cv") ** 2).fill_null(0.0))
        .otherwise(pl.lit(0.0))
        .alias("cv2"),
    )
    blocking_errors = sum(
        1
        for i in data.issues
        if i.severity == Severity.ERROR
        and i.code
        in {
            "E_DUPLICATE",
            "E_UNITS_FRACTION",
            "E_DIMENSION_CONFLICT",
            "E_KEY_EMPTY",
            "E_DATE_AMBIGUOUS",
        }
    )
    blocked = any(i.severity == Severity.ERROR for i in data.issues)
    return ProfileReport(
        {
            "n_series": len(by),
            "blocking_errors": blocking_errors,
            "coverage_obs": obs.height,
        },
        by,
        data.issues,
        blocked,
        {},
    )


def prepare_data(
    data: CanonicalDataset,
    study: StudyConfig,
    policy: TreatmentPolicy,
    cutoff: date | None = None,
) -> PreparedDataset:
    """Prepara dados com políticas (sec 6). Preserva raw e máscara de observado."""
    base = data.observations
    if cutoff is not None:
        base = base.filter(pl.col("ds") <= cutoff)

    # duplicidades por série/ds
    dups = base.group_by(["series_id", "ds", "measure", "entity_id"]).agg(
        pl.len().alias("n"), pl.col("y_raw").flatten()
    )
    if policy.duplicate_action == DuplicateAction.SUM:
        dedup = base.group_by(
            ["series_id", "ds", "measure", "entity_id", "source_row_id"]
        ).agg(pl.col("y_raw").sum())
        # unifica por série/ds somando
        dedup = dedup.group_by(["series_id", "ds", "measure", "entity_id"]).agg(
            pl.col("y_raw").sum()
        )
    else:
        dedup = base.unique(
            subset=["series_id", "ds", "measure", "entity_id"], keep="first"
        ).sort(["series_id", "ds"])

    dedup = dedup.with_columns(pl.col("y_raw").cast(pl.Float64).alias("y"))

    grid = build_time_grid(study)
    grid_dates = set(grid)

    # preenchimento de lacunas por série
    out_rows: list[dict] = []
    for (series_id, measure, entity_id), grp in dedup.group_by(
        ["series_id", "measure", "entity_id"]
    ):
        present = set(grp["ds"].to_list())
        vals = {d: v for d, v in zip(grp["ds"].to_list(), grp["y"].to_list())}
        series_dates = [
            d for d in grid_dates if d >= min(present, default=date(2020, 1, 1))
        ]
        if policy.missing_action == MissingAction.EXCLUDE_SERIES:
            series_dates = sorted(present)
            for d in series_dates:
                out_rows.append(
                    {
                        "series_id": series_id,
                        "ds": d,
                        "measure": measure,
                        "entity_id": entity_id,
                        "y": vals[d],
                        "observed": True,
                        "was_adjusted": False,
                        "adjustment_reason": "",
                    }
                )
        elif policy.missing_action == MissingAction.FFILL:
            prev = None
            for d in series_dates:
                if d in vals:
                    prev = vals[d]
                    out_rows.append(
                        {
                            "series_id": series_id,
                            "ds": d,
                            "measure": measure,
                            "entity_id": entity_id,
                            "y": vals[d],
                            "observed": True,
                            "was_adjusted": False,
                            "adjustment_reason": "",
                        }
                    )
                elif prev is not None:
                    out_rows.append(
                        {
                            "series_id": series_id,
                            "ds": d,
                            "measure": measure,
                            "entity_id": entity_id,
                            "y": prev,
                            "observed": False,
                            "was_adjusted": True,
                            "adjustment_reason": "ffill",
                        }
                    )
                else:
                    continue
        elif policy.missing_action == MissingAction.ZERO:
            for d in series_dates:
                if d in vals:
                    out_rows.append(
                        {
                            "series_id": series_id,
                            "ds": d,
                            "measure": measure,
                            "entity_id": entity_id,
                            "y": vals[d],
                            "observed": True,
                            "was_adjusted": False,
                            "adjustment_reason": "",
                        }
                    )
                else:
                    out_rows.append(
                        {
                            "series_id": series_id,
                            "ds": d,
                            "measure": measure,
                            "entity_id": entity_id,
                            "y": 0.0,
                            "observed": False,
                            "was_adjusted": True,
                            "adjustment_reason": "zero_preenchido",
                        }
                    )
        else:
            # exclude_series com séries completas mantidas (fallback para valores
            # ainda não mapeados em política conhecida)
            for d in sorted(present):
                out_rows.append(
                    {
                        "series_id": series_id,
                        "ds": d,
                        "measure": measure,
                        "entity_id": entity_id,
                        "y": vals[d],
                        "observed": True,
                        "was_adjusted": False,
                        "adjustment_reason": "",
                    }
                )

    # negaivos / outliers / winsorização
    prepped = pl.DataFrame(out_rows).with_columns(
        pl.col("y").cast(pl.Float64),
        pl.when(pl.col("y").is_null())
        .then(pl.lit(False))
        .otherwise(pl.col("observed"))
        .alias("observed"),
    )
    neg_mask = prepped["y"] < 0
    if policy.negative_action == NegativeAction.REJECT and neg_mask.any():
        # manter raw separado; sinalizar issue e tirar da preparação elegível
        prepped = prepped.filter(~neg_mask)

    if policy.outlier_action == OutlierAction.WINSORIZE:
        prepped = _winsorize(prepped, policy.upper_quantile)

    eligible = prepped.group_by("series_id").agg(
        pl.col("y").count().alias("n"), pl.col("y").null_count().alias("nulos")
    )
    eligible_ids = eligible.filter(pl.col("n") > 0, pl.col("nulos") == 0)[
        "series_id"
    ].to_list()
    excluded = sorted(set(prepped["series_id"].unique().to_list()) - set(eligible_ids))
    prep_id = stable_id(data.fingerprint, policy.to_json(), str(cutoff))
    adj = prepped.filter(pl.col("was_adjusted"))[
        ["series_id", "ds", "adjustment_reason"]
    ]
    return PreparedDataset(
        prepped, dedup, adj, eligible_ids, excluded, prep_id, policy, cutoff
    )


def _winsorize(df: pl.DataFrame, q: float) -> pl.DataFrame:
    def lim(g: pl.DataFrame) -> pl.DataFrame:
        v = g["y"].to_numpy()
        if len(v) == 0:
            return g
        thr = float(np.quantile(v, q))
        cap = (
            g.with_columns(
                pl.when(pl.col("y") > thr)
                .then(pl.lit(thr))
                .otherwise(pl.col("y"))
                .alias("y")
            )
            .with_columns(
                pl.when(pl.col("was_adjusted") == False)
                .then(pl.lit(True))
                .otherwise(pl.col("was_adjusted"))
                .alias("was_adjusted")
            )
            .with_columns(
                pl.when((pl.col("adjustment_reason") == "") & (pl.col("y") > 0))
                .then(pl.lit("winsorizado"))
                .otherwise(pl.col("adjustment_reason"))
                .alias("adjustment_reason")
            )
        )
        return cap

    return df.group_by("series_id").map_groups(lim)


# ---------------------------------------------------------------------------
# Banco e ciclo de vida (P05)
# ---------------------------------------------------------------------------


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
                "dataset_id", "entity_id", "key_json", "dimensions_json",
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
                "dataset_id", "source_row_id", "entity_id", "series_id", "ds",
                "measure", "y_raw", "observed",
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
                "preparation_id", "series_id", "ds", "entity_id", "measure", "y",
                "observed", "was_adjusted", "adjustment_reason",
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
    with _tx(conn):
        for r in result.predictions.to_dicts():
            conn.execute(
                "INSERT OR REPLACE INTO forecasts (run_id, node_id, measure,"
                " scenario_id, ds, yhat, lo80, hi80, model_alias, interval_method,"
                " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    r["node_id"],
                    r["measure"],
                    r["scenario_id"],
                    r["ds"],
                    float(r["yhat"]),
                    float(r["lo80"]) if r.get("lo80") is not None else None,
                    float(r["hi80"]) if r.get("hi80") is not None else None,
                    r["model_alias"],
                    r["interval_method"],
                    r["status"],
                ],
            )
        for r in result.cv_predictions.to_dicts():
            conn.execute(
                "INSERT OR REPLACE INTO cv_results (run_id, node_id, measure,"
                " model_alias, cutoff, ds, y_actual, yhat, evaluated, failure_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    r["node_id"],
                    r["measure"],
                    r["model_alias"],
                    r["cutoff"],
                    r["ds"],
                    float(r["y_actual"]) if r.get("y_actual") is not None else None,
                    float(r["yhat"]) if r.get("yhat") is not None else None,
                    bool(r.get("evaluated", True)),
                    r.get("failure_reason"),
                ],
            )
        for r in result.scores.to_dicts():
            conn.execute(
                "INSERT OR REPLACE INTO model_scores (run_id, node_id, measure,"
                " model_alias, mae, rmse, wape, bias, n_eval, n_folds, eligible,"
                " selected, selection_reason, fallback_used, params_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    r["node_id"],
                    r["measure"],
                    r["model_alias"],
                    float(r["mae"]) if r.get("mae") is not None else None,
                    float(r["rmse"]) if r.get("rmse") is not None else None,
                    float(r["wape"]) if r.get("wape") is not None else None,
                    float(r["bias"]) if r.get("bias") is not None else None,
                    int(r.get("n_eval", 0)),
                    int(r.get("n_folds", 0)),
                    bool(r.get("eligible", True)),
                    False,
                    "",
                    False,
                    "{}",
                ],
            )


def persist_nodes(conn, run_id: str, nodes: pl.DataFrame) -> None:
    """Persiste os nós da rodada em `run_nodes` (sec 8.1).

    `coverage_json` guarda as entidades-folha sob cada nó; `level` usa o nome
    do nível (`folha` ou `level_name`). Escrita idempotente por (run, node).
    """
    with _tx(conn):
        for r in nodes.to_dicts():
            conn.execute(
                "INSERT OR REPLACE INTO run_nodes (run_id, node_id, level,"
                " parent_node_id, entity_id, dimensions_json, coverage_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    r["node_id"],
                    r.get("level", "folha"),
                    r.get("parent_node_id"),
                    r.get("entity_id"),
                    r.get("dimensions_json", "{}"),
                    r.get("coverage_json", "{}"),
                ],
            )


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


def load_run_inputs(conn, run_id: str) -> dict:
    row = conn.execute(
        "SELECT dataset_id, preparation_id, config_json,"
        " assumptions_snapshot_json FROM runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"Run {run_id} não encontrado")
    dataset_id, prep_id, config_json, snapshot = row
    study_row = conn.execute(
        "SELECT study_json, mapping_json FROM datasets WHERE dataset_id = ?",
        [dataset_id],
    ).fetchone()
    study = StudyConfig.from_json(study_row[0])
    mapping = MappingConfig.from_json(study_row[1])
    prep = conn.execute(
        "SELECT policy_json, fingerprint FROM preparations WHERE preparation_id = ?",
        [prep_id],
    ).fetchone()
    prepped = conn.execute(
        "SELECT * FROM prepared_values WHERE preparation_id = ?", [prep_id]
    ).pl()
    snap = json.loads(snapshot or "{}")
    scenarios = [ScenarioConfig.from_json(s) for s in snap.get("scenarios", []) if s]
    regressors = [RegressorSpec.from_json(r) for r in snap.get("regressors", []) if r]
    policy_json = prep[0] if prep else "{}"
    return {
        "run_id": run_id,
        "dataset_id": dataset_id,
        "preparation_id": prep_id,
        "study": study,
        "mapping": mapping,
        "policy": (
            TreatmentPolicy.from_json(policy_json)
            if policy_json and policy_json != "{}"
            else TreatmentPolicy()
        ),
        "config": ForecastConfig.from_json(config_json),
        "scenarios": scenarios,
        "regressors": regressors,
        "prepared": prepped,
    }


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


def _query_predictions(conn, run_id: str, filters: ResultFilter) -> pl.DataFrame:
    """Previsões filtradas por cenário/medida/nível/nó, faixa de datas,
    busca textual e dimensões reais das entidades (sec 12.1).

    O nível pertence a `run_nodes`, não a `forecasts`; a junção traz `level` e
    `entity_id` para o frame seguir `SCHEMA_PREDICTIONS` e permite filtrar por
    nível sem inferir a hierarquia pela UI.
    """
    q = (
        "SELECT f.node_id, n.entity_id, n.level, f.measure, f.scenario_id, f.ds,"
        " f.yhat, f.lo80, f.hi80, f.model_alias, f.interval_method, f.status"
        " FROM forecasts f LEFT JOIN run_nodes n"
        " ON n.run_id = f.run_id AND n.node_id = f.node_id WHERE f.run_id = ?"
    )
    params: list = [run_id]
    if filters.scenario_id:
        q += " AND f.scenario_id = ?"
        params.append(filters.scenario_id)
    if filters.measure:
        q += " AND f.measure = ?"
        params.append(filters.measure)
    if filters.node_level:
        q += " AND n.level = ?"
        params.append(filters.node_level)
    if filters.node_id:
        q += " AND f.node_id = ?"
        params.append(filters.node_id)
    if filters.ds_start:
        q += " AND f.ds >= ?"
        params.append(filters.ds_start)
    if filters.ds_end:
        q += " AND f.ds <= ?"
        params.append(filters.ds_end)
    if filters.search:
        q += " AND lower(f.node_id) LIKE ?"
        params.append(f"%{str(filters.search).lower()}%")
    if filters.dimensions:
        matched = _dim_filtered_node_ids(conn, run_id, filters.dimensions)
        if not matched:
            return pl.DataFrame()
        q += f" AND f.node_id IN ({','.join('?' * len(matched))})"
        params += matched
    return conn.execute(q, params).pl()


def _dim_filtered_node_ids(
    conn, run_id: str, dimensions: dict[str, list[str]]
) -> list[str]:
    """Nós de folha cuja entidade corresponde à combinação AND/OR de dimensões.

    Segue a regra da sec 11: nada de regra/visão parcial sobre agregado —
    retorna apenas nós com `entity_id` real (nível folha), evitando somar
    pais/filhos na mesma view.
    """
    ds = conn.execute(
        "SELECT dataset_id FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    if ds is None:
        return []
    ents = conn.execute(
        "SELECT entity_id, dimensions_json FROM entities WHERE dataset_id = ?",
        [ds[0]],
    ).fetchall()
    keep: set[str] = set()
    for eid, dims_json in ents:
        try:
            dims = json.loads(dims_json or "{}")
        except Exception:  # noqa: BLE001
            dims = {}
        ok = True
        for d, vals in dimensions.items():
            actual = str(dims.get(d, "")).lower()
            allowed = {str(v).lower() for v in vals}
            if actual not in allowed:
                ok = False
                break
        if ok:
            keep.add(eid)
    if not keep:
        return []
    out: list[str] = []
    for row in conn.execute(
        "SELECT node_id, entity_id, coverage_json FROM run_nodes WHERE run_id = ?",
        [run_id],
    ).fetchall():
        nid, eid, coverage_json = row
        covered: set[str] = set()
        if eid:
            covered.add(eid)
        if coverage_json:
            try:
                covered.update(json.loads(coverage_json).get("entity_ids", []))
            except Exception:  # noqa: BLE001
                pass
        if covered & keep:
            out.append(nid)
    return out


def _query_nodes(conn, run_id: str) -> pl.DataFrame:
    return conn.execute(
        "SELECT node_id, level, parent_node_id, entity_id, dimensions_json,"
        " coverage_json FROM run_nodes WHERE run_id = ?",
        [run_id],
    ).pl()


def _query_scores(
    conn, run_id: str, node_ids: list[str], measure: str | None
) -> pl.DataFrame:
    q = (
        "SELECT node_id, measure, model_alias, mae, rmse, wape, bias, n_eval,"
        " n_folds, eligible, selected, selection_reason, fallback_used FROM"
        " model_scores WHERE run_id = ?"
    )
    params: list = [run_id]
    if measure:
        q += " AND measure = ?"
        params.append(measure)
    if node_ids:
        q += f" AND node_id IN ({','.join('?' * len(node_ids))})"
        params.extend(node_ids)
    return conn.execute(q, params).pl()


def _query_issues(conn, run_id: str) -> list[ValidationIssue]:
    rows = conn.execute(
        "SELECT code, severity, details_json, entity_id FROM issues WHERE run_id = ?",
        [run_id],
    ).fetchall()
    out: list[ValidationIssue] = []
    for code, severity, details_json, entity_id in rows:
        try:
            details = json.loads(details_json or "{}")
        except Exception:  # noqa: BLE001
            details = {}
        out.append(
            ValidationIssue(
                code=code,
                severity=Severity(severity),
                message=code,
                entity_ids=[entity_id] if entity_id else [],
                details=details,
            )
        )
    return out


def load_run_study(conn, run_id: str) -> StudyConfig:
    """StudyConfig da roda, para a UI decidir visão MAT/canônica (sec 5.3)."""
    row = conn.execute(
        "SELECT d.study_json FROM runs r JOIN datasets d"
        " ON d.dataset_id = r.dataset_id WHERE r.run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"Run {run_id} não encontrado")
    return StudyConfig.from_json(row[0])


def list_run_nodes(conn, run_id: str) -> pl.DataFrame:
    """Nós da rodada para os filtros do dashboard (níveis e séries, sec 12.1).

    Retorna nível, nó e entidades-folha cobertas — nunca os pais somados —
    para que a UI ofereça opções únicas por nível sem misturar hierarquias.
    """
    return conn.execute(
        "SELECT node_id, level, parent_node_id, entity_id, dimensions_json,"
        " coverage_json FROM run_nodes WHERE run_id = ? ORDER BY level, node_id",
        [run_id],
    ).pl()


def list_run_measures(conn, run_id: str) -> list[str]:
    """Medidas com previsão persistida da rodada, na ordem canônica."""
    rows = conn.execute(
        "SELECT DISTINCT measure FROM forecasts WHERE run_id = ? ORDER BY measure",
        [run_id],
    ).fetchall()
    return [r[0] for r in rows]


def list_run_scenarios(conn, run_id: str) -> list[str]:
    """Cenários com previsão persistida da rodada, colocando o base primeiro."""
    rows = conn.execute(
        "SELECT DISTINCT scenario_id FROM forecasts WHERE run_id = ?",
        [run_id],
    ).fetchall()
    scen = {r[0] for r in rows}
    return ["base"] + sorted(s for s in scen if s != "base")


def _aggregate_backtest(
    conn, run_id: str, node_ids: list[str], measure: str, scenario: str
) -> pl.DataFrame:
    """Métricas de backtest agregadas do vencedor por série (sec 9.3 e 12.1).

    Usa apenas os pares avaliados (`evaluated` e com y_actual) da própria
    previsão de teste do modelo vencedor; WAPE é soma de numeradores/soma de
    denominadores dos mesmos pares — nunca média simples de percentuais.
    """
    cols = [
        "node_id",
        "measure",
        "model_alias",
        "mae",
        "rmse",
        "wape",
        "bias",
        "n_eval",
        "n_folds",
        "cv_horizon",
    ]
    if not node_ids:
        return pl.DataFrame({c: [] for c in cols})
    winners = conn.execute(
        "SELECT DISTINCT node_id, model_alias FROM forecasts WHERE run_id = ?"
        " AND measure = ? AND scenario_id = ?",
        [run_id, measure, scenario],
    ).fetchall()
    if not winners:
        return pl.DataFrame({c: [] for c in cols})
    win_pairs = {(str(nid), alias) for nid, alias in winners}
    evals: list[dict] = []
    for r in conn.execute(
        "SELECT node_id, model_alias, cutoff, ds, y_actual, yhat FROM cv_results"
        " WHERE run_id = ? AND measure = ? AND evaluated",
        [run_id, measure],
    ).fetchall():
        nid, alias, cutoff, ds, y_actual, yhat = r
        if (str(nid), alias) not in win_pairs:
            continue
        if y_actual is None or yhat is None:
            continue
        evals.append(
            {
                "node_id": str(nid),
                "cutoff": cutoff,
                "ds": ds,
                "y_actual": float(y_actual),
                "yhat": float(yhat),
            }
        )
    if not evals:
        return pl.DataFrame({c: [] for c in cols})
    df = pl.DataFrame(evals).with_columns(
        (pl.col("yhat") - pl.col("y_actual")).alias("e")
    )
    denom = float(df["y_actual"].abs().sum())
    n_cutoffs = df.group_by("node_id").agg(pl.col("cutoff").n_unique())
    n_folds = int(n_cutoffs["cutoff"].sum()) if n_cutoffs.height else 0
    cv_horizon = int(
        df.group_by(["node_id", "cutoff"])
        .agg(pl.len().alias("n"))
        .group_by("node_id")
        .agg(pl.col("n").max().alias("h"))["h"]
        .max()
        or 0
    )
    mae = float(df["e"].abs().mean())
    rmse = float((df["e"] ** 2).mean() ** 0.5)
    wape = float(df["e"].abs().sum() / denom) if denom > 0 else None
    bias = float(df["e"].mean())
    return pl.DataFrame(
        [
            {
                "node_id": "",
                "measure": measure,
                "model_alias": "winner",
                "mae": mae,
                "rmse": rmse,
                "wape": wape,
                "bias": bias,
                "n_eval": df.height,
                "n_folds": n_folds,
                "cv_horizon": cv_horizon,
            }
        ]
    )


def _load_prepared_history(conn, run_id: str, measure: str) -> pl.DataFrame:
    """Carrega valores preparados da preparação da rodada para `measure`."""
    row = conn.execute(
        "SELECT preparation_id FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    if row is None:
        return pl.DataFrame(
            {
                "series_id": [],
                "entity_id": [],
                "ds": [],
                "measure": [],
                "y": [],
                "observed": [],
                "was_adjusted": [],
            }
        )
    return conn.execute(
        "SELECT series_id, entity_id, ds, measure, y, observed, was_adjusted FROM"
        " prepared_values WHERE preparation_id = ? AND measure = ?",
        [row[0], measure],
    ).pl()


_CANDIDATE_MODEL_ALIASES = {
    "Naive", "HistoricAverage", "SeasonalNaive", "AutoETS", "AutoTheta",
    "CrostonSBA", "TSB", "AutoCES", "AutoARIMA", "ZeroBaseline",
}


def forecast_candidate_for_node(
    conn, run_id: str, node_id: str, measure: str, model_alias: str
) -> tuple[pl.DataFrame, str]:
    """Previsão futura de um candidato específico para UM nó folha, sob demanda
    (T6.4) — usada só pelo comparador de modelos do dashboard, nunca em lote.

    Só funciona para nós folha (série == entidade); nós agregados da hierarquia
    não têm um modelo próprio para reajustar (a agregação Bottom-Up soma as
    folhas). Resultado fica cacheado em `forecast_candidates_cache` por
    `(run_id, node_id, measure, model_alias)` — não recalcula em cliques
    repetidos. Modelos que dependem de contexto global (`LightGBM`,
    `AutoARIMA_X`) ficam fora do comparador — exigiriam reconstruir o frame
    global/regressoras da rodada inteira só para comparar uma série.
    """
    cached = conn.execute(
        "SELECT status, payload_json FROM forecast_candidates_cache WHERE"
        " run_id = ? AND node_id = ? AND measure = ? AND model_alias = ?",
        [run_id, node_id, measure, model_alias],
    ).fetchone()
    if cached is not None:
        status, payload_json = cached
        rows = json.loads(payload_json) if payload_json else []
        for r in rows:
            r["ds"] = date.fromisoformat(r["ds"])
        return (pl.DataFrame(rows) if rows else _empty_candidate_rows()), status

    if model_alias not in _CANDIDATE_MODEL_ALIASES:
        return _empty_candidate_rows(), "modelo_indisponivel_para_comparacao"

    import forecast_engine

    study = load_run_study(conn, run_id)
    cfg_row = conn.execute(
        "SELECT config_json FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    config = ForecastConfig.from_json(cfg_row[0]) if cfg_row and cfg_row[0] else ForecastConfig()

    series = (
        _load_prepared_history(conn, run_id, measure)
        .filter(pl.col("series_id") == node_id)
        .sort("ds")
    )
    if series.height == 0:
        return _empty_candidate_rows(), "no_encontrado_ou_nao_e_folha"

    rows, status = forecast_engine.forecast_candidate(series, model_alias, study, config)
    payload_json = json.dumps([{**r, "ds": r["ds"].isoformat()} for r in rows])
    conn.execute(
        "INSERT OR REPLACE INTO forecast_candidates_cache (run_id, node_id, measure,"
        " model_alias, status, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [run_id, node_id, measure, model_alias, status, payload_json, _now_ts()],
    )
    return (pl.DataFrame(rows) if rows else _empty_candidate_rows()), status


def _empty_candidate_rows() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "ds": pl.Date,
            "yhat": pl.Float64,
            "lo80": pl.Float64,
            "hi80": pl.Float64,
            "interval_method": pl.Utf8,
            "clipped": pl.Int64,
        }
    )


def _node_entity_map(conn, run_id: str) -> dict[str, set[str]]:
    """node_id -> entidades-folha sob o nó (via run_nodes)."""
    out: dict[str, set[str]] = {}
    for nid, eid, coverage_json in conn.execute(
        "SELECT node_id, entity_id, coverage_json FROM run_nodes WHERE run_id = ?",
        [run_id],
    ).fetchall():
        covered: set[str] = set()
        if eid:
            covered.add(eid)
        if coverage_json:
            try:
                covered.update(json.loads(coverage_json).get("entity_ids", []))
            except Exception:  # noqa: BLE001
                pass
        if covered:
            out.setdefault(str(nid), set()).update(covered)
    return out


def _view_history(conn, run_id: str, node_ids: list[str], measure: str) -> pl.DataFrame:
    """Histórico preparado por nó (soma das folhas sob ele) para a view."""
    cols = ["node_id", "measure", "ds", "y", "observed", "was_adjusted"]
    if not node_ids:
        return pl.DataFrame({c: [] for c in cols})
    prep = _load_prepared_history(conn, run_id, measure)
    if prep.height == 0:
        return pl.DataFrame({c: [] for c in cols})
    node_entities = _node_entity_map(conn, run_id)
    out: list[pl.DataFrame] = []
    for node_id in node_ids:
        eids = node_entities.get(node_id)
        if not eids:
            sub = prep.filter(pl.col("series_id") == node_id).select(
                "ds", "measure", "y", "observed", "was_adjusted"
            )
        else:
            sub = (
                prep.filter(pl.col("entity_id").is_in(list(eids)))
                .group_by(["ds", "measure"])
                .agg(
                    pl.col("y").sum().alias("y"),
                    pl.col("observed").all().alias("observed"),
                    pl.col("was_adjusted").any().alias("was_adjusted"),
                )
            )
        if sub.height:
            out.append(sub.with_columns(pl.lit(node_id).alias("node_id")).select(cols))
    if not out:
        return pl.DataFrame({c: [] for c in cols})
    return pl.concat(out).sort(["node_id", "ds"])


def _view_cards(
    preds: pl.DataFrame,
    history: pl.DataFrame,
    mat_view: pl.DataFrame,
    study: StudyConfig,
    measure: str,
    scenario: str,
    level: str,
) -> pl.DataFrame:
    """Cards da view única: totais, variação por janela de igual duração e MAT.

    `N/D` (None) quando não há base comparável: histórico ausente ou total
    histórico zero para variação; menos de 12 componentes para posição MAT.
    Nunca soma pais e filhos, mistura medidas ou acumula posições MAT.
    """
    n_nodes = preds["node_id"].n_unique()
    forecast_rows = preds.height
    fc_dates = sorted(preds["ds"].unique().to_list())
    is_mat = study.mat_mode in (MatMode.DERIVED, MatMode.DIRECT)
    mat_final = None
    mat_history = None
    mat_variation = None
    total_forecast = None
    total_history = None
    variation = None
    if preds.height and fc_dates:
        if is_mat:
            if mat_view.height:
                mat_final = float(mat_view.sort("ds")["mat"].last())
            # posição equivalente: última posição histórica completa antes da previsão
            prev = _shift_period(fc_dates[0], study.model_frequency, -1)
            if history.height:
                prev_mat = _last_historical_position(history, prev, n_lag=12)
                if prev_mat is not None:
                    mat_history = prev_mat
                    if mat_history != 0 and mat_final is not None:
                        mat_variation = (mat_final - mat_history) / mat_history
        else:
            span = len(fc_dates)
            total_forecast = float(preds["yhat"].sum())
            hist_start = _shift_period(fc_dates[0], study.model_frequency, -span)
            hist_end = _shift_period(fc_dates[0], study.model_frequency, -1)
            wh = history.filter(pl.col("ds") >= hist_start, pl.col("ds") <= hist_end)
            if wh.height:
                total_history = float(wh["y"].sum())
                if total_history != 0:
                    variation = (total_forecast - total_history) / total_history
    return pl.DataFrame(
        [
            {
                "measure": measure,
                "scenario_id": scenario,
                "level": level,
                "n_nodes": n_nodes,
                "forecast_rows": forecast_rows,
                "total_forecast": total_forecast,
                "total_history": total_history,
                "variation": variation,
                "mat_final": mat_final,
                "mat_history": mat_history,
                "mat_variation": mat_variation,
            }
        ]
    )


def _last_historical_position(
    history: pl.DataFrame, ds: date, n_lag: int = 12
) -> float | None:
    """Última posição histórica completa (ex.: MAT) até `ds`, se existirem
    `n_lag` componentes; None caso contrário (sec 5.3)."""
    h = history.filter(pl.col("ds") <= ds).sort("ds").select("ds", "y")
    if h.height < n_lag:
        return None
    pos = h.with_columns(
        pl.col("y")
        .clip(lower_bound=0.0)
        .rolling_sum(window_size=n_lag, min_samples=n_lag)
        .alias("mat")
    ).filter(pl.col("ds") == ds)
    if pos.height == 0 or pos["mat"].first() is None:
        return None
    return float(pos["mat"].first())


def _mat_view_derived(
    preds: pl.DataFrame, history: pl.DataFrame, measure: str, scenario: str
) -> pl.DataFrame:
    """Visão MAT derivada: só emite posição quando os 12 componentes existirem,
    limitando componentes a zero (sec 5.3). Não soma os limites de intervalo."""
    cols = ["node_id", "measure", "scenario_id", "ds", "mat"]
    blank = pl.DataFrame({c: [] for c in cols})
    if preds.height == 0 or history.height == 0:
        return blank
    rows: list[dict] = []
    for node_id in sorted(preds["node_id"].unique().to_list()):
        h = (
            history.filter(pl.col("node_id") == node_id)
            .select(["ds", "y"])
            .rename({"y": "value"})
        )
        f = (
            preds.filter(pl.col("node_id") == node_id)
            .select(["ds", "yhat"])
            .rename({"yhat": "value"})
        )
        if h.height == 0 or f.height == 0:
            continue
        combined = (
            pl.concat([h, f])
            .sort("ds")
            .unique(subset=["ds"])
            .with_columns(pl.col("value").clip(lower_bound=0.0).alias("y_floor"))
            .with_columns(
                pl.col("y_floor")
                .rolling_sum(window_size=12, min_samples=12)
                .alias("mat")
            )
        )
        future_dates = set(f["ds"].to_list())
        for r in combined.filter(pl.col("ds").is_in(future_dates)).to_dicts():
            if r["mat"] is None:
                continue
            rows.append(
                {
                    "node_id": node_id,
                    "measure": measure,
                    "scenario_id": scenario,
                    "ds": r["ds"],
                    "mat": float(r["mat"]),
                }
            )
    if not rows:
        return blank
    return pl.DataFrame(rows)


def _mat_view_direct(preds: pl.DataFrame) -> pl.DataFrame:
    """Visão MAT direta: a previsão já são posições MAT; apenas expõe."""
    return preds.select(
        pl.col("node_id"),
        pl.col("measure"),
        pl.col("scenario_id"),
        pl.col("ds"),
        pl.col("yhat").alias("mat"),
    )


def query_results(conn, run_id: str, filters: ResultFilter) -> DashboardData:
    """Consulta parametrizada dos resultados de uma rodada (P32, sec 12.1).

    Filtros: planície de medidas/cenário/nível/nó, faixa de datas, busca e
    dimensões reais. Quando `filters.measure` e `filters.scenario_id` definem
    uma view única, preenche `cards` (totais/variação/MAT), `metrics`
    (backtest do vencedor), `history` e `mat_view`. Os frames vêm já
    filtrados; nenhuma soma cruza medida/nível/cenário.
    """
    row = conn.execute(
        "SELECT status, summary_json, config_hash, dataset_id, config_json FROM runs"
        " WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"Run {run_id} não encontrado")
    status_raw, summary_json, config_hash, dataset_id, config_json = row
    counts = json.loads(summary_json or "{}")
    status = RunStatus(status_raw)
    study = StudyConfig.from_json(
        conn.execute(
            "SELECT study_json FROM datasets WHERE dataset_id = ?", [dataset_id]
        ).fetchone()[0]
    )
    cfg_json = config_json or "{}"
    cfg = ForecastConfig.from_json(cfg_json) if cfg_json != "{}" else ForecastConfig()

    preds = _query_predictions(conn, run_id, filters)
    nodes = _query_nodes(conn, run_id)
    node_ids = preds["node_id"].unique().to_list() if preds.height else []
    scores = _query_scores(conn, run_id, node_ids, filters.measure)
    issues = _query_issues(conn, run_id)

    coverage = {
        "predicted_nodes": len(
            set(node_ids)
            if node_ids
            else (preds["node_id"].unique().to_list() if preds.height else [])
        ),
        "predicted_series": preds["node_id"].n_unique() if preds.height else 0,
        "eligible": counts.get("eligible"),
        "failed": counts.get("failed", 0),
        "predictions": counts.get("predictions", 0),
        "cv_horizon": cfg.cv_horizon,
    }
    summary = RunSummary(
        run_id, status, counts, {}, coverage, issues, config_hash or ""
    )

    cards = pl.DataFrame()
    metrics = pl.DataFrame()
    history = pl.DataFrame()
    mat_view = pl.DataFrame()
    measure = filters.measure if filters.measure else ""
    scenario = filters.scenario_id if filters.scenario_id else ""
    if measure and scenario and preds.height:
        level = (
            filters.node_level
            if filters.node_level
            else (str(preds["level"].first()) if "level" in preds.columns else "folha")
        )
        history = _view_history(conn, run_id, node_ids, measure)
        if study.mat_mode in (MatMode.DERIVED, MatMode.DIRECT):
            if study.mat_mode == MatMode.DERIVED:
                mat_view = _mat_view_derived(preds, history, measure, scenario)
            else:
                mat_view = _mat_view_direct(preds)
        cards = _view_cards(preds, history, mat_view, study, measure, scenario, level)
        metrics = _aggregate_backtest(conn, run_id, node_ids, measure, scenario)
    return DashboardData(
        summary, nodes, preds, scores, issues, cards, metrics, history, mat_view
    )


_EXCEL_MAX_ROWS = 1_048_576
_FORMULA_CHARS = ("=", "+", "-", "@")


def _dep_versions() -> dict[str, str]:
    """Versões efetivas das dependências para os metadados (sec 12.2)."""
    import importlib.metadata as _md

    out: dict[str, str] = {}
    for lib in (
        "polars",
        "duckdb",
        "openpyxl",
        "numpy",
        "pandas",
        "pyarrow",
        "statsforecast",
        "utilsforecast",
        "hierarchicalforecast",
        "mlforecast",
        "lightgbm",
    ):
        try:
            out[lib] = _md.version(lib)
        except Exception:  # noqa: BLE001, S110
            pass
    return out


def _run_mapping(conn, run_id: str) -> MappingConfig:
    """Mapeamento do dataset da rodada (separador/encoding p/ exportação)."""
    row = conn.execute(
        "SELECT d.mapping_json FROM runs r JOIN datasets d"
        " ON d.dataset_id = r.dataset_id WHERE r.run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        return MappingConfig()
    return MappingConfig.from_json(row[0])


def _export_rf(run_id: str, config: ExportConfig) -> ResultFilter:
    """ResultFilter do recorte de exportação (sec 12.2: um cenário e um nível)."""
    return ResultFilter(
        run_id=run_id,
        node_level=config.level,
        measure=config.measure,
        scenario_id=config.scenario_id or "base",
        dimensions=dict(config.filters or {}),
    )


def _node_meta(
    conn, run_id: str, nodes: pl.DataFrame, study: StudyConfig
) -> pl.DataFrame:
    """node_id → level/entity_id e dimensões expandidas para a exportação."""
    base = nodes.select(
        pl.col("node_id"),
        pl.col("level"),
        pl.col("entity_id").cast(pl.String),
    )
    for d in study.dimension_names:
        vals: list[str | None] = []
        for row in nodes.to_dicts():
            try:
                dv = json.loads(row.get("dimensions_json") or "{}")
            except Exception:  # noqa: BLE001
                dv = {}
            v = dv.get(d)
            vals.append(None if v in (None, "") else str(v))
        base = base.with_columns(pl.Series(d, vals))
    return base


def _node_coverage(nodes: pl.DataFrame) -> dict[str, set[str]]:
    """node_id → entidades-folha cobertas, só para nós agregados (sec 11)."""
    out: dict[str, set[str]] = {}
    for row in nodes.to_dicts():
        if row.get("level") == "folha":
            continue
        nid = row["node_id"]
        try:
            cov = json.loads(row.get("coverage_json") or "{}")
        except Exception:  # noqa: BLE001
            cov = {}
        eids = {str(e) for e in cov.get("entity_ids", [])}
        if eids:
            out[nid] = eids
    return out


def _long_columns(dim_names: list[str], include_intervals: bool) -> list[str]:
    cols = list(dim_names) + [
        "node_id",
        "level",
        "entity_id",
        "measure",
        "scenario_id",
        "ds",
        "tipo",
        "valor",
        "yhat_base",
        "observado",
        "ajustado",
    ]
    if include_intervals:
        cols += ["lo80", "hi80"]
    cols += [
        "model_alias",
        "interval_method",
        "status",
        "run_id",
        "preparation_id",
        "dataset_id",
    ]
    return cols


def _empty_long(config: ExportConfig, study: StudyConfig) -> pl.DataFrame:
    cols = _long_columns(study.dimension_names, config.include_intervals)
    return pl.DataFrame({c: [] for c in cols})


def _history_mat_positions(history: pl.DataFrame) -> pl.DataFrame:
    """Posições MAT históricas completas (12 componentes) por nó (sec 5.3)."""
    cols = ["node_id", "ds", "mat"]
    if history.height == 0:
        return pl.DataFrame({c: [] for c in cols})
    rows: list[dict] = []
    for node_id in sorted(history["node_id"].unique().to_list()):
        h = (
            history.filter(pl.col("node_id") == node_id)
            .sort("ds")
            .with_columns(pl.col("y").clip(lower_bound=0.0).alias("yf"))
            .with_columns(
                pl.col("yf").rolling_sum(window_size=12, min_samples=12).alias("mat")
            )
        )
        for r in h.filter(pl.col("mat").is_not_null()).to_dicts():
            rows.append({"node_id": node_id, "ds": r["ds"], "mat": float(r["mat"])})
    if not rows:
        return pl.DataFrame({c: [] for c in cols})
    return pl.DataFrame(rows)


def _measure_export_part(
    fp: pl.DataFrame,
    hist: pl.DataFrame,
    meta: pl.DataFrame,
    config: ExportConfig,
    measure: str,
    scenario_id: str,
    base_val: dict[tuple[str, str, object], float],
    runinfo: dict[str, str],
    dim_names: list[str],
) -> pl.DataFrame:
    """Parte longa de uma medida: histórico + previsão com identificadores.

    Histórico distingue `observado`/`ajustado`; previsão leva modelo, status e
    intervalos; datas vêm na ordem canônica (sec 12.2).
    """
    incl = config.include_intervals
    cols = _long_columns(dim_names, incl)
    fr = pl.DataFrame()
    hr = pl.DataFrame()
    meta_lite = meta.select(["node_id"] + dim_names)

    if fp.height:
        fr = (
            fp.select(
                "node_id",
                "measure",
                "scenario_id",
                "ds",
                "yhat",
                "lo80",
                "hi80",
                "model_alias",
                "interval_method",
                "status",
                "level",
                "entity_id",
            )
            .join(meta_lite, on="node_id", how="left")
            .with_columns(
                pl.lit("previsao").alias("tipo"),
                pl.col("yhat").alias("valor"),
                pl.lit(None, pl.Float64).alias("yhat_base"),
                pl.lit(None, pl.Boolean).alias("observado"),
                pl.lit(None, pl.Boolean).alias("ajustado"),
            )
            .drop("yhat")
        )
        if scenario_id != "base" and base_val:
            bm = pl.DataFrame(
                [
                    {"node_id": nid, "ds": d, "yhat_base": v}
                    for (nid, m, d), v in base_val.items()
                    if m == measure
                ]
            )
            if bm.height:
                fr = fr.drop("yhat_base").join(bm, on=["node_id", "ds"], how="left")
        fr = fr.with_columns(
            pl.col("entity_id").cast(pl.String),
            pl.col("lo80").cast(pl.Float64),
            pl.col("hi80").cast(pl.Float64),
            pl.col("model_alias").cast(pl.String),
            pl.col("interval_method").cast(pl.String),
            pl.col("status").cast(pl.String),
        )

    if hist.height:
        hr = (
            hist.select("node_id", "measure", "ds", "y", "observed", "was_adjusted")
            .join(meta, on="node_id", how="left")
            .rename(
                {
                    "y": "valor",
                    "observed": "observado",
                    "was_adjusted": "ajustado",
                }
            )
            .with_columns(
                pl.lit(scenario_id).alias("scenario_id"),
                pl.lit("historico").alias("tipo"),
                pl.lit(None, pl.Float64).alias("yhat_base"),
                pl.lit(None, pl.Float64).alias("lo80"),
                pl.lit(None, pl.Float64).alias("hi80"),
                pl.lit(None, pl.String).alias("model_alias"),
                pl.lit(None, pl.String).alias("interval_method"),
                pl.lit(None, pl.String).alias("status"),
                pl.col("entity_id").cast(pl.String),
            )
        )

    parts: list[pl.DataFrame] = []
    for p in (hr, fr):
        if not p.height:
            continue
        for k, v in runinfo.items():
            p = p.with_columns(pl.lit(v).alias(k))
        parts.append(p.select(cols))
    if not parts:
        return _empty_long(config, StudyConfig(name="", dimension_names=dim_names))
    return pl.concat(parts)


def _round_and_recompose(
    part: pl.DataFrame, nodes: pl.DataFrame, measure: str
) -> pl.DataFrame:
    """Arredonda Unidades nas folhas e recalcula pais a partir delas (sec 11).

    `round_units` aplica-se apenas à medida Unidades e somente às linhas de
    previsão; pais recompostos = soma dos filhos folha arredondados presentes
    no recorte. Filhos ausentes mantêm o valor persistido (cobertura parcial).
    """
    if measure != "unidades" or part.height == 0:
        return part
    forecast = part.filter(pl.col("tipo") == "previsao")
    if forecast.height == 0:
        return part
    history = part.filter(pl.col("tipo") == "historico")

    coverage = _node_coverage(nodes)
    leaf_vals: dict[tuple[str, str], float] = {}
    for r in forecast.to_dicts():
        eid = r.get("entity_id")
        if r.get("level") == "folha" and eid:
            leaf_vals[(eid, r["ds"].isoformat())] = round(float(r["valor"]), 0)

    new_rows: list[dict] = []
    for r in forecast.to_dicts():
        row = dict(r)
        if r.get("level") == "folha":
            row["valor"] = round(float(r["valor"]), 0)
        else:
            eids = coverage.get(r["node_id"])
            if eids:
                total = 0.0
                ok = True
                for eid in eids:
                    v = leaf_vals.get((eid, r["ds"].isoformat()))
                    if v is None:
                        ok = False
                        break
                    total += v
                if ok:
                    row["valor"] = total
        new_rows.append(row)
    out = pl.DataFrame(new_rows)
    return pl.concat([history, out]) if history.height else out


def _export_long(
    conn, run_id: str, config: ExportConfig, study: StudyConfig
) -> pl.DataFrame:
    """Quadro longo canônico do recorte: histórico + previsão (P34, sec 12.2).

    Inclui dimensões/chave, nó/nível, medida, cenário, datas ordenadas, tipo,
    valor, previsão base quando aplicável, limites opcionais, modelo, status e
    identificadores de rodada/recorte. Filtros respeitam exatamente o recorte.
    """
    rf = _export_rf(run_id, config)
    preds = _query_predictions(conn, run_id, rf)
    nodes = _query_nodes(conn, run_id)
    meta = _node_meta(conn, run_id, nodes, study)
    runrow = conn.execute(
        "SELECT dataset_id, preparation_id FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    if runrow is None:
        raise KeyError(f"Run {run_id} não encontrado")
    runinfo = {"run_id": run_id, "preparation_id": runrow[1], "dataset_id": runrow[0]}

    scenario_id = config.scenario_id or "base"
    measures = list_run_measures(conn, run_id)
    if config.measure:
        measures = [m for m in measures if m == config.measure]
    elif not measures:
        measures = list(preds["measure"].unique().to_list() if preds.height else [])

    base_val: dict[tuple[str, str, object], float] = {}
    if scenario_id != "base":
        bf = ResultFilter(
            run_id=run_id,
            node_level=config.level,
            measure=None,
            scenario_id="base",
            dimensions=dict(config.filters or {}),
        )
        bp = _query_predictions(conn, run_id, bf)
        if bp.height:
            for r in bp.to_dicts():
                base_val[(r["node_id"], r["measure"], r["ds"])] = float(r["yhat"])

    parts: list[pl.DataFrame] = []
    for measure in measures:
        fp = (
            preds.filter(pl.col("measure") == measure)
            if preds.height
            else pl.DataFrame()
        )
        hids = fp["node_id"].unique().to_list() if fp.height else []
        hist = _view_history(conn, run_id, hids, measure) if hids else pl.DataFrame()

        part = _measure_export_part(
            fp,
            hist,
            meta,
            config,
            measure,
            scenario_id,
            base_val,
            runinfo,
            study.dimension_names,
        )
        # Visão MAT derivada: posições (não soma de posições) e limites nulos.
        if config.temporal_view == TemporalView.MAT:
            if study.mat_mode == MatMode.DERIVED and fp.height and hist.height:
                mv = _mat_view_derived(fp, hist, measure, scenario_id)
                hm = _history_mat_positions(hist)
                prev = part.filter(pl.col("tipo") == "previsao")
                histo = part.filter(pl.col("tipo") == "historico")
                if mv.height:
                    prev = (
                        prev.join(
                            mv.select(["node_id", "ds", "mat"]),
                            on=["node_id", "ds"],
                            how="inner",
                        )
                        .with_columns(pl.col("mat").alias("valor"))
                        .drop("mat")
                    )
                else:
                    prev = pl.DataFrame(schema=prev.schema)
                if hm.height:
                    histo = (
                        histo.join(hm, on=["node_id", "ds"], how="inner")
                        .with_columns(pl.col("mat").alias("valor"))
                        .drop("mat")
                    )
                else:
                    histo = pl.DataFrame(schema=histo.schema)
                subs = [p for p in (histo, prev) if p.height]
                part = pl.concat(subs) if subs else pl.DataFrame(schema=prev.schema)
                part = part.with_columns(
                    pl.lit(None, pl.Float64).alias("lo80"),
                    pl.lit(None, pl.Float64).alias("hi80"),
                    pl.lit(None, pl.Float64).alias("yhat_base"),
                ).select(_long_columns(study.dimension_names, config.include_intervals))
            elif study.mat_mode != MatMode.DIRECT:
                pass
        elif config.round_units:
            part = _round_and_recompose(part, nodes, measure)
        parts.append(part)

    if not parts:
        return _empty_long(config, study)
    combined = pl.concat(parts)
    return combined.sort(["node_id", "measure", "ds"])


def _to_wide(long_df: pl.DataFrame, dim_names: list[str]) -> pl.DataFrame:
    """Layout largo: chave/dimensões/medida + colunas por data real com prefixo.

    Prefixos `historico_` e `previsao_` separam os tipos de forma inequívoca
    (sec 12.2); modelo/status/intervalo e previsão base ficam como colunas.
    """
    if long_df.height == 0 or "tipo" not in long_df.columns:
        return long_df
    idx = list(dim_names) + ["node_id", "level", "entity_id", "measure", "scenario_id"]
    tmp = long_df.with_columns(
        pl.concat_str(
            [
                pl.col("tipo"),
                pl.lit("_"),
                pl.col("ds").dt.strftime("%Y-%m-%d"),
            ],
            separator="",
        ).alias("_col")
    )
    gcols = ["model_alias", "interval_method", "status", "yhat_base"]
    fc = tmp.filter(pl.col("tipo") == "previsao")
    if fc.height:
        gmeta = fc.group_by(idx).agg(
            pl.col("model_alias").first().alias("model_alias"),
            pl.col("interval_method").first().alias("interval_method"),
            pl.col("status").first().alias("status"),
            pl.col("yhat_base").first().alias("yhat_base"),
        )
    else:
        gmeta = pl.DataFrame({c: [] for c in idx + gcols})
    piv = tmp.pivot(on="_col", index=idx, values="valor", aggregate_function="first")
    out = piv.join(gmeta, on=idx, how="left") if gmeta.height else piv
    for c in gcols:
        if c not in out.columns:
            out = out.with_columns(pl.lit(None).cast(pl.String).alias(c))
    date_cols = sorted(
        (c for c in out.columns if c not in idx and c not in gcols),
        key=lambda c: (c.rsplit("_", 1)[0], c.rsplit("_", 1)[1]),
    )
    return out.select(idx + gcols + date_cols)


def _protect_formula_texts(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Protege células textuais iniciadas por caractere de fórmula (sec 12.2).

    Aplica apóstrofo de proteção apenas em colunas de texto; valores numéricos
    (inclusive negativos legítimos) nunca são alterados. Número de células
    protegidas vai para os metadados.
    """
    out = df
    total = 0
    for c in out.columns:
        if out[c].dtype != pl.String:
            continue
        cond = None
        for p in _FORMULA_CHARS:
            piece = pl.col(c).is_not_null() & pl.col(c).str.starts_with(p)
            cond = piece if cond is None else (cond | piece)
        if cond is None:
            continue
        total += int(out.filter(cond).height)
        out = out.with_columns(
            pl.when(cond)
            .then(pl.concat_str([pl.lit("'"), pl.col(c)], separator=""))
            .otherwise(pl.col(c))
            .alias(c)
        )
    return out, total


def _csv_artifact(
    df: pl.DataFrame, filename: str, delimiter: str, metadata: dict
) -> ExportArtifact:
    """Escreve CSV UTF-8-SIG com separador configurável (sec 5.4/12.2)."""
    text = df.write_csv(separator=delimiter)
    buf = io.BytesIO()
    buf.write(b"\xef\xbb\xbf")
    buf.write(text.encode("utf-8"))
    return ExportArtifact(filename, "text/csv", buf.getvalue(), df.height, metadata)


def _export_metadata(
    conn, run_id: str, config: ExportConfig, study: StudyConfig, mapping: MappingConfig
) -> dict:
    """Metadados do recorte para CSV/XLSX (rodada, configuração e versões)."""
    runrow = conn.execute(
        "SELECT status, config_json, config_hash, started_at, ended_at,"
        " dataset_id, preparation_id FROM runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    status = runrow[0] if runrow else "?"
    cfg_json = runrow[1] if runrow else "{}"
    cfg = (
        ForecastConfig.from_json(cfg_json)
        if cfg_json and cfg_json != "{}"
        else ForecastConfig()
    )
    cust = (config.filters or {}) if isinstance(config.filters, dict) else {}
    return {
        "Rodada": {
            "run_id": run_id,
            "dataset_id": runrow[3] if runrow else None,
            "preparation_id": runrow[6] if runrow else None,
            "status": status,
            "config_hash": runrow[2] if runrow else None,
            "iniciado_em": str(runrow[4]) if runrow else None,
            "terminado_em": str(runrow[5]) if runrow else None,
        },
        "Estudo": {
            "nome": study.name,
            "layout_fonte": study.layout.value,
            "dimensoes": ", ".join(study.dimension_names),
            "nivel_analise": study.analysis_level,
            "frequencia_origem": study.source_frequency.value,
            "frequencia_modelagem": study.model_frequency.value,
            "mat_mode": study.mat_mode.value,
            "medidas": ", ".join(study.measures),
            "moeda": study.currency,
            "ultimo_periodo_fechado": (
                str(study.history_end) if study.history_end else None
            ),
            "historico_periodos": study.history_periods,
        },
        "Previsao": {
            "horizonte_periodos": cfg.horizon_periods,
            "modo": cfg.mode.value,
            "modelos": ", ".join(cfg.candidate_aliases) or "default",
            "cv_horizon": cfg.cv_horizon,
            "cv_windows": cfg.cv_windows,
            "interval_level": cfg.interval_level,
            "interval_metodo": cfg.interval_level,
            "seed": cfg.seed,
            "batch_size": cfg.batch_size,
            "n_jobs": cfg.n_jobs,
            "ml": cfg.enable_ml,
            "hierarquia": (
                f"{cfg.hierarchy.mode.value}:{cfg.hierarchy.forecast_level}"
                if cfg.hierarchy
                else "independente:"
            ),
        },
        "Recorte da exportacao": {
            "formato": config.format.value,
            "layout_saida": config.layout,
            "cenario": config.scenario_id or "base",
            "medida": config.measure,
            "nivel": config.level,
            "visao_temporal": config.temporal_view.value,
            "inclui_intervalos": config.include_intervals,
            "arredondar_unidades": config.round_units,
            "filtros_dimensoes": json.dumps(cust, ensure_ascii=False),
        },
        "Mapeamento": {
            "separador_csv": mapping.delimiter,
            "encoding": mapping.encoding,
            "separador_decimal": mapping.decimal_separator,
            "date_format": mapping.date_format,
            "schema_version": str(config.schema_version),
        },
        "Dependencias": _dep_versions(),
    }


def _scores_frame(
    conn, run_id: str, config: ExportConfig, study: StudyConfig
) -> pl.DataFrame:
    """Métricas por série/medida do recorte, com dimensões para leitura."""
    rf = _export_rf(run_id, config)
    preds = _query_predictions(conn, run_id, rf)
    node_ids = preds["node_id"].unique().to_list() if preds.height else []
    scores = _query_scores(conn, run_id, node_ids, config.measure)
    if scores.height == 0:
        return pl.DataFrame(
            {
                "node_id": [],
                "measure": [],
                "model_alias": [],
                "mae": [],
                "rmse": [],
                "wape": [],
                "bias": [],
                "n_eval": [],
                "n_folds": [],
                "eligible": [],
            }
        )
    nodes = _query_nodes(conn, run_id)
    meta = _node_meta(conn, run_id, nodes, study)
    scores = scores.join(meta, on="node_id", how="left")
    return scores.sort(["node_id", "measure", "model_alias"])


def _quality_frame(conn, run_id: str) -> pl.DataFrame:
    """Problemas da rodada (qualidade) em formato tabular (sec 12.2)."""
    rows: list[dict] = []
    for iss in _query_issues(conn, run_id):
        rows.append(
            {
                "code": iss.code,
                "severity": iss.severity.value,
                "message": iss.message,
                "entity_ids": ", ".join(iss.entity_ids),
                "source_row_ids": ", ".join(iss.source_row_ids),
                "field": iss.field_name or "",
                "details_json": json.dumps(iss.details, ensure_ascii=False),
            }
        )
    cols = [
        "code",
        "severity",
        "message",
        "entity_ids",
        "source_row_ids",
        "field",
        "details_json",
    ]
    return pl.DataFrame(rows) if rows else pl.DataFrame({c: [] for c in cols})


def _assumptions_frame(conn, run_id: str) -> pl.DataFrame:
    """Premissas da rodada (regras de cenário e regressoras) para a aba."""
    row = conn.execute(
        "SELECT assumptions_snapshot_json FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    cols = [
        "tipo",
        "id",
        "nome",
        "familia",
        "medida_alvo",
        "efeito",
        "taxa",
        "prioridade",
        "vigencia_inicio",
        "vigencia_fim",
        "escopo_json",
        "habilitado",
    ]
    rows: list[dict] = []
    if row:
        snap = json.loads(row[0] or "{}")
        for s in snap.get("scenarios", []) or []:
            try:
                scen = ScenarioConfig.from_json(s)
            except Exception:  # noqa: BLE001, S112
                continue
            for r in scen.rules or []:
                if not hasattr(r, "rule_id"):
                    continue
                rows.append(
                    {
                        "tipo": "regra",
                        "id": r.rule_id,
                        "nome": scen.name,
                        "familia": r.family,
                        "medida_alvo": r.target_measure,
                        "efeito": r.effect.value,
                        "taxa": r.rate,
                        "prioridade": r.priority,
                        "vigencia_inicio": (
                            str(r.start_period) if r.start_period else None
                        ),
                        "vigencia_fim": str(r.end_period) if r.end_period else None,
                        "escopo_json": json.dumps(
                            {
                                "filtros": r.filters,
                                "excluidos": list(r.excluded_entity_ids),
                            },
                            ensure_ascii=False,
                        ),
                        "habilitado": r.enabled,
                    }
                )
        for r in snap.get("regressors", []) or []:
            try:
                reg = RegressorSpec.from_json(r)
            except Exception:  # noqa: BLE001, S112
                continue
            rows.append(
                {
                    "tipo": "regressora",
                    "id": reg.regressor_id,
                    "nome": reg.name,
                    "familia": "",
                    "medida_alvo": "",
                    "efeito": "",
                    "taxa": None,
                    "prioridade": None,
                    "vigencia_inicio": None,
                    "vigencia_fim": None,
                    "escopo_json": json.dumps(
                        {
                            "escopo": reg.scope_filters,
                            "unidade": reg.unit,
                            "conhecida_antecipadamente": reg.known_in_advance,
                            "politica": reg.fill_policy.value,
                        },
                        ensure_ascii=False,
                    ),
                    "habilitado": reg.enabled,
                }
            )
    return pl.DataFrame(rows) if rows else pl.DataFrame({c: [] for c in cols})


# ---------------------------------------------------------------------------
# Escrita XLSX (P35, sec 12.2)
# ---------------------------------------------------------------------------


def _append_frame(ws, df: pl.DataFrame) -> None:
    """Escreve um frame numa aba, sem interpretar strings como fórmulas/URLs."""
    ws.append(list(df.columns))
    for row in df.iter_rows():
        ws.append(list(row))
        last = ws.max_row
        for i, v in enumerate(row, start=1):
            if isinstance(v, str) and v and v[0] in _FORMULA_CHARS:
                cell = ws.cell(last, i)
                if cell.data_type != "s":
                    cell.data_type = "s"


def _write_sheet(wb, name: str, df: pl.DataFrame) -> None:
    """Cria aba(s) dividindo acima do limite de linhas do Excel (sec 12.2)."""
    if df.height == 0:
        ws = wb.create_sheet(name)
        ws.append(list(df.columns))
        return
    n_sheets = max(1, (df.height + _EXCEL_MAX_ROWS - 1) // _EXCEL_MAX_ROWS)
    for i in range(n_sheets):
        ws_name = name if i == 0 else f"{name}_{i + 1}"
        ws = wb.create_sheet(ws_name)
        _append_frame(ws, df.slice(i * _EXCEL_MAX_ROWS, _EXCEL_MAX_ROWS))


def _write_metadata_sheet(wb, meta: dict) -> None:
    ws = wb.create_sheet("Metadados")
    ws.append(["Secao", "Chave", "Valor"])
    for section, items in meta.items():
        if not isinstance(items, dict):
            ws.append([section, "", str(items)])
            continue
        for key, value in items.items():
            ws.append(
                [
                    section,
                    str(key),
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else str(value),
                ]
            )


def _build_xlsx(
    conn,
    run_id: str,
    config: ExportConfig,
    study: StudyConfig,
    long_df: pl.DataFrame,
    meta: dict,
) -> bytes:
    """Workbook com Previsoes, Historico, Metricas, Premissas, Qualidade e
    Metadados; cabeçalhos e tipos adequados e proteção contra fórmulas."""
    prev = (
        long_df.filter(pl.col("tipo") == "previsao")
        if long_df.height
        else pl.DataFrame({c: [] for c in long_df.columns})
    )
    hist = (
        long_df.filter(pl.col("tipo") == "historico")
        if long_df.height
        else pl.DataFrame({c: [] for c in long_df.columns})
    )
    if config.layout == "wide":
        prev = _to_wide(prev, study.dimension_names)
        hist = _to_wide(hist, study.dimension_names)

    metrics = _scores_frame(conn, run_id, config, study)
    quality = _quality_frame(conn, run_id)
    prem = _assumptions_frame(conn, run_id)

    wb = openpyxl.Workbook()
    first_ws = wb.active
    if first_ws is not None:
        wb.remove(first_ws)  # type: ignore[arg-type]
    _write_sheet(wb, "Previsoes", prev)
    _write_sheet(wb, "Historico", hist)
    _write_sheet(wb, "Metricas", metrics)
    _write_sheet(wb, "Premissas", prem)
    _write_sheet(wb, "Qualidade", quality)
    _write_metadata_sheet(wb, meta)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_results(conn, run_id: str, config: ExportConfig) -> ExportArtifact:
    """Exportação do recorte (P34 CSV / P35 XLSX) — sec 12.2.

    Um cenário e um nível por exportação; várias medidas cabem no longo.
    CSV canônico longo ou largo, UTF-8-SIG com separador do mapeamento; XLSX
    com as abas previstas, dividindo acima do limite de linhas do Excel.
    """
    study = load_run_study(conn, run_id)
    mapping = _run_mapping(conn, run_id)
    long_df = _export_long(conn, run_id, config, study)
    meta = _export_metadata(conn, run_id, config, study, mapping)

    if config.format == ExportFormat.XLSX:
        data = _build_xlsx(conn, run_id, config, study, long_df, meta)
        return ExportArtifact(
            f"forecast_{run_id}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            data,
            long_df.height,
            meta,
        )

    if config.layout == "wide":
        out_df, prot = _protect_formula_texts(_to_wide(long_df, study.dimension_names))
    else:
        out_df, prot = _protect_formula_texts(long_df)
    meta["Recorte da exportacao"]["celulas_protegidas_formula"] = prot
    meta["Recorte da exportacao"]["protecao_formula"] = "apostrofo_prefixo"
    filename = f"forecast_{run_id}.csv"
    return _csv_artifact(out_df, filename, mapping.delimiter, meta)


def export_metrics_csv(conn, run_id: str, config: ExportConfig) -> ExportArtifact:
    """CSV separado de métricas por série/medida do recorte (sec 12.2)."""
    study = load_run_study(conn, run_id)
    mapping = _run_mapping(conn, run_id)
    scores, prot = _protect_formula_texts(_scores_frame(conn, run_id, config, study))
    meta = _export_metadata(conn, run_id, config, study, mapping)
    meta["Recorte da exportacao"]["celulas_protegidas_formula"] = prot
    return _csv_artifact(scores, f"metricas_{run_id}.csv", mapping.delimiter, meta)


def export_quality_csv(conn, run_id: str, config: ExportConfig) -> ExportArtifact:
    """CSV separado de problemas de qualidade da rodada (sec 12.2)."""
    study = load_run_study(conn, run_id)
    mapping = _run_mapping(conn, run_id)
    quality, prot = _protect_formula_texts(_quality_frame(conn, run_id))
    meta = _export_metadata(conn, run_id, config, study, mapping)
    meta["Recorte da exportacao"]["celulas_protegidas_formula"] = prot
    return _csv_artifact(quality, f"qualidade_{run_id}.csv", mapping.delimiter, meta)


# ---------------------------------------------------------------------------
# Assinatura pública mínima para evitar referência a stub
# ---------------------------------------------------------------------------


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
