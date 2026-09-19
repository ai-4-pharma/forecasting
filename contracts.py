"""Forecast Community - contratos compartilhados entre enginas e interface.

Este modulo nao importa data_engine.py nem forecast_engine.py. Define enums,
dataclasses, schemas de frames, constantes de limite, codigos de erro e
funcoes de validacao usadas pelos demais modulos e pela UI.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal

import polars as pl

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Layout(str, enum.Enum):
    LONG = "long"
    WIDE = "wide"


class SourceFrequency(str, enum.Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    MAT = "mat"


class MatMode(str, enum.Enum):
    NONE = "none"
    DERIVED = "derived"
    DIRECT = "direct"


class DuplicateAction(str, enum.Enum):
    REJECT = "reject"
    SUM = "sum"


class MissingAction(str, enum.Enum):
    EXCLUDE_SERIES = "exclude_series"
    ZERO = "zero"
    FFILL = "ffill"


class NegativeAction(str, enum.Enum):
    REJECT = "reject"
    ALLOW = "allow"


class OutlierAction(str, enum.Enum):
    KEEP = "keep"
    WINSORIZE = "winsorize"


class HierarchyMode(str, enum.Enum):
    INDEPENDENT = "independent"
    BOTTOM_UP = "bottom_up"
    MINTRACE = "mintrace"


class ForecastMode(str, enum.Enum):
    FAST = "fast"
    ADVANCED = "advanced"


class EffectType(str, enum.Enum):
    PULSE = "pulse"
    STEP = "step"
    ANNUAL_STEP = "annual_step"


class RegressorFillPolicy(str, enum.Enum):
    NONE = "none"
    EXPLICIT_HOLD = "explicit_hold"


class TemporalView(str, enum.Enum):
    CANONICAL = "canonical"
    MAT = "mat"


class ExportFormat(str, enum.Enum):
    CSV = "csv"
    XLSX = "xlsx"


class Severity(str, enum.Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunStage(str, enum.Enum):
    STARTED = "started"
    PROFILE = "profile"
    CV = "cv"
    FORECAST = "forecast"
    SCENARIO = "scenario"
    RECONCILE = "reconcile"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PredictionStatus(str, enum.Enum):
    OK = "ok"
    FALLBACK = "fallback"
    INSUFFICIENT_HISTORY = "insufficient_history"
    PARTIAL_COVERAGE = "partial_coverage"


class IntervalMethod(str, enum.Enum):
    NATIVE = "native"
    CONFORMAL = "conformal"
    UNAVAILABLE_INSUFFICIENT_HISTORY = "unavailable_insufficient_history"
    NULL = "null"


class SelectionReason(str, enum.Enum):
    BEST_MAE = "best_mae"
    TIE_BREAK = "tie_break"
    FALLBACK = "fallback"
    BASELINE = "baseline"
    ZERO_BASELINE = "zero_baseline"
    INSUFFICIENT_HISTORY = "insufficient_history"


# ---------------------------------------------------------------------------
# Constantes de limite e heuristica (decisoes de produto, secoes 5 e 9)
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_PHYSICAL_ROWS = 1_000_000
MAX_CANONICAL_OBSERVATIONS = 600_000
MAX_ENTITIES = 5_000
MAX_MEASURES = 2
MAX_HORIZON_MONTHLY = 120
MAX_HISTORY_MONTHS = 60
MAX_HORIZON_QUARTERLY = 40
MAX_HISTORY_QUARTERS = 20
MAX_HORIZON_YEARLY = 10
MAX_HISTORY_YEARS = 5
MAX_HORIZON_MAT = 120
MAX_HISTORY_MAT = 60

DEFAULT_CV_WINDOWS = 1
DEFAULT_BATCH_SIZE = 250
MAX_BATCH_SIZE = 500
DEFAULT_N_JOBS = 1
MAX_N_JOBS = 4
DEFAULT_INTERVAL_LEVEL = 80
DEFAULT_SEED = 42
MAX_REGRESSORS_PER_RUN = 5

# S1.6: núcleo do produto (tela única) vs. modo avançado/laboratório. O núcleo
# cobre o baseline automático (S1.7) sem exigir o complemento de ML.
CORE_ALIASES = ("Naive", "SeasonalNaive", "AutoETS", "AutoTheta", "CrostonSBA", "TSB")
ADVANCED_ALIASES = (
    "MediaMovel3",
    "MediaMovel6",
    "MediaMovel12",
    "HistoricAverage",
    "RegLinearDrift",
    "Holt",
    "HoltDamped",
    "AutoCES",
    "AutoARIMA",
    "AutoTBATS",
    "ETS_Damped",
    "AutoARIMA_X",
    "LightGBM",
    "XGBoost",
)

INTERMITTENT_ADI_THRESHOLD = 1.32
INTERMITTENT_CV2_THRESHOLD = 0.49

OUTLIER_MAD_MULTIPLIER = 3.0
OUTLIER_MAD_SCALE = 1.4826
DEFAULT_WINSORIZE_QUANTILE = 0.99

TIE_TOLERANCE = 1.01
INT_MIN_HISTORY_REGULAR = 8
INT_MIN_HISTORY_SEASONAL = 24  # dois ciclos de 12 meses
MIN_CV_FIRST_TRAIN = 3
MIN_CV_WINDOWS = 2
MIN_INTERMITTENT_POSITIVES = 2
MIN_INTERMITTENT_PERIODS = 8
MIN_TRAIN_FOR_CV = 8
MIN_FINAL_HISTORY = 4

NUMERIC_TOL = 1e-8

# Cods. de erro estaveis (code -> mensagem padrao nao faz parte do contrato,
# a UI traduz os códigos; aqui somente registramos os identificadores).
ERR_ROW_INVALID = "E_ROWS_INVALID"
ERR_DUPLICATE = "E_DUPLICATE"
ERR_UNITS_FRACTION = "E_UNITS_FRACTION"
ERR_XLSX_FORMULA_NOT_CACHED = "E_XLSX_FORMULA_NOT_CACHED"
ERR_XLSX_MERGED = "E_XLSX_MERGED_CELL"
ERR_HEADER_AMBIGUOUS = "E_HEADER_AMBIGUOUS"
ERR_ENTITY_LIMIT = "E_ENTITY_LIMIT"
ERR_OBS_LIMIT = "E_OBS_LIMIT"
ERR_ROWS_LIMIT = "E_ROWS_LIMIT"
ERR_FILE_TOO_BIG = "E_FILE_TOO_BIG"
ERR_DATE_AMBIGUOUS = "E_DATE_AMBIGUOUS"
ERR_KEY_EMPTY = "E_KEY_EMPTY"
ERR_NEGATIVE_HISTORY = "E_NEGATIVE_HISTORY"
ERR_DIMENSION_CONFLICT = "E_DIMENSION_CONFLICT"
ERR_KEY_MISSING = "E_KEY_MISSING"
ERR_MEASURE_MISSING = "E_MEASURE_MISSING"
ERR_MEASURE_UNKNOWN = "E_MEASURE_UNKNOWN"
ERR_PERIOD_COLUMN_MISSING = "E_PERIOD_COLUMN_MISSING"
ERR_WIDE_PERIOD_INCOMPLETE = "E_WIDE_PERIOD_INCOMPLETE"
ERR_HISTORY_OVERFLOW = "E_HISTORY_OVERFLOW"
ERR_NEED_DECISION = "E_NEED_DECISION"
ERR_HIERARCHY_INVALID = "E_HIERARCHY_INVALID"
ERR_ASSIGNMENT_CONFLICT = "E_ASSIGNMENT_CONFLICT"
ERR_REGRESSOR_FLAT = "E_REGRESSOR_FLAT"
ERR_REGRESSOR_INCOMPLETE_FUTURE = "E_REGRESSOR_INCOMPLETE_FUTURE"
ERR_REGRESSOR_COLLINEAR = "E_REGRESSOR_COLLINEAR"
ERR_REGRESSOR_UNAVAILABLE_AT_CUTOFF = "E_REGRESSOR_UNAVAILABLE_AT_CUTOFF"
ERR_SCENARIO_PRIORITY_TIE = "E_SCENARIO_PRIORITY_TIE"
ERR_SCENARIO_REG_FAMILY_DUP = "E_SCENARIO_REG_FAMILY_DUP"
ERR_SCENARIO_PARTIAL_NODE = "E_SCENARIO_PARTIAL_NODE"
ERR_SERIES_NO_PREDICTION = "E_SERIES_NO_PREDICTION"
ERR_MODEL_FAILED = "E_MODEL_FAILED"
WARN_PARTIAL_COVERAGE = "W_PARTIAL_COVERAGE"
WARN_OUTLIER = "W_OUTLIER"
WARN_SHORT_HISTORY = "W_SHORT_HISTORY"
WARN_ZERO_SERIES = "W_ZERO_SERIES"
WARN_INTERMITTENT = "W_INTERMITTENT"
WARN_MAT_DERIVED_NEGATIVE = "W_MAT_DERIVED_NEGATIVE"
INFO_ADJUSTED = "I_ADJUSTED"
INFO_IMPUTED = "I_IMPUTED"
INFO_EXCLUDED = "I_EXCLUDED"
INFO_WINSORIZED = "I_WINSORIZED"
INFO_FLOORED = "I_FLOORED"
INFO_LLONG_TERM = "I_LONG_TERM_AVISO"


def _canonical_json(obj: Any) -> str:
    """Serializa o objeto em JSON canonico (chaves ordenadas, sem espacos)."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def stable_id(*parts: object) -> str:
    """Hash SHA-256 estavel de uma lista ordenada de partes."""
    payload = _canonical_json(list(parts)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Schemas dos frames canonicos (secoes 7.3)
# ---------------------------------------------------------------------------

SCHEMA_OBSERVATIONS = {
    "source_row_id": pl.String,
    "entity_id": pl.String,
    "series_id": pl.String,
    "ds": pl.Date,
    "measure": pl.String,
    "y_raw": pl.Decimal(20, 2),
    "observed": pl.Boolean,
}

SCHEMA_ENTITIES = {
    "entity_id": pl.String,
    "key_json": pl.String,
    "dimensions_json": pl.String,
    "attributes_json": pl.String,
}

SCHEMA_PREPARED = {
    "entity_id": pl.String,
    "series_id": pl.String,
    "ds": pl.Date,
    "measure": pl.String,
    "y": pl.Float64,
    "observed": pl.Boolean,
    "was_adjusted": pl.Boolean,
    "adjustment_reason": pl.String,
}

SCHEMA_MODEL_INPUT = {
    "unique_id": pl.String,
    "ds": pl.Date,
    "y": pl.Float64,
}

SCHEMA_FUTURE_X = {
    "unique_id": pl.String,
    "ds": pl.Date,
}

SCHEMA_PREDICTIONS = {
    "node_id": pl.String,
    "entity_id": pl.String,
    "level": pl.String,
    "measure": pl.String,
    "scenario_id": pl.String,
    "ds": pl.Date,
    "yhat": pl.Float64,
    "lo80": pl.Float64,
    "hi80": pl.Float64,
    "model_alias": pl.String,
    "interval_method": pl.String,
    "status": pl.String,
}

SCHEMA_CV_PREDICTIONS = {
    "node_id": pl.String,
    "measure": pl.String,
    "model_alias": pl.String,
    "cutoff": pl.Date,
    "ds": pl.Date,
    "y_actual": pl.Float64,
    "yhat": pl.Float64,
    "evaluated": pl.Boolean,
    "failure_reason": pl.String,
}

SCHEMA_SCORES = {
    "node_id": pl.String,
    "measure": pl.String,
    "model_alias": pl.String,
    "mae": pl.Float64,
    "rmse": pl.Float64,
    "wape": pl.Float64,
    "bias": pl.Float64,
    "n_eval": pl.Int64,
    "n_folds": pl.Int64,
    "eligible": pl.Boolean,
    "failure_reason": pl.String,
}

SCHEMA_SELECTION = {
    "node_id": pl.String,
    "measure": pl.String,
    "model_alias": pl.String,
    "selection_reason": pl.String,
    "cv_horizon": pl.Int64,
    "n_folds": pl.Int64,
    "fallback_used": pl.Boolean,
    "clipped_count": pl.Int64,
}


def empty_long(id_col: str, n: int) -> pl.DataFrame:
    """Cria dataframe longo vazio com o numero minimo de linhas pedido."""
    return pl.DataFrame({id_col: [""] * n})


# ---------------------------------------------------------------------------
# Configuracoes (secao 7.1)
# ---------------------------------------------------------------------------


@dataclass
class StudyConfig:
    schema_version: int = SCHEMA_VERSION
    name: str = ""
    layout: Layout = Layout.LONG
    dimension_names: list[str] = field(default_factory=list)
    analysis_level: str = ""
    source_frequency: SourceFrequency = SourceFrequency.MONTHLY
    model_frequency: SourceFrequency = SourceFrequency.MONTHLY
    mat_mode: MatMode = MatMode.NONE
    measures: list[str] = field(default_factory=lambda: ["unidades"])
    currency: str = "BRL"
    history_end: date | None = None
    history_periods: int = 36

    def to_json(self) -> str:
        return _canonical_json(
            {
                "schema_version": self.schema_version,
                "name": self.name,
                "layout": self.layout.value,
                "dimension_names": list(self.dimension_names),
                "analysis_level": self.analysis_level,
                "source_frequency": self.source_frequency.value,
                "model_frequency": self.model_frequency.value,
                "mat_mode": self.mat_mode.value,
                "measures": list(self.measures),
                "currency": self.currency,
                "history_end": self.history_end.isoformat()
                if self.history_end
                else None,
                "history_periods": self.history_periods,
            }
        )

    @classmethod
    def from_json(cls, payload: str) -> "StudyConfig":
        d = json.loads(payload)
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            name=d.get("name", ""),
            layout=Layout(d.get("layout", "long")),
            dimension_names=list(d.get("dimension_names", [])),
            analysis_level=d.get("analysis_level", ""),
            source_frequency=SourceFrequency(d.get("source_frequency", "monthly")),
            model_frequency=SourceFrequency(d.get("model_frequency", "monthly")),
            mat_mode=MatMode(d.get("mat_mode", "none")),
            measures=list(d.get("measures", ["unidades"])),
            currency=d.get("currency", "BRL"),
            history_end=date.fromisoformat(d["history_end"])
            if d.get("history_end")
            else None,
            history_periods=int(d.get("history_periods", 36)),
        )

    def validate(self) -> list[str]:
        """Valida invariancias da secao 5; retorna lista de mensagens de erro."""
        errs: list[str] = []
        if not self.name.strip():
            errs.append("Nome do estudo é obrigatório.")
        if not self.dimension_names:
            errs.append("Informe pelo menos uma dimensão esperada.")
        if not self.analysis_level:
            errs.append("Nível de análise é obrigatório.")
        if self.analysis_level not in self.dimension_names:
            errs.append(
                f"Nível de análise '{self.analysis_level}' precisa estar nas dimensões."
            )
        if not self.measures:
            errs.append("Selecione pelo menos uma medida (Unidades ou Valor).")
        if len(self.measures) > MAX_MEASURES:
            errs.append(f"Suportadas no máximo {MAX_MEASURES} medidas por estudo.")
        for m in self.measures:
            if m not in ("unidades", "valor"):
                errs.append(f"Medida desconhecida: {m}")
        if self.history_end is None:
            errs.append("Informe o último período fechado.")
        if (
            self.source_frequency == SourceFrequency.MAT
            and self.mat_mode == MatMode.NONE
        ):
            errs.append("Frequência MAT exige mat_mode direto ou derivado.")
        if (
            self.source_frequency == SourceFrequency.MAT
            and self.mat_mode == MatMode.DERIVED
        ):
            errs.append(
                "MAT derivado parte de dados mensais; a fonte não pode ser MAT."
            )
        if (
            self.source_frequency != SourceFrequency.MAT
            and self.mat_mode == MatMode.DIRECT
        ):
            errs.append("MAT direto exige source_frequency = mat.")
        if self.model_frequency not in (
            self.source_frequency,
            SourceFrequency.MONTHLY,
            SourceFrequency.QUARTERLY,
            SourceFrequency.YEARLY,
        ):
            errs.append("Frequência de modelagem incompatível com a origem.")
        max_hist = self.max_history_periods()
        if self.history_periods > max_hist:
            errs.append(
                f"Histórico excede o máximo de {max_hist} períodos para a frequência."
            )
        return errs

    def max_history_periods(self) -> int:
        f = self.source_frequency
        return {
            SourceFrequency.MONTHLY: MAX_HISTORY_MONTHS,
            SourceFrequency.QUARTERLY: MAX_HISTORY_QUARTERS,
            SourceFrequency.YEARLY: MAX_HISTORY_YEARS,
            SourceFrequency.MAT: MAX_HISTORY_MAT,
        }.get(f, MAX_HISTORY_MONTHS)

    def max_horizon_periods(self) -> int:
        f = self.model_frequency
        return {
            SourceFrequency.MONTHLY: MAX_HORIZON_MONTHLY,
            SourceFrequency.QUARTERLY: MAX_HORIZON_QUARTERLY,
            SourceFrequency.YEARLY: MAX_HORIZON_YEARLY,
            SourceFrequency.MAT: MAX_HORIZON_MAT,
        }.get(f, MAX_HORIZON_MONTHLY)


