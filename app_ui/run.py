"""app_ui.run — Etapa 4 Previsao - rodada dirigida em lotes e finalizacao.
(extraido de app.py single-file; split 16/09/2026).
"""

from datetime import datetime
import polars as pl
import streamlit as st
import data_engine
import forecast_engine
from contracts import (
    DEFAULT_BATCH_SIZE,
    ForecastConfig,
    ForecastMode,
    HierarchyConfig,
    HierarchyMode,
    RunInputs,
    RunStatus,
    RunSummary,
    ScenarioConfig,
)
from .shell import EVENTS_PER_RUN, _db, _model_explanation, _model_name, _page_header
from .assumptions import _edit_assumptions, _forecast_periods, _preview_assumptions


def _finalize_run(last) -> None:
    """Persiste o batch final/parcial e classifica a rodada (sec 8.2)."""
    conn = _db()
    run_id = st.session_state.run_id
    payload = last.payload if last.payload is not None else {}
    batch = payload.get("batch")
    n_failed = payload.get("n_failed", 0) or 0
    if batch is not None:
        data_engine.persist_batch(conn, run_id, "final", batch)
    nodes = payload.get("nodes")
    if nodes is not None and nodes.height:
        data_engine.persist_nodes(conn, run_id, nodes)
    cancelled = last.stage.value == "cancelled"
    status = (
        RunStatus.CANCELLED
        if cancelled
        else (RunStatus.PARTIAL if n_failed else RunStatus.COMPLETED)
    )
    n_preds = payload.get("n_predictions")
    if n_preds is None:
        n_preds = batch.predictions.height if batch is not None else 0
    cfg = st.session_state.get("run_config")
    eligible = (
        len(st.session_state.prepared.eligible_series)
        if st.session_state.prepared is not None
        else 0
    )
    summ = RunSummary(
        run_id,
        status,
        {"predictions": n_preds, "failed": n_failed, "eligible": eligible},
        {},
        {
            "n_nodes": batch.predictions["node_id"].n_unique()
            if batch is not None
            else 0
        },
        [],
        cfg.to_json() if cfg is not None else "",
    )
    data_engine.finish_run(conn, summ)

    st.session_state["run_gen"] = None
    st.session_state["run_cancel"] = False
    st.session_state["run_running"] = False
    st.session_state["run_finalized"] = True
    st.session_state["run_result_summary"] = {
        "cancelled": cancelled,
        "n_preds": n_preds,
        "n_failed": n_failed,
        "has_batch": batch is not None and batch.predictions.height > 0,
    }
    _show_finalized_summary()


def _show_finalized_summary() -> None:
    """Mostra o resultado da última rodada concluída e o botão "Ver resultados →".

    Chamada tanto por `_finalize_run` (assim que a rodada termina) quanto por
    `page_run` em reruns seguintes — sem isso, o botão só existia no rerun que
    processou o último lote e sumia (com `run_gen=None`) antes do clique em
    "Ver resultados →" conseguir ser processado (bug encontrado na Fase 6).
    """
    summary = st.session_state.get("run_result_summary")
    if not summary:
        return
    n_preds = summary["n_preds"]
    n_failed = summary["n_failed"]
    if summary["cancelled"]:
        st.warning(
            f"Rodada cancelada. {n_preds} previsões parciais e folds preservados "
            "para diagnóstico."
        )
    elif n_failed:
        st.warning(
            f"Rodada concluída com {n_failed} série(s) sem previsão. "
            f"Resultado parcial: {n_preds} previsões."
        )
    else:
        st.success(f"Rodada concluída: {n_preds} previsões.")
    if summary["has_batch"]:
        _show_run_summary(st.session_state.get("run_config"), n_preds, n_failed)

    c1, c2 = st.columns([1, 1])
    if c1.button("Ver resultados →", key="btn_ver_resultados", type="primary"):
        st.session_state.step = 4
        st.rerun()
    if c2.button("↻ Configurar nova rodada", key="btn_nova_rodada"):
        for k in ("run_finalized", "run_result_summary", "run_last", "run_config"):
            st.session_state.pop(k, None)
        st.rerun()


def _show_run_summary(cfg, n_preds: int, n_failed: int) -> None:
    """Resumo final de cobertura e configuração da rodada (P31)."""
    st.subheader("Resumo da rodada")
    study = st.session_state.study
    if study is None:
        return
    est = len(st.session_state.prepared.eligible_series)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Séries elegíveis", est)
    c2.metric("Observações previstas", n_preds)
    c3.metric("Falhas", n_failed)
    if cfg is not None:
        c4.metric("Horizonte", cfg.horizon_periods)
    st.caption(
        "Configuração: "
        f"modo {cfg.mode.value if cfg is not None else '—'}, "
        f"intervalo {cfg.interval_level}% se suportado, "
        f"hierarquia {cfg.hierarchy.mode.value if cfg and cfg.hierarchy else '—'}, "
        f"lote {cfg.batch_size if cfg is not None else '—'}, "
        f"threads {cfg.n_jobs if cfg is not None else '—'}."
    )


