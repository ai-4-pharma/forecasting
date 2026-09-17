"""app_ui.mapping — Etapa 2 Mapeamento - leitura, inspecao e normalizacao do arquivo.
(extraido de app.py single-file; split 16/09/2026).
"""

from datetime import date
import hashlib
import streamlit as st
import data_engine
from contracts import Layout, MappingConfig, MatMode, SourceFrequency, StudyConfig
from .shell import _db, _page_header, _parse_period_col, _reset_downstream

def page_import():
    _page_header(1)

    insp = st.session_state.get("_draft_insp")
    if insp is None:
        st.info("📂 Volte à etapa anterior e carregue um arquivo para continuar.")
        return

    layout = Layout(st.session_state.get("_draft_layout", "long"))
    name = st.session_state.get("_draft_name", "")
    all_cols = insp.columns

    # Pré-classifica colunas de período
    auto_period_map: dict[str, str] = {}
    non_period_cols: list[str] = []
    for c in all_cols:
        det = _parse_period_col(c)
        if det:
            auto_period_map[c] = det
        else:
            non_period_cols.append(c)

    n_periods = len(auto_period_map)
    sorted_periods = sorted(set(auto_period_map.values()))

    # ── A. Colunas de identificação ─────────────────────────────────────────
    st.subheader("A — Identificadores de entidade")
    st.caption(
        "Selecione as colunas que identificam seus produtos, marcas ou entidades "
        "(Classe, Molécula, Marca, EAN, etc.)."
    )

    # Pré-seleção inteligente: exclui colunas que parecem medidas ou datas
    _measure_keywords = {
        "unidades",
        "valor",
        "value",
        "units",
        "qty",
        "quantity",
        "vendas",
        "sales",
        "revenue",
        "receita",
        "periodo",
        "period",
        "date",
        "data",
        "mes",
        "month",
    }
    smart_default = [c for c in non_period_cols if c.lower() not in _measure_keywords]

    dim_cols = st.multiselect(
        "Colunas de identificação (dimensões)",
        options=non_period_cols,
        default=smart_default,
        help="Inclua todas as colunas que formam a identidade única de uma entidade.",
    )

    # ── B. Hierarquia e grão ────────────────────────────────────────────────
    ordered_dims: list[str] = []
    analysis_level = ""

    if dim_cols:
        st.subheader("B — Hierarquia e grão de análise")
        st.caption(
            "Ordene as dimensões **do mais geral (topo) para o mais específico (grão mínimo)**. "
            "O último nível será o grão de análise."
        )
        used: set[str] = set()
        for i in range(len(dim_cols)):
            available = [c for c in dim_cols if c not in used]
            if not available:
                break
            if i == 0:
                label = "Nível 1 — mais geral (ex: Classe Terapêutica)"
            elif i == len(dim_cols) - 1:
                label = f"Nível {i + 1} — grão mínimo (ex: SKU / EAN)"
            else:
                label = f"Nível {i + 1}"
            choice = st.selectbox(label, available, key=f"hier_{i}")
            ordered_dims.append(choice)
            used.add(choice)

        if ordered_dims:
            analysis_level = ordered_dims[-1]
            st.info(
                f"📌 Grão mínimo definido: **{analysis_level}**  "
                f"| Hierarquia: {' → '.join(ordered_dims)}"
            )

    # ── C. Períodos ─────────────────────────────────────────────────────────
    st.subheader("C — Períodos")

    period_col: str | None = None  # apenas para formato longo
    suggested: dict | None = None  # T3.4/B14: sugestão automática de tempo

    if layout == Layout.WIDE:
        if n_periods > 0:
            st.success(
                f"✅ **{n_periods} colunas de período detectadas automaticamente** "
                + (
                    f"({sorted_periods[0][:7]} a {sorted_periods[-1][:7]})"
                    if sorted_periods
                    else ""
                )
            )
            with st.expander("Ver colunas de período detectadas", expanded=False):
                st.dataframe(
                    [
                        {"Coluna original": k, "Período": v[:7]}
                        for k, v in auto_period_map.items()
                    ],
                    width="stretch",
                    hide_index=True,
                )
        else:
            st.warning(
                "⚠️ Nenhuma coluna de período detectada automaticamente. "
                "Verifique se o arquivo está no formato correto (colunas nomeadas como YYYYMM ou YYYY-MM)."
            )
        suggested = data_engine.suggest_time_settings(
            insp.sample, None, list(auto_period_map.values())
        )
    else:
        # Formato longo: o usuário escolhe qual coluna é o período
        periodo_candidates = [c for c in non_period_cols if c not in dim_cols]
        default_period = next(
            (
                c
                for c in ["periodo", "period", "date", "data", "mes", "month"]
                if c in periodo_candidates
            ),
            periodo_candidates[0] if periodo_candidates else None,
        )
        period_col = st.selectbox(
            "Coluna de período",
            options=non_period_cols,
            index=non_period_cols.index(default_period)
            if default_period in non_period_cols
            else 0,
            help="Coluna que contém as datas ou rótulos de período (ex: 2026-07, 202607).",
        )
        suggested = data_engine.suggest_time_settings(insp.sample, period_col, [])

    freq_labels = {
        "monthly": "📅 Mensal",
        "quarterly": "📆 Trimestral",
        "yearly": "🗓️ Anual",
        "mat": "📊 MAT (acumulado móvel de 12 meses)",
    }
    _freq_keys = list(freq_labels.keys())
    _suggested_freq = (suggested or {}).get("frequency")
    freq = st.radio(
        "Frequência dos dados",
        _freq_keys,
        index=_freq_keys.index(_suggested_freq) if _suggested_freq in _freq_keys else 0,
        format_func=freq_labels.get,
        horizontal=True,
    )

    mat_mode = "none"
    if freq == "mat":
        mat_mode = st.radio(
            "Modo MAT",
            ["derived", "direct"],
            format_func={
                "derived": "Derivado de meses (recomendado se tiver dados mensais)",
                "direct": "Direto (modela o acumulado de 12 meses)",
            }.get,
        )

    # Último período fechado
    if layout == Layout.WIDE and sorted_periods:
        hist_end_str = st.selectbox(
            "Último período fechado",
            options=sorted(sorted_periods, reverse=True),
            format_func=lambda p: p[
                :7
            ],  # exibe "YYYY-MM"; valor interno é ISO completo
            help="O período mais recente com dados completos no arquivo.",
        )
        try:
            parts = hist_end_str.split("-")
            hist_end: date = date(int(parts[0]), int(parts[1]), 1)
        except Exception:
            hist_end = date.today().replace(day=1)
        hist_periods = min(n_periods, 60)
    else:
        _default_hist_end = (suggested or {}).get(
            "history_end"
        ) or date.today().replace(day=1)
        _default_n_periods = min(120, max(1, (suggested or {}).get("n_periods") or 36))
        hist_end = st.date_input(
            "Último período fechado",
            value=_default_hist_end,
            help="O período mais recente com dados completos.",
        )
        hist_periods = st.number_input(
            "Quantidade de períodos de histórico",
            min_value=1,
            max_value=120,
            value=_default_n_periods,
        )

    # ── D. Medidas ──────────────────────────────────────────────────────────
    st.subheader("D — Medidas (valores a prever)")

    meas_map: dict[str, str] = {}

    if layout == Layout.WIDE:
        # No formato Largo, os valores já estão dentro das colunas de período.
        # Não há coluna separada de medida — só precisamos saber o que elas representam.
        st.caption(
            "As colunas de período contendo os números: o que representam esses valores?"
        )
        wide_measure_choice = st.radio(
            "Os valores nas colunas de período representam:",
            ["unidades", "valor"],
            format_func={
                "unidades": "📦 Unidades / volume / quantidade",
                "valor": "💰 Valor monetário / receita",
            }.get,
            horizontal=False,
        )
        st.caption("Tem unidades e valor no mesmo arquivo? Use o formato **Longo**.")
        meas_map = {wide_measure_choice: "__wide__"}
    else:
        # Formato Longo: o usuário seleciona as colunas que contêm os valores
        st.caption("Quais colunas contêm os valores numéricos que você quer prever?")

        excluded_from_measures = set(dim_cols)
        if period_col:
            excluded_from_measures.add(period_col)
        measure_candidates = [
            c for c in non_period_cols if c not in excluded_from_measures
        ]

        _value_kw = {
            "unidad",
            "unit",
            "qty",
            "quant",
            "valor",
            "value",
            "vend",
            "revenue",
            "sales",
            "receita",
        }
        smart_meas = [
            c for c in measure_candidates if any(kw in c.lower() for kw in _value_kw)
        ]
        if not smart_meas:
            smart_meas = measure_candidates[:1]

        selected_measure_cols = st.multiselect(
            "Colunas de medidas",
            options=measure_candidates,
            default=[c for c in smart_meas if c in measure_candidates],
            help="Selecione uma ou duas colunas (ex: unidades e valor).",
        )

        if selected_measure_cols:
            st.caption("Para cada coluna, indique se representa Unidades ou Valor:")
            n_m = len(selected_measure_cols)
            mcols = st.columns(min(n_m, 3))
            for i, col_name in enumerate(selected_measure_cols):
                with mcols[i % len(mcols)]:
                    cl = col_name.lower()
                    is_value = any(
                        kw in cl
                        for kw in ["valor", "value", "revenue", "receita", "sales"]
                    )
                    default_type = "valor" if is_value else "unidades"
                    m_type = st.radio(
                        f'"{col_name}"',
                        ["unidades", "valor"],
                        index=["unidades", "valor"].index(default_type),
                        key=f"mtype_{col_name}",
                        horizontal=True,
                    )
                    meas_map[m_type] = col_name

    # ── E. Configurações avançadas ──────────────────────────────────────────
    read_opts = st.session_state.get(
        "_draft_read_opts",
        {"encoding": None, "delimiter": None, "decimal_separator": ","},
    )
    encoding = read_opts.get("encoding") or "utf-8"
    dec = read_opts.get("decimal_separator") or ","
    delimiter = read_opts.get("delimiter") or ";"
    with st.expander("⚙️  Configurações avançadas", expanded=False):
        currency = st.text_input("Moeda", "BRL")
        st.caption(
            "Encoding, separador de colunas e decimal ficam na etapa "
            '**Arquivo**, em "Opções de leitura (CSV)".'
        )
        hist_periods_override = st.number_input(
            "Limitar histórico a (períodos)",
            min_value=1,
            max_value=120,
            value=int(hist_periods),
            help="Padrão: todos os períodos detectados no arquivo, até o máximo de 60.",
        )
        hist_periods = hist_periods_override

    # ── Validação e confirmação ─────────────────────────────────────────────
    st.divider()
    errors: list[str] = []
    if not name.strip():
        errors.append("Informe um nome para o estudo na etapa anterior (Arquivo).")
    if not dim_cols:
        errors.append("Seção A: selecione ao menos uma coluna de identificação.")
    if not ordered_dims and dim_cols:
        errors.append("Seção B: defina a hierarquia das dimensões.")
    if not meas_map:
        errors.append("Seção D: selecione ao menos uma medida.")
    if layout == Layout.WIDE and not auto_period_map:
        errors.append("Seção C: nenhuma coluna de período detectada.")
    if layout == Layout.LONG and not period_col:
        errors.append("Seção C: selecione a coluna de período.")
    if len(meas_map) > 2:
        errors.append("Seção D: máximo de 2 medidas por estudo (unidades e valor).")

    for e in errors:
        st.warning(f"⚠️ {e}")

    if st.session_state.get("dataset") is not None:
        ds_prev = st.session_state.dataset
        st.caption(
            f"Prévia já confirmada: **{ds_prev.observations.height}** observações · "
            f"**{ds_prev.entities.height}** entidades."
        )

    if not errors:
        if st.button("✅  Confirmar e preparar", type="primary"):
            # ── Monta StudyConfig ──────────────────────────────────────────
            model_freq = SourceFrequency("monthly" if freq == "mat" else freq)
            study = StudyConfig(
                name=name,
                layout=layout,
                dimension_names=ordered_dims,
                analysis_level=analysis_level,
                source_frequency=SourceFrequency(freq),
                model_frequency=model_freq,
                mat_mode=MatMode(mat_mode),
                # Para wide, meas_map usa "__wide__" como valor ficticio;
                # o StudyConfig só precisa das chaves (nomes das medidas).
                measures=list(meas_map.keys())[:2],
                currency=currency,
                history_end=hist_end,
                history_periods=int(hist_periods),
            )
            study_errs = study.validate()
            if study_errs:
                for e in study_errs:
                    st.error(f"❌ {e}")
                return

            # ── Monta MappingConfig ───────────────────────────────────────
            dim_map = {d: d for d in ordered_dims}
            if layout == Layout.WIDE:
                # O motor de dados espera que measure_columns tenha 1 chave para inferir
                # a medida no formato largo quando não há coluna de rótulo.
                mapping = MappingConfig(
                    key_columns=ordered_dims,
                    dimension_columns=dim_map,
                    period_column=None,
                    measure_columns=meas_map,
                    wide_period_map=auto_period_map,
                    delimiter=delimiter,
                    encoding=encoding,
                    decimal_separator=dec,
                    date_format="YYYY-MM",
                    key_json_order=ordered_dims,
                )
            else:
                mapping = MappingConfig(
                    key_columns=ordered_dims,
                    dimension_columns=dim_map,
                    period_column=period_col,
                    measure_columns=meas_map,
                    wide_period_map={},
                    delimiter=delimiter,
                    encoding=encoding,
                    decimal_separator=dec,
                    date_format="YYYY-MM",
                    key_json_order=ordered_dims,
                )

            # ── Normaliza e persiste (idempotente — T3.2/B4) ───────────────
            content = st.session_state["_draft_file_content"]
            file_name = st.session_state["_draft_file_name"]
            content_hash = hashlib.sha256(content).hexdigest()
            sig = hashlib.sha256(
                (study.to_json() + mapping.to_json() + content_hash).encode("utf-8")
            ).hexdigest()

            if (
                st.session_state.get("_import_sig") == sig
                and st.session_state.get("dataset_id") is not None
            ):
                # Configuração idêntica já confirmada nesta sessão — não salva de
                # novo (evita duplicar dataset num duplo clique), só avança.
                st.session_state.step = 2
                st.rerun()
            else:
                with st.spinner("Normalizando dados..."):
                    ds = data_engine.normalize_file(content, file_name, study, mapping)

                _reset_downstream(2)
                st.session_state.study = study
                st.session_state.mapping = mapping
                st.session_state.dataset = ds

                conn = _db()
                dataset_id = data_engine.save_dataset(
                    conn,
                    study,
                    mapping,
                    ds,
                    filename=file_name,
                    file_sha256=content_hash,
                )
                st.session_state.dataset_id = dataset_id
                st.session_state.profile = data_engine.profile_data(ds, study)
                st.session_state["_import_sig"] = sig

                flash = (
                    f"✅ Dataset salvo com sucesso! (ID: {dataset_id[:8]}…) "
                    f"Total de observações: **{ds.observations.height}** · "
                    f"Entidades: **{ds.entities.height}**."
                )
                if ds.issues:
                    flash += f" ⚠️ {len(ds.issues)} avisos de qualidade a revisar."
                st.session_state["_flash"] = flash
                st.session_state.step = 2
                st.rerun()