@dataclass
class MappingConfig:
    schema_version: int = SCHEMA_VERSION
    key_columns: list[str] = field(default_factory=list)
    dimension_columns: dict[str, str] = field(default_factory=dict)
    attribute_columns: dict[str, str] = field(default_factory=dict)
    period_column: str | None = None
    measure_columns: dict[str, str] = field(default_factory=dict)
    measure_label_column: str | None = None
    wide_period_map: dict[str, str] = field(default_factory=dict)
    sheet_name: str | None = None
    delimiter: str = ";"
    encoding: str = "utf-8"
    decimal_separator: str = ","
    thousands_separator: str | None = None
    date_format: str = "YYYY-MM"
    key_json_order: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return _canonical_json(
            {
                "schema_version": self.schema_version,
                "key_columns": list(self.key_columns),
                "dimension_columns": dict(self.dimension_columns),
                "attribute_columns": dict(self.attribute_columns),
                "period_column": self.period_column,
                "measure_columns": dict(self.measure_columns),
                "measure_label_column": self.measure_label_column,
                "wide_period_map": dict(self.wide_period_map),
                "sheet_name": self.sheet_name,
                "delimiter": self.delimiter,
                "encoding": self.encoding,
                "decimal_separator": self.decimal_separator,
                "thousands_separator": self.thousands_separator,
                "date_format": self.date_format,
                "key_json_order": list(self.key_json_order),
            }
        )

    @classmethod
    def from_json(cls, payload: str) -> "MappingConfig":
        d = json.loads(payload)
        return cls(
            key_columns=list(d.get("key_columns", [])),
            dimension_columns=dict(d.get("dimension_columns", {})),
            attribute_columns=dict(d.get("attribute_columns", {})),
            period_column=d.get("period_column"),
            measure_columns=dict(d.get("measure_columns", {})),
            measure_label_column=d.get("measure_label_column"),
            wide_period_map=dict(d.get("wide_period_map", {})),
            sheet_name=d.get("sheet_name"),
            delimiter=d.get("delimiter", ";"),
            encoding=d.get("encoding", "utf-8"),
            decimal_separator=d.get("decimal_separator", ","),
            thousands_separator=d.get("thousands_separator"),
            date_format=d.get("date_format", "YYYY-MM"),
            key_json_order=list(d.get("key_json_order", [])),
        )


