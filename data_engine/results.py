"""data_engine.results — Consultas de resultados, views de dashboard e candidatos sob demanda (P32).
(extraído de data_engine.py single-file; split 16/09/2026).
"""

from __future__ import annotations

from datetime import date
import json
import polars as pl
from contracts import (
    DashboardData,
    ForecastConfig,
    MatMode,
    ResultFilter,
    RunStatus,
    RunSummary,
    Severity,
    StudyConfig,
    ValidationIssue,
)
from .dates import _shift_period
from .db import _now_ts


_CANDIDATE_MODEL_ALIASES = {
    "Naive",
    "HistoricAverage",
    "SeasonalNaive",
    "AutoETS",
    "AutoTheta",
    "CrostonSBA",
    "TSB",
    "AutoCES",
    "AutoARIMA",
    "ZeroBaseline",
}


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
    if filters.model_alias:
        q += " AND f.model_alias = ?"
        params.append(filters.model_alias)
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
    """Métricas de backtest agregadas POR MODELO no recorte (sec 9.3 e 12.1).

    Não há mais "modelo vencedor": a rodada executa todos os modelos que o
    usuário escolheu. Para cada `model_alias` presente nas previsões do
    recorte, agrega os pares avaliados (`evaluated` e com y_actual) da própria
    previsão de teste desse modelo; WAPE é soma de numeradores/soma de
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
    evals_by_model: dict[str, list[dict]] = {}
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
        evals_by_model.setdefault(alias, []).append(
            {
                "node_id": str(nid),
                "cutoff": cutoff,
                "ds": ds,
                "y_actual": float(y_actual),
                "yhat": float(yhat),
            }
        )
    if not evals_by_model:
        return pl.DataFrame({c: [] for c in cols})
    rows: list[dict] = []
    for alias, evals in sorted(evals_by_model.items()):
        df = pl.DataFrame(evals).with_columns(
            (pl.col("yhat") - pl.col("y_actual")).alias("e")
        )
        denom = float(df["y_actual"].abs().sum())
        n_folds = int(
            df.group_by("node_id").agg(pl.col("cutoff").n_unique())["cutoff"].sum()
        )
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
        rows.append(
            {
                "node_id": "",
                "measure": measure,
                "model_alias": alias,
                "mae": mae,
                "rmse": rmse,
                "wape": wape,
                "bias": bias,
                "n_eval": df.height,
                "n_folds": n_folds,
                "cv_horizon": cv_horizon,
            }
        )
    return pl.DataFrame(rows)


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
    config = (
        ForecastConfig.from_json(cfg_row[0])
        if cfg_row and cfg_row[0]
        else ForecastConfig()
    )

    series = (
        _load_prepared_history(conn, run_id, measure)
        .filter(pl.col("series_id") == node_id)
        .sort("ds")
    )
    if series.height == 0:
        return _empty_candidate_rows(), "no_encontrado_ou_nao_e_folha"

    rows, status = forecast_engine.forecast_candidate(
        series, model_alias, study, config
    )
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

    Filtros: planície de medidas/cenário/nível/nó/modelo, faixa de datas,
    busca e dimensões reais. Quando `filters.measure` e `filters.scenario_id`
    definem uma view única, preenche `cards` (totais/variação/MAT), `metrics`
    (backtest agregado por modelo), `history` e `mat_view`. Os frames vêm já
    filtrados; nenhuma soma cruza medida/nível/cenário/modelo.
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
        # cards/MAT são por MODELO de referência (a soma de yhat não pode
        # misturar modelos); sem filtro explícito, toma o primeiro disponível.
        ref_model = filters.model_alias or (
            str(preds["model_alias"].sort().first())
            if "model_alias" in preds.columns
            else None
        )
        view_preds = (
            preds.filter(pl.col("model_alias") == ref_model)
            if ref_model is not None
            else preds
        )
        history = _view_history(conn, run_id, node_ids, measure)
        if study.mat_mode in (MatMode.DERIVED, MatMode.DIRECT):
            if study.mat_mode == MatMode.DERIVED:
                mat_view = _mat_view_derived(view_preds, history, measure, scenario)
            else:
                mat_view = _mat_view_direct(view_preds)
        cards = _view_cards(
            view_preds, history, mat_view, study, measure, scenario, level
        )
        metrics = _aggregate_backtest(conn, run_id, node_ids, measure, scenario)
    return DashboardData(
        summary, nodes, preds, scores, issues, cards, metrics, history, mat_view
    )
