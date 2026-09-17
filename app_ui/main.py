"""app_ui.main — Inicializacao, roteamento das etapas e shell do wizard.
(extraido de app.py single-file; split 16/09/2026).
"""

import streamlit as st
import data_engine
from .shell import APP_VERSION, STEPS, _db, _init_state, _logger, _nav_footer, _reset_downstream, _step_gate
from .config_study import page_config_study
from .mapping import page_import
from .quality import page_quality
from .run import page_run
from .dashboard import page_dashboard

PAGES = [page_config_study, page_import, page_quality, page_run, page_dashboard]
def main():
    _init_state()
    _db()
    step = st.session_state.step

    flash = st.session_state.get("_flash")
    if flash:
        st.success(flash)
        st.session_state["_flash"] = None

    with st.sidebar:
        st.title("Forecast Community")
        for i, name in enumerate(STEPS):
            if i == step:
                marker = "▶"
            elif _step_gate(i)[0]:
                marker = "✅"
            else:
                marker = "○"
            st.markdown(f"{marker} {i + 1}. {name}")
        st.divider()
        st.caption("Abrir estudo salvo")
        if st.button("Recarregar lista"):
            st.session_state.studies = data_engine.list_datasets(_db())
        studies = st.session_state.studies
        if studies is not None and studies.height:
            rows = studies.to_dicts()
            options = {
                f"{r['name']} ({r['status']}) — {r['dataset_id'][:8]}…": r["dataset_id"]
                for r in rows
            }
            escolha = st.selectbox(
                "Estudo", list(options.keys()), key="sb_open_dataset"
            )
            if st.button("Abrir", key="sb_open_dataset_btn"):
                sel_id = options[escolha]
                study, mapping, ds = data_engine.load_dataset(_db(), sel_id)
                _reset_downstream(1)
                st.session_state.study = study
                st.session_state.mapping = mapping
                st.session_state.dataset = ds
                st.session_state.dataset_id = sel_id
                st.session_state.profile = data_engine.profile_data(ds, study)
                st.session_state.step = 2
                st.rerun()
        st.divider()
        st.caption(f"versão {APP_VERSION} — informe ao reportar problemas")

    st.progress(
        (step + 1) / len(STEPS),
        text=f"Etapa {step + 1} de {len(STEPS)} — {STEPS[step]}",
    )

    try:
        PAGES[step]()
    except Exception as e:
        try:
            from streamlit.runtime.scriptrunner import RerunException, StopException

            if isinstance(e, (RerunException, StopException)):
                raise
        except ImportError:
            if type(e).__name__ in ("RerunException", "StopException"):
                raise
        _logger.exception("Erro na etapa %s (%s)", step + 1, STEPS[step])
        st.error(
            "Algo deu errado nesta etapa. Tente voltar e refazer; se persistir, "
            "envie o arquivo .local/app.log no grupo."
        )
        with st.expander("Detalhes técnicos"):
            st.exception(e)
    _nav_footer()