@dataclass
class TreatmentPolicy:
    schema_version: int = SCHEMA_VERSION
    duplicate_action: DuplicateAction = DuplicateAction.REJECT
    missing_action: MissingAction = MissingAction.EXCLUDE_SERIES
    negative_action: NegativeAction = NegativeAction.REJECT
    outlier_action: OutlierAction = OutlierAction.KEEP
    upper_quantile: float = DEFAULT_WINSORIZE_QUANTILE
    excluded_row_ids: list[str] = field(default_factory=list)
    excluded_series_ids: list[str] = field(default_factory=list)
    trim_window: tuple[date, date] | None = None
    currency_rounding_confirmed: bool = False
    dimension_resolution: str = ""  # "static" ou descrição da decisão
    justification: str = ""

    def to_json(self) -> str:
        return _canonical_json(
            {
                "schema_version": self.schema_version,
                "duplicate_action": self.duplicate_action.value,
                "missing_action": self.missing_action.value,
                "negative_action": self.negative_action.value,
                "outlier_action": self.outlier_action.value,
                "upper_quantile": self.upper_quantile,
                "excluded_row_ids": list(self.excluded_row_ids),
                "excluded_series_ids": list(self.excluded_series_ids),
                "trim_window": [
                    self.trim_window[0].isoformat(),
                    self.trim_window[1].isoformat(),
                ]
                if self.trim_window
                else None,
                "currency_rounding_confirmed": self.currency_rounding_confirmed,
                "dimension_resolution": self.dimension_resolution,
                "justification": self.justification,
            }
        )

    @classmethod
    def from_json(cls, payload: str) -> "TreatmentPolicy":
        d = json.loads(payload)
        tw = d.get("trim_window")
        return cls(
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            duplicate_action=DuplicateAction(d.get("duplicate_action", "reject")),
            missing_action=MissingAction(d.get("missing_action", "exclude_series")),
            negative_action=NegativeAction(d.get("negative_action", "reject")),
            outlier_action=OutlierAction(d.get("outlier_action", "keep")),
            upper_quantile=float(d.get("upper_quantile", DEFAULT_WINSORIZE_QUANTILE)),
            excluded_row_ids=list(d.get("excluded_row_ids", [])),
            excluded_series_ids=list(d.get("excluded_series_ids", [])),
            trim_window=(
                (date.fromisoformat(tw[0]), date.fromisoformat(tw[1])) if tw else None
            ),
            currency_rounding_confirmed=bool(
                d.get("currency_rounding_confirmed", False)
            ),
            dimension_resolution=d.get("dimension_resolution", ""),
            justification=d.get("justification", ""),
        )


