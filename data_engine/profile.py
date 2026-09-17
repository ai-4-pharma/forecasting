"""data_engine.profile — Profiling de qualidade (P11) e preparação com política de tratamento (P12).
(extraído de data_engine.py single-file; split 16/09/2026).
"""

from __future__ import annotations

from datetime import date
import numpy as np
import polars as pl
from contracts import CanonicalDataset, DuplicateAction, MissingAction, NegativeAction, OutlierAction, PreparedDataset, ProfileReport, Severity, StudyConfig, TreatmentPolicy, empty_long, stable_id
from .dates import build_time_grid



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
