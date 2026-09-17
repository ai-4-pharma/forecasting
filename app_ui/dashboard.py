"""app_ui.dashboard — Etapa 5 Resultados - cards, graficos, comparador e exportacoes.
(extraido de app.py single-file; split 16/09/2026).
"""

from pathlib import Path
import json
import plotly.graph_objects as go
import polars as pl
import streamlit as st
import data_engine
import forecast_engine
from contracts import (
    ExportConfig,
    ExportFormat,
    MatMode,
    ResultFilter,
    RunStatus,
    Severity,
    TemporalView,
)
from .shell import _db, _model_explanation, _model_name, _page_header


def _fmt_num(v) -> str:
    """Formata valor de card para exibição, com N/D quando for nulo (sec 12.1)."""
    if v is None:
        return "N/D"
    return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_pct(v) -> str:
    if v is None:
        return "N/D"
    return (
        f"{float(v) * 100:+,.1f}%".replace(",", "X").replace(".", ",").replace("X", ".")
    )


def _node_label(row: dict) -> str:
    """Rótulo legível de um nó a partir das dimensões reais (T6.3), com
    fallback para o início do `node_id` (hash) quando não há dimensões."""
    dims_json = row.get("dimensions_json")
    if dims_json:
        try:
            dims = json.loads(dims_json)
            vals = [str(v) for v in dims.values() if v]
            if vals:
                return " / ".join(vals)
        except Exception:  # noqa: BLE001, S110
            pass
    return str(row.get("node_id", ""))[:12]


@st.cache_data(show_spinner="Gerando arquivo…")
def _export_bytes(
    run_id: str, cfg_json: str, tipo: str, _cfg: ExportConfig
) -> tuple[bytes, str, str]:
    """Gera os bytes do arquivo de exportação, cacheados por (run_id, config, tipo)."""
    conn = _db()
    if tipo == "previsoes":
        art = data_engine.export_results(conn, run_id, _cfg)
        mime = art.mime_type
    elif tipo == "metricas":
        art = data_engine.export_metrics_csv(conn, run_id, _cfg)
        mime = "text/csv"
    else:
        art = data_engine.export_quality_csv(conn, run_id, _cfg)
        mime = "text/csv"
    data = (
        art.bytes_or_path
        if isinstance(art.bytes_or_path, bytes)
        else Path(art.bytes_or_path).read_bytes()
    )
    return data, art.filename, mime