@dataclass
class HierarchyConfig:
    schema_version: int = SCHEMA_VERSION
    mode: HierarchyMode = HierarchyMode.INDEPENDENT
    ordered_levels: list[list[str]] = field(default_factory=list)
    forecast_level: list[str] = field(default_factory=list)
    include_total: bool = True

    def to_json(self) -> str:
        return _canonical_json(
            {
                "schema_version": self.schema_version,
                "mode": self.mode.value,
                "ordered_levels": [list(lv) for lv in self.ordered_levels],
                "forecast_level": list(self.forecast_level),
                "include_total": self.include_total,
            }
        )


@dataclass
class ForecastConfig:
    schema_version: int = SCHEMA_VERSION
    horizon_periods: int = 12
    mode: ForecastMode = ForecastMode.FAST
    candidate_aliases: list[str] = field(default_factory=list)
    enable_ml: bool = False
    cv_horizon: int | None = None
    cv_windows: int = DEFAULT_CV_WINDOWS
    batch_size: int = DEFAULT_BATCH_SIZE
    n_jobs: int = DEFAULT_N_JOBS
    nonnegative_output: bool = True
    interval_level: int = DEFAULT_INTERVAL_LEVEL
    seed: int = DEFAULT_SEED
    hierarchy: HierarchyConfig | None = None
    regressor_ids: list[str] = field(default_factory=list)
    scenario_ids: list[str] = field(default_factory=lambda: ["base"])
    season_length: int | None = None

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.horizon_periods < 1:
            errs.append("Horizonte deve ser ao menos 1 período.")
        if self.season_length is not None and self.season_length < 1:
            errs.append("Comprimento da sazonalidade deve ser ao menos 1 período.")
        if not self.nonnegative_output:
            errs.append(
                "Piso zero é obrigatório; não é possível desativar nonnegative_output."
            )
        if not 50 <= self.interval_level < 100:
            errs.append("interval_level deve estar entre 50 e 99.")
        if self.batch_size < 1 or self.batch_size > MAX_BATCH_SIZE:
            errs.append(f"batch_size deve estar entre 1 e {MAX_BATCH_SIZE}.")
        if self.n_jobs < 1 or self.n_jobs > MAX_N_JOBS:
            errs.append(f"n_jobs deve estar entre 1 e {MAX_N_JOBS}.")
        if self.cv_horizon is not None and self.cv_horizon < 1:
            errs.append("cv_horizon deve ser positivo.")
        if len(self.candidate_aliases) > 5:
            errs.append("Selecione no máximo 5 métodos para comparar.")
        return errs

    def to_json(self) -> str:
        h = self.hierarchy.to_json() if self.hierarchy else None
        return _canonical_json(
            {
                "schema_version": self.schema_version,
                "horizon_periods": self.horizon_periods,
                "mode": self.mode.value,
                "candidate_aliases": list(self.candidate_aliases),
                "enable_ml": self.enable_ml,
                "cv_horizon": self.cv_horizon,
                "cv_windows": self.cv_windows,
                "batch_size": self.batch_size,
                "n_jobs": self.n_jobs,
                "nonnegative_output": self.nonnegative_output,
                "interval_level": self.interval_level,
                "seed": self.seed,
                "hierarchy": h,
                "regressor_ids": list(self.regressor_ids),
                "scenario_ids": list(self.scenario_ids),
                "season_length": self.season_length,
            }
        )

    @classmethod
    def from_json(cls, payload: str) -> "ForecastConfig":
        d = json.loads(payload)
        return cls(
            horizon_periods=int(d.get("horizon_periods", 24)),
            mode=ForecastMode(d.get("mode", "fast")),
            candidate_aliases=list(d.get("candidate_aliases", [])),
            enable_ml=bool(d.get("enable_ml", False)),
            cv_horizon=d.get("cv_horizon"),
            cv_windows=int(d.get("cv_windows", 3)),
            batch_size=int(d.get("batch_size", DEFAULT_BATCH_SIZE)),
            n_jobs=int(d.get("n_jobs", DEFAULT_N_JOBS)),
            nonnegative_output=bool(d.get("nonnegative_output", True)),
            interval_level=int(d.get("interval_level", DEFAULT_INTERVAL_LEVEL)),
            seed=int(d.get("seed", DEFAULT_SEED)),
            hierarchy=None,
            regressor_ids=list(d.get("regressor_ids", [])),
            scenario_ids=list(d.get("scenario_ids", ["base"])),
            season_length=d.get("season_length"),
        )


