"""app_ui.config_study — Etapa 1 Arquivo - configuracao do estudo e download do modelo.
(extraido de app.py single-file; split 16/09/2026).
"""

import hashlib
import re
import streamlit as st
import data_engine
from contracts import Layout, MappingConfig, StudyConfig
from .shell import _page_header, _reset_downstream

def page_config_study():
    _page_header(0)
    st.caption(
        "Planejamento de vendas e demanda — ferramenta local de previsão para dados "
        "mensais, trimestrais, anuais ou MAT."
    )

    # ── Nome do estudo ──────────────────────────────────────────────────────
    name = st.text_input(
        "Nome do estudo",
        value=st.session_state["_draft_name"],
        placeholder="Ex: Mercado N05A – Set/2026",
    )
    st.session_state["_draft_name"] = name

    # ── Formato dos dados ───────────────────────────────────────────────────
    layout_str = st.radio(
        "Formato dos dados",
        ["long", "wide"],
        index=["long", "wide"].index(st.session_state["_draft_layout"]),
        format_func={
            "long": "📋  Longo — uma linha por período  (dimensão | período | valor)",
            "wide": "📊  Largo — colunas por período  (dimensão | 2024-01 | 2024-02 | ...)",
        }.get,
        horizontal=True,
    )
    st.session_state["_draft_layout"] = layout_str

    # ── Modelo de arquivo e ajuda ───────────────────────────────────────────
    template_cfg = StudyConfig(
        name=re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()) or "estudo",
        layout=Layout(layout_str),
        dimension_names=["classe", "marca"],
        measures=["unidades"],
        history_periods=12,
    )
    template = data_engine.build_template(template_cfg, "xlsx")
    st.download_button(
        "⬇️ Baixar modelo (XLSX)",
        template.bytes_or_path,
        file_name=template.filename,
        mime=template.mime_type,
        key="dl_template",
    )
    with st.expander("Como preparar meu arquivo?"):
        st.markdown(
            "- **Formato Longo:** uma linha por entidade + período + medida "
            "(dimensões nas colunas, período numa coluna, valor noutra).\n"
            "- **Formato Largo:** uma linha por entidade, um período por coluna "
            "(cabeçalho `YYYYMM`, `YYYY-MM` ou `MM/YYYY`).\n"
            "- Cada medida (Unidades, Valor) ocupa **uma coluna própria** — "
            "não misture as duas numa mesma célula.\n"
            "- Não use **células mescladas**; a primeira linha deve conter só "
            "os nomes das colunas."
        )

    # ── Upload ──────────────────────────────────────────────────────────────
    st.markdown("---")
    f = st.file_uploader(
        "Carregar arquivo (CSV ou XLSX)",
        type=["csv", "txt", "xlsx", "xlsm"],
        help="Após carregar, o sistema lê as colunas automaticamente e guia a configuração.",
    )

    with st.expander("⚙️  Opções de leitura (CSV)", expanded=False):
        st.caption(
            "Ajuste apenas se a prévia abaixo não ficar correta (ex.: acentos "
            "quebrados ou colunas não separadas)."
        )
        enc_choice = st.selectbox(
            "Encoding",
            ["Detectar automaticamente", "utf-8", "utf-8-sig", "cp1252"],
            key="read_opt_encoding",
        )
        delim_choice = st.selectbox(
            "Separador de colunas",
            ["Detectar automaticamente", ";", ",", "\t"],
            key="read_opt_delimiter",
        )
        dec_choice = st.selectbox(
            "Separador decimal", [",", "."], key="read_opt_decimal"
        )

    read_opts = {
        "encoding": None if enc_choice == "Detectar automaticamente" else enc_choice,
        "delimiter": None
        if delim_choice == "Detectar automaticamente"
        else delim_choice,
        "decimal_separator": dec_choice,
    }
    st.session_state["_draft_read_opts"] = read_opts

    if f is not None:
        if not name.strip():
            st.warning("⚠️ Informe um nome para o estudo antes de continuar.")
            return

        content = f.getvalue()
        file_hash = hashlib.sha256(content).hexdigest()
        prev_hash = st.session_state.get("_draft_file_hash", "")
        if prev_hash and (
            file_hash != prev_hash
            or f.name != st.session_state.get("_draft_file_name", "")
        ):
            _reset_downstream(1)
        st.session_state["_draft_file_hash"] = file_hash

        with st.spinner("Lendo arquivo e detectando colunas..."):
            insp = data_engine.inspect_file(content, f.name, MappingConfig(**read_opts))

        blocking = [w for w in insp.warnings if w.severity == "error"]
        if blocking:
            for w in blocking:
                st.error(f"❌ {w.message}")
            return

        for w in insp.warnings:
            st.warning(f"⚠️ {w.message}")

        st.session_state["_draft_file_content"] = content
        st.session_state["_draft_file_name"] = f.name
        st.session_state["_draft_insp"] = insp

        n_cols = len(insp.columns)
        st.success(
            f"✅ **{n_cols} colunas detectadas.** "
            "Clique em **Avançar →** para configurar dimensões, hierarquia e medidas."
        )
        if insp.sample is not None:
            with st.expander("Prévia do arquivo", expanded=True):
                st.dataframe(insp.sample.head(5), width="stretch")

    st.caption(
        'Para abrir um estudo já salvo, use "Abrir estudo salvo" na barra lateral.'
    )
