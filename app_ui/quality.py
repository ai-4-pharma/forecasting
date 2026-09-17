"""app_ui.quality — Etapa 3 Qualidade - perfil, pontos e preparacao dos dados.
(extraido de app.py single-file; split 16/09/2026).
"""

import duckdb
import streamlit as st
import data_engine
from contracts import DuplicateAction, MissingAction, NegativeAction, OutlierAction, Severity, TreatmentPolicy
from .shell import _db, _page_header

_UNRESOLVABLE_ERROR_CODES = {
    "E_UNITS_FRACTION",
    "E_DIMENSION_CONFLICT",
    "E_KEY_EMPTY",
    "E_DATE_AMBIGUOUS",
}
def page_quality():
    _page_header(2)
    if st.session_state.dataset is None or st.session_state.dataset_id is None:
        st.info("Volte à etapa anterior e confirme o mapeamento antes.")
        return
    prof = st.session_state.profile or data_engine.profile_data(
        st.session_state.dataset, st.session_state.study
    )

    error_issues = [i for i in prof.issues if i.severity == Severity.ERROR]
    warning_issues = [i for i in prof.issues if i.severity != Severity.ERROR]
    unresolvable = [i for i in error_issues if i.code in _UNRESOLVABLE_ERROR_CODES]

    c1, c2, c3 = st.columns(3)
    c1.metric("Séries", prof.summary.get("n_series", 0))
    c2.metric("Erros impeditivos", len(error_issues))
    c3.metric("Avisos", len(warning_issues))

    if error_issues:
        by_code: dict[str, int] = {}
        for i in error_issues:
            by_code[i.code] = by_code.get(i.code, 0) + 1
        st.dataframe(
            [{"Código": k, "Ocorrências": v} for k, v in sorted(by_code.items())],
            width="stretch",
            hide_index=True,
        )

    st.subheader("Diagnóstico por série")
    st.dataframe(prof.by_series.head(200), width="stretch")

    if unresolvable:
        st.error(
            "Há erros que nenhuma política de tratamento abaixo resolve "
            f"({', '.join(sorted({i.code for i in unresolvable}))}). "
            "Volte à etapa **Mapeamento** e ajuste dimensões/medidas/período."
        )
    elif prof.blocked:
        st.warning(
            "Há erros impeditivos (ex.: duplicidades) — escolha a política "
            "abaixo que os resolve antes de preparar."
        )

    if st.session_state.get("prepared") is not None:
        prep_prev = st.session_state.prepared
        st.caption(
            f"Preparação já concluída nesta sessão — séries elegíveis: "
            f"**{len(prep_prev.eligible_series)}** · "
            f"excluídas: **{len(prep_prev.excluded_series)}**."
        )

    with st.form("qualidade"):
        dup = st.selectbox(
            "Duplicidades",
            ["reject", "sum"],
            format_func=lambda x: (
                "Bloquear" if x == "reject" else "Somar (confirmado aditivo)"
            ),
        )
        missing = st.selectbox(
            "Lacunas",
            ["exclude_series", "zero", "ffill"],
            format_func={
                "exclude_series": "Excluir série",
                "zero": "Preencher zero",
                "ffill": "Carregar último valor",
            }.get,
        )
        neg = st.selectbox(
            "Negativos",
            ["reject", "allow"],
            format_func={
                "reject": "Excluir da preparação",
                "allow": "Aceitar venda líquida",
            }.get,
        )
        outl = st.selectbox(
            "Outliers",
            ["keep", "winsorize"],
            format_func={"keep": "Manter", "winsorize": "Winsorizar"}.get,
        )
        q = (
            st.number_input("Quantil para winsorização", 0.5, 1.0, 0.99, 0.01)
            if outl == "winsorize"
            else 0.99
        )
        if unresolvable:
            st.caption(
                "Botão desabilitado: volte ao Mapeamento para resolver os "
                "erros impeditivos listados acima."
            )
        submitted = st.form_submit_button("Preparar dados", disabled=bool(unresolvable))

    if submitted and not unresolvable:
        pol = TreatmentPolicy(
            duplicate_action=DuplicateAction(dup),
            missing_action=MissingAction(missing),
            negative_action=NegativeAction(neg),
            outlier_action=OutlierAction(outl),
            upper_quantile=float(q),
        )
        prep = data_engine.prepare_data(
            st.session_state.dataset, st.session_state.study, pol
        )
        st.session_state.prepared = prep
        conn = _db()
        try:
            prep_id = data_engine.save_preparation(
                conn, st.session_state.dataset_id, prep
            )
        except duckdb.ConstraintException:
            # B19: mesma preparação (dataset + política) já existe — reaproveita
            # o preparation_id calculado em vez de deixar o traceback subir.
            prep_id = prep.preparation_id
            st.session_state.preparation_id = prep_id
            st.session_state["_flash"] = (
                "ℹ️ Esta preparação já existe; abrindo o resultado salvo. "
                f"Séries elegíveis: **{len(prep.eligible_series)}** · "
                f"excluídas: **{len(prep.excluded_series)}**."
            )
            st.session_state.step = 3
            st.rerun()
        st.session_state.preparation_id = prep_id
        st.session_state["_flash"] = (
            "✅ Preparação salva. "
            f"Séries elegíveis: **{len(prep.eligible_series)}** · "
            f"excluídas: **{len(prep.excluded_series)}**."
        )
        st.session_state.step = 3
        st.rerun()