@dataclass
class ScenarioConfig:
    scenario_id: str = "base"
    name: str = "Base"
    description: str = "Cenário base (imutável)."
    rules: list[Any] = field(default_factory=list)  # list[AssumptionRule]
    regressor_future_overrides: dict[str, dict[str, float]] = field(
        default_factory=dict
    )

    def to_json(self) -> str:
        return _canonical_json(
            {
                "scenario_id": self.scenario_id,
                "name": self.name,
                "description": self.description,
                "rules": [
                    r.to_json_dict() if hasattr(r, "to_json_dict") else dict(r)
                    for r in self.rules
                ],
                "regressor_future_overrides": self.regressor_future_overrides,
            }
        )

    @classmethod
    def from_json(cls, payload: str) -> "ScenarioConfig":
        d = json.loads(payload)
        return cls(
            scenario_id=d.get("scenario_id", "base"),
            name=d.get("name", "Base"),
            description=d.get("description", ""),
            rules=[
                AssumptionRule.from_json_dict(r)
                for r in d.get("rules", [])
                if isinstance(r, dict)
            ],
            regressor_future_overrides=dict(d.get("regressor_future_overrides", {})),
        )


@dataclass
class AssumptionRule:
    rule_id: str = ""
    scenario_id: str = "base"
    family: str = ""
    target_measure: str = "valor"
    filters: dict[str, list[str]] = field(default_factory=dict)
    excluded_entity_ids: list[str] = field(default_factory=list)
    start_period: date | None = None
    end_period: date | None = None
    effect: EffectType = EffectType.STEP
    rate: float = 0.0
    priority: int = 0
    enabled: bool = True

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "scenario_id": self.scenario_id,
            "family": self.family,
            "target_measure": self.target_measure,
            "filters": self.filters,
            "excluded_entity_ids": list(self.excluded_entity_ids),
            "start_period": self.start_period.isoformat()
            if self.start_period
            else None,
            "end_period": self.end_period.isoformat() if self.end_period else None,
            "effect": self.effect.value,
            "rate": self.rate,
            "priority": self.priority,
            "enabled": self.enabled,
        }

    @classmethod
    def from_json_dict(cls, d: dict[str, Any]) -> "AssumptionRule":
        return cls(
            rule_id=d.get("rule_id", ""),
            scenario_id=d.get("scenario_id", "base"),
            family=d.get("family", ""),
            target_measure=d.get("target_measure", "valor"),
            filters=dict(d.get("filters", {})),
            excluded_entity_ids=list(d.get("excluded_entity_ids", [])),
            start_period=date.fromisoformat(d["start_period"])
            if d.get("start_period")
            else None,
            end_period=date.fromisoformat(d["end_period"])
            if d.get("end_period")
            else None,
            effect=EffectType(d.get("effect", "step")),
            rate=float(d.get("rate", 0.0)),
            priority=int(d.get("priority", 0)),
            enabled=bool(d.get("enabled", True)),
        )