def _drive_run() -> None:
    """Avança o gerador em lotes, permitindo cancelar entre lotes (P31).

    O gerador é conservado em st.session_state; cada avanço processa até
    EVENTS_PER_RUN eventos e devolve o controle à UI para que o usuário possa
    solicitar cancelamento. Não promete interromper uma chamada estatística já
    em andamento (sec 4).

    A fase de previsão final emite um evento por modelo concluído, então o log e
    a barra mostram "modelo (k/n) — X de N séries" com estimativa de tempo
    restante: sem isso, rodadas longas (ex.: AutoTBATS) pareciam travadas porque
    a persistência só acontece no fim.
    """
    gen = st.session_state.get("run_gen")
    if gen is None:
        return
    st.caption("Rodada em andamento — você pode cancelar entre lotes.")
    if st.button("Cancelar", key="btn_cancel"):
        st.session_state["run_cancel"] = True
        st.rerun()
    progress = st.progress(0.0, text="Processando…")
    run_log: list[tuple[str, str, str]] = st.session_state.setdefault("run_log", [])
    log_box = st.container(height=240)
    cancel = st.session_state.get("run_cancel", False)
    last = st.session_state.get("run_last")
    steps = 0
    done = False
    while steps < EVENTS_PER_RUN or cancel:
        try:
            ev = next(gen)
        except StopIteration:
            done = True
            break
        last = ev
        st.session_state["run_last"] = ev
        progress.progress(min(1.0, ev.completed / max(1, ev.total)), text=ev.message)
        run_log.append(
            (datetime.now().strftime("%H:%M:%S"), ev.stage.value, ev.message)
        )
        del run_log[:-200]
        steps += 1
        if ev.stage.value in ("completed", "cancelled", "failed"):
            done = True
            break
    with log_box:
        for ts, stage, message in run_log:
            st.text(f"[{ts}] {stage}: {message}")
    if done and last is not None:
        _finalize_run(last)
        return
    st.rerun()


