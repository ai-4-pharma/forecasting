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
    ).pl()
    if winners.is_empty():
        return pl.DataFrame({c: [] for c in cols})
    evals = conn.execute(
        "SELECT node_id, model_alias, cutoff, ds, y_actual, yhat FROM cv_results"
        " WHERE run_id = ? AND measure = ? AND evaluated"
        " AND y_actual IS NOT NULL AND yhat IS NOT NULL",
        [run_id, measure],
    ).pl()
    if evals.is_empty():
        return pl.DataFrame({c: [] for c in cols})
    evals = (
        evals.with_columns(pl.col("node_id").cast(pl.Utf8))
        .join(
            winners.with_columns(pl.col("node_id").cast(pl.Utf8)),
            on=["node_id", "model_alias"],
            how="inner",
        )
        .with_columns((pl.col("yhat") - pl.col("y_actual")).alias("e"))
    )
    if evals.is_empty():
        return pl.DataFrame({c: [] for c in cols})

    folds = (
        evals.group_by(["model_alias", "node_id"])
        .agg(pl.col("cutoff").n_unique().alias("_n_folds_node"))
        .group_by("model_alias")
        .agg(pl.col("_n_folds_node").sum().alias("n_folds"))
    )
    cv_h = (
        evals.group_by(["model_alias", "node_id", "cutoff"])
        .agg(pl.len().alias("_n"))
        .group_by(["model_alias", "node_id"])
        .agg(pl.col("_n").max().alias("_h"))
        .group_by("model_alias")
        .agg(pl.col("_h").max().alias("cv_horizon"))
    )
    agg = (
        evals.group_by("model_alias")
        .agg(
            pl.col("e").abs().mean().alias("mae"),
            (pl.col("e") ** 2).mean().sqrt().alias("rmse"),
            pl.col("e").abs().sum().alias("_num"),
            pl.col("y_actual").abs().sum().alias("_den"),
            pl.col("e").mean().alias("bias"),
            pl.len().alias("n_eval"),
        )
        .with_columns(
            pl.when(pl.col("_den") > 0)
            .then(pl.col("_num") / pl.col("_den"))
            .otherwise(None)
            .alias("wape")
        )
        .drop(["_num", "_den"])
    )
    return (
        agg.join(folds, on="model_alias", how="left")
        .join(cv_h, on="model_alias", how="left")
        .with_columns(
            pl.lit("").alias("node_id"),
            pl.lit(measure).alias("measure"),
            pl.col("n_folds").fill_null(0).cast(pl.Int64),
            pl.col("cv_horizon").fill_null(0).cast(pl.Int64),
        )
        .select(cols)
        .sort("model_alias")
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
    """Histórico preparado por nó (soma das folhas sob ele) para a view.

    Vetorizado (S1.5): explode `coverage_json` uma vez em `node->entidade` e
    faz 1 join + 1 group_by para todos os nós pedidos, em vez de 1 filtro/nó.
    """
    cols = ["node_id", "measure", "ds", "y", "observed", "was_adjusted"]
    if not node_ids:
        return pl.DataFrame({c: [] for c in cols})
    prep = _load_prepared_history(conn, run_id, measure)
    if prep.height == 0:
        return pl.DataFrame({c: [] for c in cols})
    node_entities = _node_entity_map(conn, run_id)
    wanted = set(node_ids)
    map_rows = [
        {"node_id": nid, "entity_id": eid}
        for nid in node_ids
        for eid in (node_entities.get(nid) or ())
    ]
    fallback_ids = [nid for nid in node_ids if not node_entities.get(nid)]

    parts: list[pl.DataFrame] = []
    if map_rows:
        map_df = pl.DataFrame(map_rows)
        agg = (
            prep.join(map_df, on="entity_id", how="inner")
            .filter(pl.col("node_id").is_in(list(wanted)))
            .group_by(["node_id", "ds", "measure"])
            .agg(
                pl.col("y").sum().alias("y"),
                pl.col("observed").all().alias("observed"),
                pl.col("was_adjusted").any().alias("was_adjusted"),
            )
        )
        if agg.height:
            parts.append(agg.select(cols))
    if fallback_ids:
        leaf = prep.filter(pl.col("series_id").is_in(fallback_ids)).select(
            pl.col("series_id").alias("node_id"),
            "measure",
            "ds",
            "y",
            "observed",
            "was_adjusted",
        )
        if leaf.height:
            parts.append(leaf.select(cols))
    if not parts:
        return pl.DataFrame({c: [] for c in cols})
    return pl.concat(parts).sort(["node_id", "ds"])


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


def _leaf_dim_frame(leaves: pl.DataFrame, dims: list[str]) -> pl.DataFrame:
    """`node_id` + 1 coluna por dimensão, lidas de `dimensions_json` (S2.3).

    Loop em Python sobre as linhas-folha (contagem de séries, não de
    observações — não é o loop por linha de dado que S1.5 proíbe) porque
    `pl.Expr.str.json_decode` exige um `dtype` fixo por coluna nesta versão
    do polars, incompatível com dimensões de nomes variáveis por estudo.
    """
    rows: list[dict] = []
    for r in leaves.to_dicts():
        try:
            d = json.loads(r["dimensions_json"] or "{}")
        except Exception:  # noqa: BLE001
            d = {}
        row = {"node_id": r["node_id"]}
        for name in dims:
            row[name] = str(d.get(name, "") or "")
        rows.append(row)
    if not rows:
        return pl.DataFrame({"node_id": [], **{d: [] for d in dims}})
    return pl.DataFrame(rows)


def build_run_tree(conn, run_id: str, study: StudyConfig) -> dict:
    """Árvore de dimensões para a tela única (S2.3).

    Agrupa as linhas-folha de `run_nodes` (sempre presentes, com ou sem
    hierarquia configurada — S2.3/exploração) por `study.dimension_names`, na
    ordem do estudo, contando séries por nó. Não depende de `parent_node_id`
    porque, no caso comum (sem `HierarchyConfig`), `run_nodes` só tem folhas.
    """
    leaves = conn.execute(
        "SELECT node_id, dimensions_json FROM run_nodes"
        " WHERE run_id = ? AND level = 'folha'",
        [run_id],
    ).pl()
    if leaves.height == 0:
        raise KeyError(f"Rodada {run_id} sem nós persistidos")
    dims = list(study.dimension_names)
    parsed = _leaf_dim_frame(leaves, dims)
    counts = parsed.group_by(dims).agg(pl.col("node_id").n_unique().alias("n_series"))

    root: dict = {
        "name": study.name or "Total",
        "n_series": int(counts["n_series"].sum()),
        "children": {},
    }
    for row in counts.to_dicts():
        node = root
        for d in dims:
            val = str(row.get(d) or "(vazio)")
            children = node["children"]
            if val not in children:
                children[val] = {"name": val, "dim": d, "n_series": 0, "children": {}}
            node = children[val]
            node["n_series"] += row["n_series"]

    def _to_list(n: dict) -> dict:
        out = {k: v for k, v in n.items() if k != "children"}
        out["children"] = [_to_list(c) for c in n["children"].values()]
        return out

    return _to_list(root)


def _cv_selected_errors(
    conn, run_id: str, node_ids: list[str], measure: str, selected_scores: pl.DataFrame
) -> pl.DataFrame:
    """Erros de backtest só do modelo `selected` de cada nó (S2.3/`series_view`).

    Mesma fonte (`cv_results`) e mesma convenção de WAPE (soma/soma, não média
    de percentuais) de `_aggregate_backtest`, mas restrita ao vencedor por nó
    em vez de agregar por `model_alias` inteiro.
    """
    empty = pl.DataFrame({"e": [], "y_actual": []})
    if not node_ids or selected_scores.height == 0:
        return empty
    q = (
        "SELECT node_id, model_alias, y_actual, yhat FROM cv_results"
        " WHERE run_id = ? AND measure = ? AND evaluated"
        " AND y_actual IS NOT NULL AND yhat IS NOT NULL"
        f" AND node_id IN ({','.join('?' * len(node_ids))})"
    )
    cv = conn.execute(q, [run_id, measure, *node_ids]).pl()
    if cv.height == 0:
        return empty
    joined = cv.join(
        selected_scores.select("node_id", "model_alias"),
        on=["node_id", "model_alias"],
        how="inner",
    )
    if joined.height == 0:
        return empty
    return joined.with_columns((pl.col("yhat") - pl.col("y_actual")).alias("e"))


def series_view(
    conn,
    run_id: str,
    dim: str,
    value: str,
    measure: str = "unidades",
    scenario: str = "base",
    model_alias: str | None = None,
) -> dict:
    """Série agregada de todas as folhas sob `dim=value` (S2.3).

    Soma simples das folhas (mesma regra de `page_dashboard`/`_view_history`,
    nunca soma pai+filho); `baseline` usa o modelo `selected` (S1.7) de CADA
    folha, não um único "modelo de referência" global como no wizard antigo —
    o bottom-up preserva a melhor escolha por série.

    `model_alias` (S2.8, Aba 3): quando informado, ignora o `selected` por
    folha e usa esse alias em todas — resposta a "e se eu usasse sempre o
    método X neste item", sem precisar rerrodar a previsão.
    """
    study = load_run_study(conn, run_id)
    if dim not in study.dimension_names:
        raise KeyError(
            f"Dimensão '{dim}' não existe no estudo (opções: {study.dimension_names})"
        )
    leaves = conn.execute(
        "SELECT node_id, dimensions_json FROM run_nodes"
        " WHERE run_id = ? AND level = 'folha'",
        [run_id],
    ).pl()
    if leaves.height == 0:
        raise KeyError(f"Rodada {run_id} sem nós persistidos")
    parsed = _leaf_dim_frame(leaves, [dim])
    node_ids = parsed.filter(pl.col(dim) == value)["node_id"].to_list()
    if not node_ids:
        raise KeyError(f"Nenhuma série para {dim}={value}")

    history = _view_history(conn, run_id, node_ids, measure)
    hist_agg = (
        history.group_by("ds").agg(pl.col("y").sum().alias("y")).sort("ds")
        if history.height
        else pl.DataFrame({"ds": [], "y": []})
    )

    scores = _query_scores(conn, run_id, node_ids, measure)
    if model_alias:
        selected_scores = (
            scores.filter(pl.col("model_alias") == model_alias) if scores.height else scores
        )
    else:
        selected_scores = scores.filter(pl.col("selected")) if scores.height else scores
        # Quando o usuário restringe `candidate_aliases` (S2.6+), o motor
        # (forecast_engine/cv.py, decisão do S1.7) marca `selected=True` em
        # TODOS os aliases pedidos por série — de propósito, para nunca
        # eleger vencedor às escondidas (`dev/test_pipeline.py::
        # test_b11_recomendacao_automatica_por_serie` trava esse
        # comportamento no motor). Mas esta Aba 1 ("Projeção do item") só
        # tem espaço para 1 curva por nó; sem este filtro, o `join` abaixo
        # somava as previsões de TODOS os modelos marcados `selected` por
        # nó (ex.: 4 métodos escolhidos = baseline ~4x maior que qualquer
        # modelo individual) — é o "salto" reportado entre histórico e
        # projeção. Resolvido aqui, visivelmente (mesmo critério de
        # `alternatives`: menor WAPE), sem alterar o motor: quando há mais
        # de 1 `selected=True` por nó, fica só o de menor WAPE como
        # referência desta view; a comparação completa entre todos os
        # métodos escolhidos continua na aba "Comparar métodos" (`/series/
        # by-model`), que não usa `selected`.
        if selected_scores.height:
            selected_scores = (
                selected_scores.sort(["node_id", "wape"], nulls_last=True)
                .group_by("node_id", maintain_order=True)
                .first()
            )

    placeholders = ",".join("?" * len(node_ids))
    preds = conn.execute(
        "SELECT node_id, model_alias, ds, yhat, lo80, hi80 FROM forecasts"
        " WHERE run_id = ? AND measure = ? AND scenario_id = ?"
        f" AND node_id IN ({placeholders})",
        [run_id, measure, scenario, *node_ids],
    ).pl()
    base_preds = (
        preds.join(
            selected_scores.select("node_id", "model_alias"),
            on=["node_id", "model_alias"],
            how="inner",
        )
        if preds.height and selected_scores.height
        else preds.clear()
    )
    baseline = (
        base_preds.group_by("ds")
        .agg(
            pl.col("yhat").sum().alias("yhat"),
            pl.col("lo80").sum().alias("lo80"),
            pl.col("hi80").sum().alias("hi80"),
        )
        .sort("ds")
        if base_preds.height
        else pl.DataFrame({"ds": [], "yhat": [], "lo80": [], "hi80": []})
    )

    alternatives: list[dict] = []
    if scores.height:
        elig = scores.filter(pl.col("eligible"))
        if elig.height:
            ranked = (
                elig.group_by("model_alias")
                .agg(pl.col("wape").mean().alias("wape"))
                .sort("wape")
                .head(3)
            )
            alternatives = [
                {"alias": r["model_alias"], "wape": r["wape"]}
                for r in ranked.to_dicts()
            ]

    cv_err = _cv_selected_errors(conn, run_id, node_ids, measure, selected_scores)
    wape = bias = None
    if cv_err.height:
        den = float(cv_err["y_actual"].abs().sum())
        if den > 0:
            wape = float(cv_err["e"].abs().sum() / den)
        bias = float(cv_err["e"].mean())

    total_h = float(baseline["yhat"].sum()) if baseline.height else 0.0
    hist_kpis = _history_kpis(hist_agg)

    return {
        "dim": dim,
        "value": value,
        "measure": measure,
        "scenario": scenario,
        "model_alias": model_alias,
        "n_series": len(node_ids),
        "history": [
            {"ds": r["ds"].isoformat(), "y": r["y"]} for r in hist_agg.to_dicts()
        ],
        "baseline": [
            {
                "ds": r["ds"].isoformat(),
                "yhat": r["yhat"],
                "lo80": r["lo80"],
                "hi80": r["hi80"],
            }
            for r in baseline.to_dicts()
        ],
        "alternatives": alternatives,
        "kpis": {
            **hist_kpis,
            "total_h": total_h,
            "horizon_n": baseline.height,
            "wape": wape,
            "bias": bias,
        },
    }


def _history_kpis(hist_agg: pl.DataFrame) -> dict:
    """Indicadores do histórico observado (mensal) de uma série agregada.

    `mat` = soma dos últimos 12 meses do histórico (só observado, sem projeção);
    `yoy` = último MAT contra o MAT de 12 meses antes (exige ≥ 24 meses);
    `cagr` = crescimento anual composto entre o primeiro e o último bloco de
    12 meses alinhados ao fim do histórico (exige ≥ 36 meses);
    `stats` = máx./mín. (com o mês), desvio padrão amostral e erro padrão da
    média dos valores mensais do histórico.
    """
    out: dict = {
        "mat": None,
        "yoy": None,
        "cagr": None,
        "cagr_years": None,
        "hist_months": hist_agg.height,
        "stats": None,
    }
    if hist_agg.height == 0:
        return out
    ys = hist_agg["y"].to_list()
    n = len(ys)
    if n >= 12:
        out["mat"] = float(sum(ys[-12:]))
    if n >= 24:
        prev = float(sum(ys[-24:-12]))
        if prev:
            out["yoy"] = (out["mat"] - prev) / prev
    if n >= 36:
        blocks = n // 12
        first = float(sum(ys[n - 12 * blocks : n - 12 * (blocks - 1)]))
        if first > 0 and out["mat"] is not None and out["mat"] >= 0:
            out["cagr_years"] = blocks - 1
            out["cagr"] = (out["mat"] / first) ** (1.0 / (blocks - 1)) - 1.0
    ds = hist_agg["ds"].to_list()
    i_max = max(range(n), key=lambda i: ys[i])
    i_min = min(range(n), key=lambda i: ys[i])
    mean = sum(ys) / n
    std = (sum((v - mean) ** 2 for v in ys) / (n - 1)) ** 0.5 if n > 1 else None
    out["stats"] = {
        "n": n,
        "max": float(ys[i_max]),
        "max_ds": ds[i_max].isoformat(),
        "min": float(ys[i_min]),
        "min_ds": ds[i_min].isoformat(),
        "std": std,
        "sem": std / n**0.5 if std is not None else None,
    }
    return out


def series_by_model(
    conn,
    run_id: str,
    dim: str,
    value: str,
    measure: str = "unidades",
    scenario: str = "base",
) -> dict:
    """1 curva por método rodado no nó, para comparação lado a lado (S2.8, Aba 2).

    Mesma soma bottom-up de `series_view`, mas agrupando por `model_alias` em
    vez de restringir ao `selected` de cada folha. O número de curvas é
    naturalmente pequeno porque o teto de métodos por rodada é 5 (S2.6).
    """
    study = load_run_study(conn, run_id)
    if dim not in study.dimension_names:
        raise KeyError(
            f"Dimensão '{dim}' não existe no estudo (opções: {study.dimension_names})"
        )
    leaves = conn.execute(
        "SELECT node_id, dimensions_json FROM run_nodes"
        " WHERE run_id = ? AND level = 'folha'",
        [run_id],
    ).pl()
    if leaves.height == 0:
        raise KeyError(f"Rodada {run_id} sem nós persistidos")
    parsed = _leaf_dim_frame(leaves, [dim])
    node_ids = parsed.filter(pl.col(dim) == value)["node_id"].to_list()
    if not node_ids:
        raise KeyError(f"Nenhuma série para {dim}={value}")

    history = _view_history(conn, run_id, node_ids, measure)
    hist_agg = (
        history.group_by("ds").agg(pl.col("y").sum().alias("y")).sort("ds")
        if history.height
        else pl.DataFrame({"ds": [], "y": []})
    )

    placeholders = ",".join("?" * len(node_ids))
    preds = conn.execute(
        "SELECT node_id, model_alias, ds, yhat, lo80, hi80 FROM forecasts"
        " WHERE run_id = ? AND measure = ? AND scenario_id = ?"
        f" AND node_id IN ({placeholders})",
        [run_id, measure, scenario, *node_ids],
    ).pl()

    # WAPE por método sobre o nó inteiro: soma|erro| / soma|real| (ponderado pelo
    # volume, mesma convenção do card WAPE). A média simples dos WAPEs por folha
    # explodia com séries pequenas/intermitentes (ex.: 560% numa classe inteira).
    wape_by_alias: dict[str, float] = {}
    cv = conn.execute(
        "SELECT model_alias, y_actual, yhat FROM cv_results"
        " WHERE run_id = ? AND measure = ? AND evaluated"
        " AND y_actual IS NOT NULL AND yhat IS NOT NULL"
        f" AND node_id IN ({placeholders})",
        [run_id, measure, *node_ids],
    ).pl()
    if cv.height:
        agg = cv.group_by("model_alias").agg(
            (pl.col("yhat") - pl.col("y_actual")).abs().sum().alias("abs_e"),
            pl.col("y_actual").abs().sum().alias("abs_y"),
        )
        wape_by_alias = {
            r["model_alias"]: r["abs_e"] / r["abs_y"]
            for r in agg.to_dicts()
            if r["abs_y"] > 0
        }

    models: list[dict] = []
    if preds.height:
        for alias in sorted(preds["model_alias"].unique().to_list()):
            sub = (
                preds.filter(pl.col("model_alias") == alias)
                .group_by("ds")
                .agg(
                    pl.col("yhat").sum().alias("yhat"),
                    pl.col("lo80").sum().alias("lo80"),
                    pl.col("hi80").sum().alias("hi80"),
                )
                .sort("ds")
            )
            models.append(
                {
                    "alias": alias,
                    "wape": wape_by_alias.get(alias),
                    "baseline": [
                        {
                            "ds": r["ds"].isoformat(),
                            "yhat": r["yhat"],
                            "lo80": r["lo80"],
                            "hi80": r["hi80"],
                        }
                        for r in sub.to_dicts()
                    ],
                }
            )

    return {
        "dim": dim,
        "value": value,
        "measure": measure,
        "scenario": scenario,
        "n_series": len(node_ids),
        "history": [
            {"ds": r["ds"].isoformat(), "y": r["y"]} for r in hist_agg.to_dicts()
        ],
        "models": models,
    }


def exceptions_ranking(
    conn, run_id: str, top: int = 20, measure: str = "unidades"
) -> dict:
    """Ranking de séries a revisar por `volume_12m × wape` do modelo `selected`
    (S2.3), com motivo legível em PT-BR.

    Nota (sec 9 do plano): a tarefa cita "queda > 30% no último ano" como
    exemplo de motivo; não implementado aqui por exigir uma janela adicional
    de 24 meses por série sem precedente no código existente — ficam apenas
    "erro alto" e "muitos zeros", com um motivo genérico de fallback.
    """
    leaves = conn.execute(
        "SELECT node_id, dimensions_json FROM run_nodes"
        " WHERE run_id = ? AND level = 'folha'",
        [run_id],
    ).pl()
    if leaves.height == 0:
        raise KeyError(f"Rodada {run_id} sem nós persistidos")
    node_ids = leaves["node_id"].to_list()
    scores = _query_scores(conn, run_id, node_ids, measure)
    selected = scores.filter(pl.col("selected")) if scores.height else scores
    if selected.height == 0:
        return {"top": top, "measure": measure, "items": []}

    history = _view_history(conn, run_id, node_ids, measure)
    if history.height:
        volume = (
            history.sort("ds")
            .group_by("node_id")
            .tail(12)
            .group_by("node_id")
            .agg(
                pl.col("y").sum().alias("volume_12m"),
                pl.len().alias("_n"),
                (pl.col("y") == 0).sum().alias("_n_zeros"),
            )
        )
    else:
        volume = pl.DataFrame(
            {"node_id": [], "volume_12m": [], "_n": [], "_n_zeros": []}
        )

    ranked = (
        selected.join(volume, on="node_id", how="left")
        .with_columns(
            pl.col("volume_12m").fill_null(0.0),
            pl.col("_n").fill_null(0),
            pl.col("_n_zeros").fill_null(0),
        )
        .with_columns(
            (pl.col("volume_12m") * pl.col("wape").fill_null(0.0)).alias("score")
        )
        .sort("score", descending=True)
        .head(top)
    )

    dims_by_node = {
        r["node_id"]: json.loads(r["dimensions_json"] or "{}")
        for r in leaves.to_dicts()
    }
    items: list[dict] = []
    for row in ranked.to_dicts():
        reasons = []
        if row["wape"] is not None and row["wape"] >= 0.30:
            reasons.append(f"erro alto no backtest (WAPE {row['wape']:.0%})")
        if row["_n"] and row["_n_zeros"] / row["_n"] >= 0.6:
            reasons.append("muitos zeros no histórico")
        if not reasons:
            reasons.append("maior impacto (volume × erro) da rodada")
        items.append(
            {
                "node_id": row["node_id"],
                "dimensions": dims_by_node.get(row["node_id"], {}),
                "model_alias": row["model_alias"],
                "wape": row["wape"],
                "volume_12m": row["volume_12m"],
                "reason": "; ".join(reasons),
            }
        )
    return {"top": top, "measure": measure, "items": items}


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