def validate_assumption_rule(rule: "AssumptionRule") -> list[str]:
    """Valida uma regra isolada antes de persistir (sec 10.1)."""
    errs: list[str] = []
    if not rule.rule_id.strip():
        errs.append("Regra sem identificador.")
    if rule.scenario_id == "base":
        errs.append("O cenário base não aceita regras.")
    if not rule.family.strip():
        errs.append("Informe a família da regra.")
    if rule.target_measure not in ("unidades", "valor"):
        errs.append("Medida-alvo deve ser 'unidades' ou 'valor'.")
    if rule.rate <= -1.0:
        errs.append("Taxa deve ser maior que -100%.")
    if rule.effect == EffectType.PULSE and rule.end_period is None:
        errs.append("Efeito pulse exige período final.")
    if rule.end_period and rule.start_period and rule.end_period < rule.start_period:
        errs.append("Período final anterior ao início.")
    if rule.priority < 0:
        errs.append("Prioridade deve ser não negativa.")
    return errs


def validate_scenario_draft(scenario: "ScenarioConfig") -> list[str]:
    """Valida rascunho de cenário, preservando o cenário base sem regras."""
    errs: list[str] = []
    if not scenario.scenario_id.strip():
        errs.append("Cenário sem identificador.")
    if scenario.scenario_id == "base" and scenario.rules:
        errs.append("O cenário base é imutável e não pode ter regras.")
    for r in scenario.rules:
        if hasattr(r, "rule_id"):
            errs.extend(validate_assumption_rule(r))
    return errs