def page_dashboard():
    _page_header(4)
    conn = _db()
    if st.session_state.run_id is None:
        st.info("Volte à etapa anterior e execute uma rodada primeiro.")
        return
    run_id = st.session_state.run_id
    nodes = data_engine.list_run_nodes(conn, run_id)
    measures = data_engine.list_run_measures(conn, run_id)
    scenarios = data_engine.list_run_scenarios(conn, run_id)
    levels = (
        sorted({r["level"] for r in nodes.to_dicts() if r["level"]})
        if nodes.height
        else []
    )
    node_labels = (
        {r["node_id"]: _node_label(r) for r in nodes.to_dicts()} if nodes.height else {}
    )

    st.caption(
        "Escolha o recorte da análise. Todos os cards, gráficos e tabelas abaixo "
        "respeitam exatamente esta seleção."
    )
    f_measure = st.selectbox("O que analisar", measures) if measures else None
    f_scenario = st.selectbox("Cenário", scenarios) if scenarios else "base"
    search = st.text_input(
        "Buscar série (texto parcial do identificador)", value="", key="dash_search"
    )
    if f_measure is None:
        st.info("Ainda não há resultados para esta rodada.")
        return

    # Grão = dimensão real da Etapa 2 Mapeamento B (Nível 1..5).
    # Ex.: Classe Terap_IV -> itens N05A1, N05A9; Molécula_inglês ->
    # AMISULPRIDE, QUETIAPINE... As previsões são na folha e SOMADAS
    # por valor da dimensão — por isso todo grão sempre tem dados.
    study = data_engine.load_run_study(conn, run_id)
    dim_names = list(study.dimension_names or [])
    if not dim_names:
        st.warning("Estudo sem dimensões; reexecute a rodada.")
        return
    # mapa folha -> dimensões (só nós 'folha' têm dimensions_json completo)
    leaf_dims: dict[str, dict] = {}
    for r in nodes.to_dicts():
        if r.get("level") != "folha":
            continue
        try:
            leaf_dims[str(r["node_id"])] = json.loads(r.get("dimensions_json") or "{}")
        except Exception:  # noqa: BLE001
            leaf_dims[str(r["node_id"])] = {}
    if not leaf_dims:
        st.warning("Nenhuma série folha nesta rodada.")
        return
    f_grain = st.selectbox(
        "Analisar por",
        dim_names,
        index=0,
        help="Escolha a dimensão em que deseja consolidar e comparar as previsões.",
        key="dash_grain",
    )
    # valores distintos da dimensão no escopo da rodada
    full_pool = sorted(
        {
            str(d.get(f_grain, ""))
            for d in leaf_dims.values()
            if str(d.get(f_grain, "")).strip()
        }
    )
    if search:
        _s = str(search).lower()
        item_pool = [v for v in full_pool if _s in v.lower()]
    else:
        item_pool = full_pool
    if not item_pool:
        st.warning(f"Nenhum item em '{f_grain}' para esta busca.")
        return
    f_items = st.multiselect(
        f"Itens de '{f_grain}' ({len(item_pool)} valores)",
        item_pool,
        default=item_pool[:1],
        help="Comece por um item para comparar os métodos com clareza. Você pode incluir outros itens quando precisar.",
        key=f"dash_items_{f_grain}",
    )
    if not f_items:
        st.caption("Selecione ao menos 1 item acima para ver o gráfico.")
        return

    # Busca sempre na folha e agrega por valor da dimensão (soma).
    rf = ResultFilter(
        run_id=run_id,
        node_level="folha",
        measure=f_measure,
        scenario_id=f_scenario,
        search="",
    )
    d = data_engine.query_results(conn, run_id, rf)
    run_status, issues = d.run.status, d.issues
    leaf_pred, leaf_hist, leaf_scores = d.predictions, d.history, d.scores
    if leaf_pred.height == 0:
        st.warning("Sem previsão na folha para medida/cenário. Reexecute a rodada.")
        return
    # folhas no escopo = cujo valor da dimensão está nos Itens
    scope_leaves = [
        nid
        for nid, dv in leaf_dims.items()
        if str(dv.get(f_grain, "")).strip() in set(f_items)
    ]
    if not scope_leaves:
        st.warning(f"Nenhuma folha em '{f_grain}' com estes itens.")
        return
    leaf_pred = leaf_pred.filter(pl.col("node_id").is_in(scope_leaves))
    if leaf_hist.height:
        leaf_hist = leaf_hist.filter(pl.col("node_id").is_in(scope_leaves))
    if leaf_scores.height:
        leaf_scores = leaf_scores.filter(pl.col("node_id").is_in(scope_leaves))
    # modelos executados na rodada (cada um tem suas próprias previsões)
    if "model_alias" in leaf_pred.columns:
        run_models = sorted(leaf_pred["model_alias"].drop_nulls().unique().to_list())
    else:
        run_models = []
    if not run_models:
        st.warning("Rodada sem previsões persistidas por modelo.")
        return
    # anexa valor do grão a cada folha e mantém o modelo (várias previsões série).
    grain_of = {nid: str(dv.get(f_grain, "")).strip() for nid, dv in leaf_dims.items()}
    preds = (
        leaf_pred.with_columns(
            pl.col("node_id")
            .map_elements(
                lambda n: grain_of.get(str(n), str(n)), return_dtype=pl.String
            )
            .alias("_grain")
        )
        .group_by(["_grain", "ds", "measure", "scenario_id", "level", "model_alias"])
        .agg(pl.col("yhat").sum().alias("yhat"))
        .rename({"_grain": "node_id"})
        .sort(["node_id", "ds"])
    )
    history = (
        leaf_hist.with_columns(
            pl.col("node_id")
            .map_elements(
                lambda n: grain_of.get(str(n), str(n)), return_dtype=pl.String
            )
            .alias("_grain")
        )
        .group_by(["_grain", "ds"])
        .agg(pl.col("y").sum().alias("y"))
        .rename({"_grain": "node_id"})
        .sort(["node_id", "ds"])
        if leaf_hist.height
        else pl.DataFrame()
    )
    scores = leaf_scores
    metrics = d.metrics
    node_labels = {v: v for v in f_items}
    # método de referência: a soma/métrica dos cards é POR MODELO (a aplicação
    # não elege vencedor; o usuário escolhe o método em que quer focar).
    ref_model = st.selectbox(
        "Método de referência (cards/tabela)",
        run_models,
        index=0,
        format_func=lambda alias: f"{_model_name(alias)} — {_model_explanation(alias)}",
        key="dash_ref_model",
        help="A rodada previu todos os métodos que você escolheu; use este seletor para decidir qual deles guia os cards e a tabela.",
    )
    preds_ref = (
        preds.filter(pl.col("model_alias") == ref_model)
        if preds.height
        else pl.DataFrame()
    )

    # identificação de resultado parcial (sec 8.2; nunca preencher faltas com zero)
    partial = run_status in (RunStatus.PARTIAL, RunStatus.CANCELLED)
    if partial:
        st.warning("Estes resultados são parciais e não representam a série completa.")

    # Cards calculados no recorte atual e no método de referência.
    study = data_engine.load_run_study(conn, run_id)
    is_mat = study.mat_mode in (MatMode.DERIVED, MatMode.DIRECT)
    horizon_periods = preds_ref["ds"].n_unique() if preds_ref.height else 0
    total_forecast = float(preds_ref["yhat"].sum()) if preds_ref.height else None
    hist_equivalent = None
    if history.height and horizon_periods:
        hist_equivalent = float(
            history.sort("ds")
            .group_by("ds")
            .agg(pl.col("y").sum())
            .tail(horizon_periods)["y"]
            .sum()
        )
    variation = (
        (total_forecast / hist_equivalent) - 1
        if total_forecast is not None and hist_equivalent not in (None, 0)
        else None
    )
    ref_scores = (
        scores.filter(pl.col("model_alias") == ref_model)
        if scores.height
        else pl.DataFrame()
    )
    wape = (
        float(ref_scores["wape"].mean())
        if ref_scores.height and ref_scores["wape"].null_count() < ref_scores.height
        else None
    )
    st.subheader("Resumo da análise")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric(
            "Itens na análise",
            preds_ref["node_id"].n_unique() if preds_ref.height else 0,
        )
    with c2:
        st.metric(
            "Posição final (MAT)" if is_mat else "Total previsto",
            _fmt_num(total_forecast),
        )
    with c3:
        st.metric("Variação vs. histórico equivalente", _fmt_pct(variation))
    with c4:
        st.metric(
            f"WAPE do método de referência",
            _fmt_pct(wape),
            help=(
                "Erro percentual médio no backtest das séries deste recorte "
                "para o método selecionado. Menor é melhor."
            ),
        )

    # Comparação de métodos: o menor erro ajuda a decidir, mas não substitui a
    # leitura de negócio do usuário.
    st.subheader("Comparar métodos")
    st.caption(
        "Use o histórico e estas métricas para escolher o método mais adequado. "
        "A aplicação sugere; você decide."
    )
    if scores.height:
        lb = (
            scores.filter(pl.col("eligible"))
            .group_by("model_alias")
            .agg(
                pl.col("mae").mean().alias("MAE_medio"),
                pl.col("rmse").mean().alias("RMSE_medio"),
                pl.col("wape").mean().alias("WAPE_medio"),
                pl.col("bias").mean().alias("Bias_medio"),
                pl.col("node_id").n_unique().alias("n_series"),
            )
            .sort("MAE_medio")
        )
        lb = (
            lb.with_columns(
                pl.col("model_alias")
                .map_elements(_model_name, return_dtype=pl.String)
                .alias("Método")
            )
            .with_columns(
                pl.col("model_alias")
                .map_elements(_model_explanation, return_dtype=pl.String)
                .alias("Explicação")
            )
            .select(
                [
                    "Método",
                    "Explicação",
                    "MAE_medio",
                    "RMSE_medio",
                    "WAPE_medio",
                    "Bias_medio",
                    "n_series",
                ]
            )
        )
        st.dataframe(lb, width="stretch")
        st.caption(
            "Menor MAE, RMSE e WAPE indicam melhor aderência ao histórico. "
            "Bias positivo indica tendência a superestimar."
        )
    else:
        st.caption("Sem métricas para este recorte.")

    # Layout da tabela abaixo do gráfico e da exportação (T6.6: um só seletor).
    layout_saida = st.radio(
        "Layout da tabela/exportação",
        ["longo", "largo"],
        horizontal=True,
        key="dash_layout",
    )

    # Gráfico fiel: 1 linha de histórico + previsão do método de referência
    # por valor da dimensão, + overlay opcional por modelo (soma das folhas).
    plot_items = list(f_items)
    MAX_PLOT = 5
    if len(plot_items) > MAX_PLOT:
        st.warning(
            f"{len(plot_items)} itens selecionados — o gráfico mostra até {MAX_PLOT} "
            "para manter a leitura clara. Refine sua seleção para comparar outros itens."
        )
        plot_items = plot_items[:MAX_PLOT]
    candidate_opts = (
        sorted(scores.filter(pl.col("eligible"))["model_alias"].unique().to_list())
        if scores.height
        else run_models
    )
    compare_models = st.multiselect(
        "Métodos exibidos no gráfico",
        candidate_opts,
        default=candidate_opts[:4],
        format_func=lambda alias: f"{_model_name(alias)} — {_model_explanation(alias)}",
        help="Selecione os métodos que deseja visualizar junto ao histórico.",
        key="dash_compare_models",
    )
    grain_to_leaves: dict[str, list[str]] = {}
    for nid, dv in leaf_dims.items():
        gv = str(dv.get(f_grain, "")).strip()
        if gv in set(plot_items):
            grain_to_leaves.setdefault(gv, []).append(str(nid))
    fig = go.Figure()
    for gv in plot_items:
        h = (
            history.filter(pl.col("node_id") == gv).sort("ds")
            if history.height
            else pl.DataFrame()
        )
        if h.height:
            fig.add_trace(
                go.Scatter(x=h["ds"], y=h["y"], mode="lines", name=f"{gv} · histórico")
            )
        if ref_model in compare_models:
            p = (
                preds.filter(
                    pl.col("node_id") == gv, pl.col("model_alias") == ref_model
                ).sort("ds")
                if preds.height
                else pl.DataFrame()
            )
            if p.height:
                fig.add_trace(
                    go.Scatter(
                        x=p["ds"],
                        y=p["yhat"],
                        mode="lines",
                        name=f"{gv} · {forecast_engine.model_label(ref_model)}",
                    )
                )
    # overlays: demais métodos selecionados (leitura do frame persistido)
    for gv in plot_items:
        leaves = grain_to_leaves.get(gv, [])
        if not leaves or not compare_models:
            continue
        for m in compare_models:
            if m == ref_model:
                continue
            pred_m = (
                preds.filter(pl.col("node_id") == gv, pl.col("model_alias") == m).sort(
                    "ds"
                )
                if preds.height
                else pl.DataFrame()
            )
            if pred_m.height:
                fig.add_trace(
                    go.Scatter(
                        x=pred_m["ds"],
                        y=pred_m["yhat"],
                        mode="lines",
                        name=f"{gv} · {forecast_engine.model_label(m)}",
                        line={"dash": "dash"},
                    )
                )
    st.plotly_chart(fig, width="stretch")

    st.subheader(
        f"📋 Tabela executiva — soma por valor da dimensão ({_model_name(ref_model)})"
    )
    table_df = (
        preds.filter(
            pl.col("node_id").is_in(plot_items), pl.col("model_alias") == ref_model
        )
        .select("node_id", "ds", "yhat")
        .sort(["node_id", "ds"])
    )
    if layout_saida == "largo" and table_df.height:
        table_df = table_df.pivot(
            on="ds", index="node_id", values="yhat", aggregate_function="first"
        ).sort("node_id")
    st.dataframe(table_df, width="stretch")

    with st.expander("Tabelas de diagnóstico"):
        st.caption(
            "**MAE/RMSE**: erro médio absoluto/quadrático do backtest (menor é"
            " melhor, mesma unidade da medida). **WAPE**: erro % ponderado pelo"
            " volume. **Bias**: viés médio (positivo = superestima). **Eligible**:"
            " a série teve histórico mínimo para o modelo. **Selected**: o método"
            " foi executado nesta série. **Fallback_used**: reservado para quando"
            " o método solicitado falha e o módulo de robustez assume."
        )
        st.subheader("Métricas por série e método")
        if scores.height:
            st.dataframe(
                scores.with_columns(
                    pl.col("mae").round(3),
                    pl.col("rmse").round(3),
                ).head(200),
                width="stretch",
            )
        else:
            st.caption("Sem métricas para este recorte.")
        st.subheader("Backtest por método (agregado)")
        if metrics.height:
            st.dataframe(
                metrics.with_columns(pl.col(pl.Float64).round(3)),
                width="stretch",
            )
        st.subheader("Regras aplicadas")
        st.caption(
            "Rastro de premissas por série/período é emitido na exportação XLSX."
        )
        st.subheader("Problemas da rodada")
        if issues:
            for iss in issues:
                fn = st.error if iss.severity == Severity.ERROR else st.warning
                fn(iss.message)
        else:
            st.caption("Nenhum problema registrado nesta rodada.")

    st.subheader("Exportação")
    fmt = st.selectbox("Formato", ["csv", "xlsx"], key="dash_fmt")
    c_opts = st.columns(3)
    include_intervals = c_opts[0].checkbox(
        "Incluir limites (lo80/hi80)", value=True, key="dash_export_intervals"
    )
    round_units = c_opts[1].checkbox(
        "Arredondar Unidades (folhas + total)",
        value=False,
        key="dash_export_round",
    )
    temporal_opt = (
        c_opts[2].selectbox(
            "Visão temporal",
            ["canonical", "mat"],
            key="dash_export_temporal",
        )
        if is_mat
        else "canonical"
    )
    st.download_button(
        f"Baixar tabela agregada ({f_grain} × período)",
        table_df.to_pandas().to_csv(index=False).encode("utf-8-sig"),
        file_name=f"agregado_{f_grain}_{f_measure}_{f_scenario}.csv",
        mime="text/csv",
        key="dash_export_agg",
    )
    export_cfg = ExportConfig(
        format=ExportFormat(fmt),
        layout="long" if layout_saida == "longo" else "wide",
        scenario_id=f_scenario or "base",
        measure=f_measure,
        level="folha",
        filters={},
        temporal_view=TemporalView(temporal_opt),
        include_intervals=include_intervals,
        round_units=round_units,
    )
    cfg_json = export_cfg.to_json()
    col_b, col_m, col_q = st.columns(3)
    with col_b:
        data, fname, mime = _export_bytes(run_id, cfg_json, "previsoes", export_cfg)
        st.download_button(
            "Baixar previsões", data, file_name=fname, mime=mime, key="dash_export"
        )
    with col_m:
        data, fname, mime = _export_bytes(run_id, cfg_json, "metricas", export_cfg)
        st.download_button(
            "Baixar métricas",
            data,
            file_name=fname,
            mime=mime,
            key="dash_export_metrics",
        )
    with col_q:
        data, fname, mime = _export_bytes(run_id, cfg_json, "qualidade", export_cfg)
        st.download_button(
            "Baixar qualidade",
            data,
            file_name=fname,
            mime=mime,
            key="dash_export_quality",
        )
