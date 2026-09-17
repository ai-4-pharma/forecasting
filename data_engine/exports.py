"""data_engine.exports — Exportação CSV/XLSX (P34/P35): long/largo, métricas e qualidade.
(extraído de data_engine.py single-file; split 16/09/2026).
"""

from __future__ import annotations

import io
import json
import polars as pl
import openpyxl
from contracts import (
    ExportArtifact,
    ExportConfig,
    ExportFormat,
    ForecastConfig,
    MappingConfig,
    MatMode,
    RegressorSpec,
    ResultFilter,
    ScenarioConfig,
    StudyConfig,
    TemporalView,
)
from .results import (
    _mat_view_derived,
    _query_issues,
    _query_nodes,
    _query_predictions,
    _query_scores,
    _view_history,
    list_run_measures,
    load_run_study,
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
        model_alias=config.model_alias,
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
    base_val: dict[tuple[str, str, object, str], float],
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
                    {
                        "node_id": nid,
                        "ds": d,
                        "model_alias": a,
                        "yhat_base": v,
                    }
                    for (nid, m, d, a), v in base_val.items()
                    if m == measure
                ]
            )
            if bm.height:
                fr = fr.drop("yhat_base").join(
                    bm, on=["node_id", "ds", "model_alias"], how="left"
                )
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
    leaf_vals: dict[tuple[str, str, str], float] = {}
    for r in forecast.to_dicts():
        eid = r.get("entity_id")
        alias = r.get("model_alias") or "Naive"
        if r.get("level") == "folha" and eid:
            leaf_vals[(eid, r["ds"].isoformat(), alias)] = round(float(r["valor"]), 0)

    new_rows: list[dict] = []
    for r in forecast.to_dicts():
        row = dict(r)
        alias = r.get("model_alias") or "Naive"
        if r.get("level") == "folha":
            row["valor"] = round(float(r["valor"]), 0)
        else:
            eids = coverage.get(r["node_id"])
            if eids:
                total = 0.0
                ok = True
                for eid in eids:
                    v = leaf_vals.get((eid, r["ds"].isoformat(), alias))
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

    base_val: dict[tuple[str, str, object, str], float] = {}
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
                base_val[(r["node_id"], r["measure"], r["ds"], r["model_alias"])] = (
                    float(r["yhat"])
                )

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
    O layout largo é UMA série por nó: em rodadas multi-modelo, a exportação
    segue o `model_alias` do recorte (método de referência); o longo mantém
    todos os métodos para rastreabilidade.
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