@dataclass
class RegressorSpec:
    regressor_id: str = ""
    name: str = ""
    scope_filters: dict[str, list[str]] = field(default_factory=dict)
    unit: str = ""
    known_in_advance: bool = True
    history_values: dict[str, float] = field(default_factory=dict)
    future_values: dict[str, float] = field(default_factory=dict)
    fill_policy: RegressorFillPolicy = RegressorFillPolicy.NONE
    enabled: bool = True

    def to_json(self) -> str:
        return _canonical_json(
            {
                "regressor_id": self.regressor_id,
                "name": self.name,
                "scope_filters": self.scope_filters,
                "unit": self.unit,
                "known_in_advance": self.known_in_advance,
                "history_values": self.history_values,
                "future_values": self.future_values,
                "fill_policy": self.fill_policy.value,
                "enabled": self.enabled,
            }
        )

    @classmethod
    def from_json(cls, payload: str) -> "RegressorSpec":
        d = json.loads(payload)
        return cls(
            regressor_id=d.get("regressor_id", ""),
            name=d.get("name", ""),
            scope_filters=dict(d.get("scope_filters", {})),
            unit=d.get("unit", ""),
            known_in_advance=bool(d.get("known_in_advance", True)),
            history_values=dict(d.get("history_values", {})),
            future_values=dict(d.get("future_values", {})),
            fill_policy=RegressorFillPolicy(d.get("fill_policy", "none")),
            enabled=bool(d.get("enabled", True)),
        )


@dataclass
class ExportConfig:
    schema_version: int = SCHEMA_VERSION
    format: ExportFormat = ExportFormat.CSV
    layout: Literal["long", "wide"] = "long"
    scenario_id: str = "base"
    measure: str | None = None
    level: str | None = None
    model_alias: str | None = None
    filters: dict[str, list[str]] = field(default_factory=dict)
    temporal_view: TemporalView = TemporalView.CANONICAL
    include_intervals: bool = True
    round_units: bool = False

    def to_json(self) -> str:
        return _canonical_json(
            {
                "schema_version": self.schema_version,
                "format": self.format.value,
                "layout": self.layout,
                "scenario_id": self.scenario_id,
                "measure": self.measure,
                "level": self.level,
                "model_alias": self.model_alias,
                "filters": self.filters,
                "temporal_view": self.temporal_view.value,
                "include_intervals": self.include_intervals,
                "round_units": self.round_units,
            }
        )


# ---------------------------------------------------------------------------
# Objetos de retorno (secao 7.2)
# ---------------------------------------------------------------------------


@dataclass
class ValidationIssue:
    code: str
    severity: Severity
    message: str
    source_row_ids: list[str] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)
    field_name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileInspection:
    sheet_names: list[str]
    columns: list[str]
    sample: pl.DataFrame
    suggested_types: dict[str, str]
    warnings: list[ValidationIssue]
    options: dict[str, Any]