def page_run():
    _page_header(3)
    if st.session_state.prepared is None or st.session_state.preparation_id is None:
        st.info("Conclua a preparação na etapa anterior.")
        return
    study = st.session_state.study
    max_h = study.max_horizon_periods()
    # proteção contra execução simultânea: uma rodada ativa assume a página
    if st.session_state.get("run_gen") is not None:
        st.caption("Uma rodada está em execução nesta sessão.")
        _drive_run()
        return
    if st.session_state.get("run_finalized") and st.session_state.get(
        "run_result_summary"
    ):
        _show_finalized_summary()
        return
    with st.expander("Premissas e cenários (opcional)", expanded=False):
        _edit_assumptions()
    entities = st.session_state.dataset.entities
    st.caption(
        "Defina o horizonte, escolha os métodos que deseja comparar e execute. "
        "As opções técnicas ficam recolhidas para não atrapalhar a análise."
    )
    horizon = st.number_input(
        "Horizonte de projeção (períodos)",
        1,
        max_h,
        min(12, max_h),
        help=(
            "O limite acompanha a frequência selecionada: até 120 meses para dados "
            "mensais; para outras frequências, o limite é convertido para períodos."
        ),
    )
    if horizon > 24:
        st.warning(
            "Projeções de longo prazo têm elevada incerteza e dependem das premissas"
            " adotadas. O horizonte validado historicamente pode ser menor que o"
            " projetado. A interpretação e condução do estudo são de responsabilidade"
            " do usuário."
        )
    use_advanced = st.checkbox(
        "Incluir métodos avançados (ARIMA, CES e TBATS)",
        value=False,
        help="Deixe desligado para uma comparação simples e mais rápida.",
    )
    mode = "advanced" if use_advanced else "fast"
    with st.expander("Opções avançadas de execução", expanded=False):
        enable_ml = st.checkbox(
            "Habilitar aprendizado global (MLForecast + LightGBM/XGBoost)", value=False
        )
        interval_level = st.number_input(
            "Nível do intervalo de previsão (%)",
            50,
            99,
            80,
            help="Solicitado à biblioteca quando o modelo oferece intervalo; "
            "indisponível em agregações e MAT.",
        )
        batch = st.number_input("Tamanho do lote", 50, 500, DEFAULT_BATCH_SIZE, 50)
        n_jobs = st.number_input(
            "Threads (padrão 1; em Windows evita travamentos)", 1, 4, 1
        )
        season_choice = st.selectbox(
            "Comprimento da sazonalidade (ciclo)",
            options=_season_options(study.model_frequency),
            format_func=_season_option_label,
            help=(
                "Ciclo sazonal usado por SeasonalNaive, AutoETS, AutoTheta, "
                "AutoCES, AutoTBATS, AutoARIMA e AutoARIMA_X. Modelos de "
                "tendência (Holt/HoltDamped) e intermitentes (Croston/TSB) "
                "não usam sazonalidade."
            ),
        )
        hier_mode = st.selectbox(
            "Hierarquia",
            ["independent", "bottom_up", "mintrace"],
            format_func={
                "independent": "Independente (modela o nível escolhido)",
                "bottom_up": "Bottom-Up (modela a folha e soma para os pais)",
                "mintrace": "MinT / Reconciliação ótima (coerência mínima variância)",
            }.get,
        )
        sel_dims = st.multiselect(
            "Dimensões dos níveis (ordem topo → folha)",
            study.dimension_names,
            default=study.dimension_names,
        )
        if not sel_dims:
            sel_dims = [study.analysis_level]
        level_opts = [
            f"{i + 1}: {' + '.join(sel_dims[: i + 1])}" for i in range(len(sel_dims))
        ]
        lvl_txt = st.selectbox(
            "Nível a modelar",
            level_opts,
            index=len(level_opts) - 1,
            disabled=(hier_mode == "bottom_up"),
        )
    st.subheader("Métodos para comparar")
    st.caption(
        "Escolha individualmente os métodos no seletor abaixo. A aplicação executa "
        "todos os marcados e permite comparar os resultados no dashboard."
    )
    probe = ForecastConfig(mode=ForecastMode(mode), enable_ml=enable_ml, seed=42)
    all_cands = forecast_engine.build_candidates(None, study, probe)
    all_aliases = [c.alias for c in all_cands]
    # os modelos globais entram no catalogo apenas com o complemento ML instalado
    if enable_ml and _ml_available():
        for ml_alias in ("LightGBM", "XGBoost"):
            if ml_alias == "XGBoost" and not _xgb_available():
                continue
            if ml_alias not in all_aliases:
                all_aliases.append(ml_alias)
    catalog = pl.DataFrame(
        [
            {
                "Método": _model_name(alias),
                "Como funciona": _model_explanation(alias),
                "Código": alias,
            }
            for alias in all_aliases
        ]
    )
    st.dataframe(catalog, width="stretch", hide_index=True)
    default_sel = [
        alias
        for alias in ("Naive", "HistoricAverage", "RegLinearDrift", "AutoETS")
        if alias in all_aliases
    ]
    selected_models = st.multiselect(
        "Métodos selecionados",
        options=all_aliases,
        default=default_sel,
        format_func=lambda alias: f"{_model_name(alias)} — {_model_explanation(alias)}",
        key="run_selected_models",
        placeholder="Abra a lista e escolha os métodos",
    )
    if not selected_models:
        st.warning("Selecione ao menos um método para executar.")
    prev_scenarios = [
        s
        for s in st.session_state.scenarios
        if getattr(s, "scenario_id", "base") != "base"
    ]
    with st.expander("Prévia das premissas (entidades e fatores)"):
        pv_periods = _forecast_periods(study, int(horizon))
        preview, pv_issues = _preview_assumptions(prev_scenarios, entities, pv_periods)
        if not prev_scenarios:
            st.caption(
                "Nenhum cenário com regras configurado; a rodada executará "
                "somente o cenário base."
            )
        for sc, compiled, summary in preview:
            st.markdown(f"**{sc.name}** (`{sc.scenario_id}`)")
            st.dataframe(summary, width="stretch")
            st.dataframe(compiled.head(200), width="stretch")
        for issue in pv_issues:
            st.error(issue.message)
    submitted = st.button("▶ Executar previsão", type="primary")

    if submitted:
        if (
            st.session_state.get("run_running")
            or st.session_state.get("run_gen") is not None
        ):
            st.error(
                "Já existe uma rodada em execução nesta sessão. Conclua-a "
                "(ou cancele no dashboard) antes de iniciar outra."
            )
            st.stop()
        lvl_idx = level_opts.index(lvl_txt)
        ordered = [list(sel_dims[: i + 1]) for i in range(len(sel_dims))]
        if not selected_models:
            st.error("Selecione ao menos 1 modelo antes de executar.")
            st.stop()
        cfg = ForecastConfig(
            horizon_periods=int(horizon),
            mode=ForecastMode(mode),
            enable_ml=enable_ml,
            interval_level=int(interval_level),
            batch_size=int(batch),
            n_jobs=int(n_jobs),
            season_length=_season_length_from_choice(season_choice),
            hierarchy=HierarchyConfig(
                mode=HierarchyMode(hier_mode),
                ordered_levels=ordered,
                forecast_level=ordered[lvl_idx]
                if hier_mode == "independent"
                else ordered[-1],
                include_total=(hier_mode in ("bottom_up", "mintrace")),
            ),
        )
        cfg.candidate_aliases = list(selected_models)
        if enable_ml and not _ml_available():
            st.warning(
                "Complemento ML não detectado. Instale com "
                "`pip install -r requirements.txt` para usar o modo global;"
                " o núcleo continuará com modelos estatísticos."
            )
            cfg.enable_ml = False
        conn = _db()
        scenarios, regressors = data_engine.load_assumptions(
            conn, st.session_state.dataset_id
        )
        if not any(s.scenario_id == "base" for s in scenarios):
            scenarios.insert(0, ScenarioConfig())
        # bloqueia a rodada se houver conflito de prioridade em qualquer cenário
        run_conflicts = _preview_assumptions(
            scenarios, entities, _forecast_periods(study, int(horizon))
        )[1]
        if run_conflicts:
            for issue in run_conflicts:
                st.error(issue.message)
            st.stop()
        cfg.scenario_ids = [s.scenario_id for s in scenarios]
        cfg.regressor_ids = [r.regressor_id for r in regressors if r.enabled]
        fut_periods = _forecast_periods(study, int(horizon))
        for issue in forecast_engine.validate_regressors(
            [r for r in regressors if r.enabled], {}, fut_periods
        ):
            if issue.severity.value == "error":
                st.error(issue.message)
            else:
                st.warning(issue.message)
        run_id = data_engine.create_run(
            conn,
            st.session_state.dataset_id,
            st.session_state.preparation_id,
            cfg,
            scenarios,
            regressors,
        )
        st.session_state.run_id = run_id
        st.session_state["run_config"] = cfg
        inputs = RunInputs(
            run_id=run_id,
            dataset_id=st.session_state.dataset_id,
            preparation_id=st.session_state.preparation_id,
            study=study,
            mapping=st.session_state.mapping,
            policy=st.session_state.prepared.policy,
            config=cfg,
            scenarios=scenarios,
            regressors=regressors,
            prepared=st.session_state.prepared.prepared,
            raw_series=st.session_state.prepared.raw_series,
            entities=st.session_state.dataset.entities,
            should_cancel=lambda: st.session_state.get("run_cancel", False),
        )
        # um gerador por processo: a UI avança entre lotes e permite cancelar
        st.session_state["run_gen"] = forecast_engine.run_forecast(inputs)
        st.session_state["run_cancel"] = False
        st.session_state["run_running"] = True
        st.session_state["run_last"] = None
        st.session_state["run_finalized"] = False
        st.session_state["run_log"] = []
        st.rerun()