@dataclass
class CanonicalDataset:
    observations: pl.DataFrame  # SCHEMA_OBSERVATIONS
    entities: pl.DataFrame  # SCHEMA_ENTITIES
    source_rows: pl.DataFrame  # tipo de schema livre (raw_json)
    issues: list[ValidationIssue]
    fingerprint: str
    history_start: date | None = None
    history_end: date | None = None

    def to_json(self) -> str:
        return _canonical_json(
            {
                "n_obs": self.observations.height,
                "n_entities": self.entities.height,
                "n_issues": len(self.issues),
                "fingerprint": self.fingerprint,
                "history_start": self.history_start.isoformat()
                if self.history_start
                else None,
                "history_end": self.history_end.isoformat()
                if self.history_end
                else None,
            }
        )


@dataclass
class ProfileReport:
    summary: dict[str, Any]
    by_series: pl.DataFrame
    issues: list[ValidationIssue]
    blocked: bool
    coverage: dict[str, Any]


@dataclass
class PreparedDataset:
    prepared: pl.DataFrame  # SCHEMA_PREPARED
    raw_series: pl.DataFrame  # serie origem normalizada (não tratada)
    adjustments: pl.DataFrame
    eligible_series: list[str]
    excluded_series: list[str]
    preparation_id: str
    policy: TreatmentPolicy
    cutoff: date | None = None


@dataclass
class RunEvent:
    stage: RunStage
    completed: int
    total: int
    message: str
    batch_id: str | None = None
    payload: dict[str, Any] | None = None


@dataclass
class ForecastBatch:
    predictions: pl.DataFrame  # SCHEMA_PREDICTIONS
    cv_predictions: pl.DataFrame  # SCHEMA_CV_PREDICTIONS
    scores: pl.DataFrame  # SCHEMA_SCORES
    selection: pl.DataFrame  # SCHEMA_SELECTION
    issues: list[ValidationIssue]
    scenario_factors: pl.DataFrame | None = None  # rastro compilado por cenário


@dataclass
class RunSummary:
    run_id: str
    status: RunStatus
    counts: dict[str, int]
    durations: dict[str, float]
    coverage: dict[str, Any]
    warnings: list[ValidationIssue]
    config_hash: str


@dataclass
class ExportArtifact:
    filename: str
    mime_type: str
    bytes_or_path: bytes | Path
    row_count: int
    metadata: dict[str, Any]


@dataclass
class CVWindow:
    cutoff: date
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    eval_dates: list[date]


@dataclass
class ModelSpec:
    alias: str
    params: dict[str, Any]
    supports_regressors: bool = False
    supports_intervals: bool = False
    min_train: int = 3
    min_seasonal_cycles: int = 2
    intermittent: bool = False
    requires_positives: bool = True
    baseline: bool = False
    global_model: bool = False
    need_ml: bool = False
    rank: int = 100


@dataclass
class SelectionResult:
    selection: pl.DataFrame  # SCHEMA_SELECTION
    ranking_notes: dict[str, str]
    median_n_folds: int = 0


@dataclass
class RunInputs:
    run_id: str
    dataset_id: str
    preparation_id: str
    study: StudyConfig
    mapping: MappingConfig
    policy: TreatmentPolicy
    config: ForecastConfig
    scenarios: list[ScenarioConfig]
    regressors: list[RegressorSpec]
    prepared: pl.DataFrame  # SCHEMA_PREPARED
    raw_series: pl.DataFrame
    entities: pl.DataFrame  # SCHEMA_ENTITIES
    # Callback opcional consultado entre lotes (não dentro de uma chamada
    # estatística). Quando retorna True, o motor interrompe a rodada, emite
    # RunStage.CANCELLED com um ForecastBatch parcial e encerra. A UI injeta
    # aqui seu flag de cancelamento (sec 4 e 8.2).
    should_cancel: Callable[[], bool] | None = None


@dataclass
class ResultFilter:
    run_id: str
    node_level: str | None = None
    node_id: str | None = None
    measure: str | None = None
    scenario_id: str = "base"
    model_alias: str | None = None
    ds_start: date | None = None
    ds_end: date | None = None
    dimensions: dict[str, list[str]] = field(default_factory=dict)
    search: str = ""


@dataclass
class DashboardData:
    """Agrupamento tipado dos resultados de uma rodada para o dashboard (sec 12.1).

    `predictions`, `scores` e `nodes` já vêm filtrados por `ResultFilter`.
    Os campos extras (P32) só são populados quando o filtro define uma **view
    única** (medida e cenário): `cards` (totais/variação/window UUID e MAT),
    `metrics` (backtest agregado por modelo), `history` (histórico preparado
    por nó) e `mat_view` (posições MAT no mesmo layout de `predictions`).
    """

    run: RunSummary
    nodes: pl.DataFrame
    predictions: pl.DataFrame
    scores: pl.DataFrame
    issues: list[ValidationIssue]
    cards: pl.DataFrame = field(default_factory=pl.DataFrame)
    metrics: pl.DataFrame = field(default_factory=pl.DataFrame)
    history: pl.DataFrame = field(default_factory=pl.DataFrame)
    mat_view: pl.DataFrame = field(default_factory=pl.DataFrame)