def _ml_available() -> bool:
    try:
        import mlforecast  # noqa: F401
        import lightgbm  # noqa: F401

        return True
    except ImportError:
        return False


def _xgb_available() -> bool:
    """XGBoost é opcional dentro do complemento ML (ver `forecast_engine.ml`)."""
    try:
        import xgboost  # noqa: F401

        return True
    except ImportError:
        return False


_SEASON_LABELS = {
    12: "anual",
    6: "semestral",
    4: "quadrimestral",
    3: "trimestral",
    2: "bimestral",
    1: "sem sazonalidade",
}


def _season_label(v: int) -> str:
    return _SEASON_LABELS.get(int(v), f"{v} períodos")


def _season_options(freq) -> list[str]:
    """Opções de ciclo sazonal coerentes com a frequência ("auto" = padrão)."""
    from contracts import SourceFrequency

    if freq in (SourceFrequency.MONTHLY, SourceFrequency.MAT):
        values = [12, 6, 4, 3, 2, 1]
    elif freq == SourceFrequency.QUARTERLY:
        values = [4, 2, 1]
    else:
        values = [1]
    return ["auto"] + [str(v) for v in values]


def _season_option_label(option: str) -> str:
    """Rótulo do selectbox de sazonalidade ("auto" mostra o ciclo da frequência)."""
    if option == "auto":
        return "auto (pela frequência)"
    v = int(option)
    return f"{v} ({_season_label(v)})"


def _season_length_from_choice(choice) -> int | None:
    if choice is None or choice == "auto":
        return None
    try:
        v = int(choice)
    except (TypeError, ValueError):
        return None
    return v if v >= 1 else None
