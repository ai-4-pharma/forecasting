"""Forecast Community - interface Streamlit e coordenacao do fluxo.

Este modulo coordena chamadas aos engines (data_engine/forecast_engine).
Regra de negocio permanece nos engines; aqui ficam formularios e estado.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import uuid
import datetime as dt
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import duckdb
import plotly.graph_objects as go
import polars as pl
import streamlit as st

import data_engine
import forecast_engine
from contracts import (
    DEFAULT_BATCH_SIZE,
    MAX_REGRESSORS_PER_RUN,
    AssumptionRule,
    DuplicateAction,
    EffectType,
    ExportConfig,
    ExportFormat,
    ForecastConfig,
    ForecastMode,
    HierarchyConfig,
    HierarchyMode,
    Layout,
    MappingConfig,
    MatMode,
    MissingAction,
    NegativeAction,
    OutlierAction,
    RegressorFillPolicy,
    RegressorSpec,
    ResultFilter,
    RunInputs,
    RunStatus,
    RunSummary,
    ScenarioConfig,
    Severity,
    SourceFrequency,
    StudyConfig,
    TemporalView,
    TreatmentPolicy,
    validate_assumption_rule,
    validate_scenario_draft,
)

APP_VERSION = "0.2.0"

LOCAL_DIR = Path(__file__).parent / ".local"
LOCAL_DIR.mkdir(exist_ok=True)
DB_PATH = LOCAL_DIR / "forecast.duckdb"

st.set_page_config(page_title="Forecast Community", layout="wide")

_logger = logging.getLogger("forecast_community")
if not _logger.handlers:
    _logger.setLevel(logging.ERROR)
    _handler = RotatingFileHandler(
        LOCAL_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_handler)


# ---------------------------------------------------------------------------
# Estado de sessao
# ---------------------------------------------------------------------------


def _init_state() -> None:
    for k, dv in {
        "conn": None,
        "studies": None,
        "study": None,
        "mapping": None,
        "dataset_id": None,
        "dataset": None,
        "profile": None,
        "prepared": None,
        "preparation_id": None,
        "run_id": None,
        "nodes": None,
        # estado de execução (P31): gerador dirigido em avanço por lotes
        "run_gen": None,
        "run_cancel": False,
        "run_running": False,
        "run_last": None,
        "run_finalized": False,
        "run_config": None,
        # rascunho de importação (Etapas 1–2)
        "_draft_name": "",
        "_draft_layout": "long",
        "_draft_file_content": None,
        "_draft_file_name": "",
        "_draft_file_hash": "",
        "_draft_insp": None,
        "_draft_read_opts": {
            "encoding": None,
            "delimiter": None,
            "decimal_separator": ",",
        },
        "_import_sig": None,
        "step": 0,
        "_flash": None,
    }.items():
        if k not in st.session_state:
            st.session_state[k] = dv


# Número de eventos processados por avanço antes de devolver o controle à UI,
# permitindo cancelamento entre lotes sem prometer interrupção imediata (sec 4).
EVENTS_PER_RUN = 60

# ---------------------------------------------------------------------------
# Wizard: etapas, estado de navegação e liberação (Fase 2 do _plan_opus.md)
# ---------------------------------------------------------------------------

STEPS = ["Arquivo", "Mapeamento", "Qualidade", "Previsão", "Resultados"]


def _step_gate(i: int) -> tuple[bool, str]:
    """Diz se a etapa `i` está concluída (pode avançar) e, se não, o motivo.

    Não acessa o banco nem chama os engines — usa só o que já está em
    `st.session_state` para decidir se o botão "Avançar →" libera.
    """
    if i == 0:
        if st.session_state.get("_draft_insp") is None:
            return False, "Carregue um arquivo para continuar."
        if not str(st.session_state.get("_draft_name", "")).strip():
            return False, "Informe um nome para o estudo."
        return True, ""
    if i == 1:
        if st.session_state.get("dataset_id") is None:
            return False, "Confirme o mapeamento dos dados para continuar."
        return True, ""
    if i == 2:
        if st.session_state.get("preparation_id") is None:
            return False, "Prepare os dados (Etapa Qualidade) para continuar."
        return True, ""
    if i == 3:
        if (
            not st.session_state.get("run_finalized")
            or st.session_state.get("run_id") is None
        ):
            return False, "Execute uma rodada de previsão para continuar."
        return True, ""
    return True, ""


def _reset_downstream(from_step: int) -> None:
    """Zera o estado de sessão das etapas >= `from_step` (B5): evita que dados
    de um estudo/arquivo anterior vazem para etapas posteriores."""
    groups: dict[int, list[str]] = {
        1: ["study", "mapping", "dataset", "dataset_id", "profile", "_import_sig"],
        2: ["prepared", "preparation_id"],
        3: [
            "run_id",
            "run_gen",
            "run_cancel",
            "run_running",
            "run_last",
            "run_finalized",
            "run_config",
            "run_log",
            "scenarios",
            "regressors",
        ],
    }
    for step_i, keys in groups.items():
        if step_i >= from_step:
            for k in keys:
                st.session_state.pop(k, None)
    _init_state()


def _page_header(i: int) -> None:
    """Título padronizado da etapa `i` (`"N. Nome"`), coerente com o wizard."""
    st.header(f"{i + 1}. {STEPS[i]}")


def _nav_footer() -> None:
    """Rodapé fixo de navegação: ← Voltar | motivo do bloqueio | Avançar →."""
    st.divider()
    step = st.session_state.step
    ok, reason = _step_gate(step)
    c1, c2, c3 = st.columns([1, 4, 1])
    with c1:
        if st.button("← Voltar", key="nav_back", disabled=(step == 0)):
            st.session_state.step = max(0, step - 1)
            st.rerun()
    with c2:
        if not ok:
            st.caption(reason)
    with c3:
        is_last = step >= len(STEPS) - 1
        if st.button(
            "Avançar →",
            key="nav_next",
            type="primary",
            disabled=(not ok or is_last),
        ):
            st.session_state.step = min(len(STEPS) - 1, step + 1)
            st.rerun()


def _db() -> object:
    if st.session_state.conn is None:
        conn = data_engine.open_database(DB_PATH)
        data_engine.initialize_database(conn)
        data_engine.mark_interrupted_runs(conn)
        st.session_state.conn = conn
    return st.session_state.conn


# ---------------------------------------------------------------------------
# Helper: detectar coluna de periodo pelo nome
# ---------------------------------------------------------------------------


def _parse_period_col(col_name: str) -> str | None:
    """Tenta converter o nome da coluna num período ISO YYYY-MM-01 (data completa,
    exigida por `date.fromisoformat` em `data_engine.normalize_file`).
    Aceita: YYYYMM, YYYY-MM, YYYY/MM, YYYY-Qn, YYYY, datetime serializado
    (ex.: "2016-06-01 00:00:00"), número serial (ex.: "201606.0") e MM/YYYY.
    Retorna None se não reconhecer.
    """
    c = str(col_name).strip()
    # datetime já em formato ISO (ex.: openpyxl convertendo cabeçalho para texto)
    m = re.match(r"^(\d{4})-(\d{2})-\d{2}([ T].*)?$", c)
    if m:
        y, mth = int(m.group(1)), int(m.group(2))
        if 1 <= mth <= 12:
            return f"{y:04d}-{mth:02d}-01"
    m = re.match(r"^(\d{4})(\d{2})(\.0)?$", c)
    if m:
        y, mth = int(m.group(1)), int(m.group(2))
        if 1 <= mth <= 12:
            return f"{y:04d}-{mth:02d}-01"
    m = re.match(r"^(\d{4})[-/](\d{2})$", c)
    if m:
        y, mth = int(m.group(1)), int(m.group(2))
        if 1 <= mth <= 12:
            return f"{y:04d}-{mth:02d}-01"
    m = re.match(r"^(\d{2})/(\d{4})$", c)
    if m:
        mth, y = int(m.group(1)), int(m.group(2))
        if 1 <= mth <= 12:
            return f"{y:04d}-{mth:02d}-01"
    m = re.match(r"^(\d{4})-Q([1-4])$", c, re.IGNORECASE)
    if m:
        qstart = {1: "01", 2: "04", 3: "07", 4: "10"}
        return f"{m.group(1)}-{qstart[int(m.group(2))]}-01"
    if re.match(r"^\d{4}$", c):
        return f"{c}-01-01"
    return None


# ---------------------------------------------------------------------------
# Etapa 1 — Novo estudo: nome + formato + upload
# ---------------------------------------------------------------------------


def page_config_study():
    _page_header(0)
    st.caption("Planejamento de vendas e demanda — ferramenta local de previsão.")

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
                st.dataframe(insp.sample.head(5), use_container_width=True)

    st.caption(
        'Para abrir um estudo já salvo, use "Abrir estudo salvo" na barra lateral.'
    )


# ---------------------------------------------------------------------------
# Etapa 2 — Configurar (guiado pelas colunas reais do arquivo)
# ---------------------------------------------------------------------------


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
                    use_container_width=True,
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


# ---------------------------------------------------------------------------
# Qualidade (P13)
# ---------------------------------------------------------------------------


# Códigos de erro impeditivo que NENHUMA política de tratamento resolve — só
# corrigem voltando ao mapeamento (Etapa 2). E_DUPLICATE fica de fora: a
# política "Somar" resolve duplicidade.
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
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Diagnóstico por série")
    st.dataframe(prof.by_series.head(200), use_container_width=True)

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


# ---------------------------------------------------------------------------
# Premissas e execução (P21/P31)
# ---------------------------------------------------------------------------


def _dimension_values() -> dict[str, list[str]]:
    """Valores reais das dimensões das entidades para seletores de filtro."""
    ent = st.session_state.dataset.entities
    if ent is None or ent.height == 0:
        return {}

    values: dict[str, set] = {}
    for row in ent.to_dicts():
        try:
            dims = json.loads(row.get("dimensions_json", "{}"))
        except Exception:  # noqa: BLE001
            dims = {}
        for k, v in dims.items():
            values.setdefault(k, set()).add(str(v))
    return {k: sorted(v) for k, v in values.items()}


def _edit_assumptions() -> None:
    """Editor de cenários e regras (P21): alvo, família, medida, filtros,
    exclusões, vigência, tipo, taxa e prioridade, alimentado por atributos
    válidos das entidades. Persiste rascunhos e preserva o cenário base."""
    st.subheader("Premissas e cenários")
    dim_vals = _dimension_values()
    if not dim_vals:
        st.caption("Dimensões indisponíveis; cadastre regras sem filtro de atributo.")

    if "scenarios" not in st.session_state or "regressors" not in st.session_state:
        if st.session_state.dataset_id:
            _sc_tmp, _rg_tmp = data_engine.load_assumptions(
                _db(), st.session_state.dataset_id
            )
            st.session_state.scenarios = (
                _sc_tmp
                if any(s.scenario_id == "base" for s in _sc_tmp)
                else [ScenarioConfig()] + _sc_tmp
            )
            st.session_state.regressors = _rg_tmp
        else:
            st.session_state.scenarios = [ScenarioConfig()]
            st.session_state.regressors = []
    scenarios: list[ScenarioConfig] = [
        s for s in st.session_state.scenarios if s.scenario_id != "base"
    ]

    # cria novo cenário
    with st.form("novo_cenario"):
        new_id = st.text_input("Novo cenário (identificador)", key="new_sc_id")
        new_name = st.text_input("Nome", key="new_sc_name")
        if st.form_submit_button("Adicionar cenário") and new_id.strip():
            if new_id.strip() == "base":
                st.error("'base' é reservado e imutável.")
            elif any(s.scenario_id == new_id for s in scenarios):
                st.warning("Cenário já existe; edite abaixo.")
            else:
                scenarios.append(
                    ScenarioConfig(
                        scenario_id=new_id.strip(),
                        name=new_name.strip() or new_id.strip(),
                        description="",
                        rules=[],
                    )
                )
                st.session_state.scenarios = [ScenarioConfig()] + scenarios

    # editar regras de cada cenário por data_editor
    removed_scenario_ids: set[str] = set()
    for sc_idx, sc in enumerate(scenarios):
        with st.expander(f"Cenário: {sc.name} ({sc.scenario_id})", expanded=True):
            if st.button("Excluir cenário", key=f"del_sc_{sc_idx}"):
                removed_scenario_ids.add(sc.scenario_id)
                continue
            rules = sc.rules
            rule_rows = []
            for r in rules:
                if not hasattr(r, "rule_id"):
                    continue
                rule_rows.append(
                    {
                        "rule_id": r.rule_id,
                        "family": r.family,
                        "target_measure": r.target_measure,
                        "effect": r.effect.value,
                        "rate": r.rate,
                        "priority": r.priority,
                        "start_period": r.start_period.isoformat()
                        if r.start_period
                        else "",
                        "end_period": r.end_period.isoformat() if r.end_period else "",
                        "filters": _filters_to_text(r.filters),
                        "excluded_entity_ids": ",".join(r.excluded_entity_ids),
                        "enabled": r.enabled,
                    }
                )
            if not rule_rows:
                st.caption("Nenhuma regra ainda. Use 'Adicionar regra' abaixo.")
            else:
                edited = st.data_editor(
                    pl.DataFrame(rule_rows), num_rows="dynamic", key=f"rules_{sc_idx}"
                )
                new_rules = []
                for row in edited.to_dicts():
                    r = _row_to_rule(sc.scenario_id, row)
                    if r is not None:
                        errs = validate_assumption_rule(r)
                        if errs:
                            st.error(f"Regra '{r.rule_id}': " + "; ".join(errs))
                        else:
                            new_rules.append(r)
                sc.rules = new_rules

            st.markdown("**Adicionar regra**")
            with st.form(f"nova_regra_{sc_idx}"):
                ffilters = {}
                if dim_vals:
                    fdim = st.selectbox(
                        "Filtrar pela dimensão", [None] + list(dim_vals)
                    )
                    if fdim:
                        fvals = st.multiselect("Valores do atributo", dim_vals[fdim])
                        ffilters = {fdim: fvals}
                c1, c2, c3 = st.columns(3)
                fam = c1.text_input("Família (ex.: preco, promocao, cmed)", "")
                tam = c2.selectbox("Medida-alvo", ["valor", "unidades"])
                eff = c3.selectbox(
                    "Efeito",
                    ["step", "pulse", "annual_step"],
                    format_func={
                        "step": "Degrau (a partir do início)",
                        "pulse": "Pulso (entre início e fim)",
                        "annual_step": "Reajuste anual recorrente",
                    }.get,
                )
                rate = st.number_input("Taxa % (ex.: 5 = +5%)", -99.0, 1000.0, 0.0)
                prio = st.number_input("Prioridade (maior vence)", 0, 100, 1)
                d1, d2 = st.columns(2)
                sp = d1.date_input("Início da vigência", dt.date.today().replace(day=1))
                ep = d2.date_input("Fim (só para pulse)", value=None)
                if st.form_submit_button("Adicionar regra"):
                    r = AssumptionRule(
                        rule_id=f"r_{uuid.uuid4().hex[:8]}",
                        scenario_id=sc.scenario_id,
                        family=fam,
                        target_measure=tam,
                        filters=ffilters,
                        start_period=sp,
                        end_period=ep,
                        effect=EffectType(eff),
                        rate=rate / 100.0,
                        priority=int(prio),
                        enabled=True,
                    )
                    errs = validate_assumption_rule(r)
                    if errs:
                        st.error("; ".join(errs))
                    else:
                        sc.rules = rules + [r]
                        st.success("Regra adicionada.")

    # validação conjunta + persistência dos rascunhos
    scenarios = [sc for sc in scenarios if sc.scenario_id not in removed_scenario_ids]
    draft_errs: list[str] = []
    for sc in scenarios:
        draft_errs.extend(validate_scenario_draft(sc))
    if "regressors" not in st.session_state:
        st.session_state.regressors = []
    _edit_regressors()
    if st.session_state.dataset_id:
        sig = hashlib.sha256(
            (
                "".join(s.to_json() for s in scenarios)
                + "".join(r.to_json() for r in st.session_state.regressors)
            ).encode()
        ).hexdigest()
        if st.session_state.get("_assump_sig") != sig:
            conn = _db()
            data_engine.save_assumptions(
                conn,
                st.session_state.dataset_id,
                scenarios + [ScenarioConfig()],
                st.session_state.regressors,
            )
            st.session_state["_assump_sig"] = sig
    st.session_state.scenarios = [ScenarioConfig()] + scenarios
    if draft_errs:
        st.warning("Ajustes pendentes: " + "; ".join(set(draft_errs)))
    else:
        st.caption(
            "Cenário 'base' é imutável. Regras são ajustes incrementais sobre a "
            "previsão base; não representam a inflação/reajuste já implícitos no "
            "histórico. CMED afeta somente Valor. MAT direto ajusta o acumulado."
        )


def _filters_to_text(filters: dict[str, list[str]]) -> str:
    return "; ".join(f"{k}={','.join(v)}" for k, v in filters.items())


def _text_to_filters(text: str) -> dict[str, list[str]]:
    """Reconstrói filtros de dimensão a partir da coluna textual do editor."""
    out: dict[str, list[str]] = {}
    if not text:
        return out
    for part in str(text).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        vals = [x for x in v.split(",") if x]
        if vals:
            out[k.strip()] = vals
    return out


def _row_to_rule(scenario_id: str, row: dict) -> AssumptionRule | None:
    rid = str(row.get("rule_id", "")).strip()
    if not rid:
        return None
    start = (
        dt.date.fromisoformat(row["start_period"]) if row.get("start_period") else None
    )
    end = dt.date.fromisoformat(row["end_period"]) if row.get("end_period") else None
    return AssumptionRule(
        rule_id=rid,
        scenario_id=scenario_id,
        family=str(row.get("family", "")),
        target_measure=str(row.get("target_measure", "valor")),
        filters=_text_to_filters(row.get("filters", "")),
        excluded_entity_ids=[
            x for x in str(row.get("excluded_entity_ids", "")).split(",") if x
        ],
        start_period=start,
        end_period=end,
        effect=EffectType(row.get("effect", "step")),
        rate=float(row.get("rate", 0.0)),
        priority=int(row.get("priority", 0) or 0),
        enabled=bool(row.get("enabled", True)),
    )


def _forecast_periods(study, horizon: int) -> list[dt.date]:
    """Períodos futuros do horizonte, a partir do último período fechado."""
    if study.history_end is None:
        return []
    return [
        forecast_engine._add_period(study.history_end, study.model_frequency, i)
        for i in range(1, int(horizon) + 1)
    ]


def _preview_assumptions(
    scenarios: list[ScenarioConfig],
    entities,
    periods: list[dt.date],
):
    """Prévia (P22): entidades atingidas e fatores calculados por cenário."""
    preview: list[tuple] = []
    issues: list = []
    for sc in scenarios:
        if getattr(sc, "scenario_id", "base") == "base":
            continue
        rules = [r for r in sc.rules if hasattr(r, "rule_id") and r.enabled]
        if not rules:
            continue
        confl = forecast_engine.scenario_conflicts(entities, periods, rules)
        if confl:
            issues.extend(confl)
            continue
        compiled = forecast_engine.compile_assumptions(entities, periods, rules)
        if compiled.height:
            summary = compiled.group_by(["measure", "family"]).agg(
                pl.col("entity_id").n_unique().alias("n_entidades"),
                pl.col("period").n_unique().alias("n_periodos"),
            )
            preview.append((sc, compiled, summary))
    return preview, issues


def _parse_pv(text: str) -> dict[str, float]:
    """Parseia 'periodo=valor' por linha em dict período(ISO) -> float."""
    out: dict[str, float] = {}
    for line in str(text).splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        try:
            out[k.strip()] = float(v.strip())
        except ValueError:
            continue
    return out


def _grid(pv: dict[str, float]) -> pl.DataFrame:
    if not pv:
        return pl.DataFrame({"periodo": [], "valor": []})
    return pl.DataFrame({"periodo": sorted(pv), "valor": [pv[k] for k in sorted(pv)]})


def _from_grid(df) -> dict[str, float]:
    """Converte o retorno de `st.data_editor` (polars ou pandas) em dict.

    Importante: usar o **valor retornado** pela chamada de `st.data_editor`,
    não `st.session_state[key]` — a chave de sessão de um data_editor guarda
    apenas o diff de edições (`edited_rows`/`added_rows`/`deleted_rows`), não
    a tabela completa (B2).
    """
    if hasattr(df, "to_dicts"):
        rows = df.to_dicts()
    elif hasattr(df, "to_dict"):
        rows = df.to_dict("records")
    else:
        rows = list(df)
    out: dict[str, float] = {}
    for r in rows:
        p = str(r.get("periodo", "")).strip()
        v = r.get("valor")
        if not p or v is None:
            continue
        try:
            out[p] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _repeat_last(hist: dict[str, float], n: int) -> dict[str, float]:
    """Repete o último valor histórico por n períodos futuros (comando explícito)."""
    if not hist:
        return {}
    last = max(hist.keys())
    v = hist[last]
    study = st.session_state.study
    if study is None or study.history_end is None:
        end = dt.datetime.fromisoformat(last).date()
        return {f"{(dt.date(end.year, end.month, 1))}": v}
    periods = _forecast_periods(study, n)
    return {p.isoformat(): v for p in periods}


def _edit_regressors() -> None:
    """Cadastro de regressoras (P23): grade histórica/futura, repetição explícita."""
    st.markdown("### Regressoras (efeito aprendido)")
    dim_vals = _dimension_values()
    if "regressors" not in st.session_state:
        st.session_state.regressors = []
    regs: list[RegressorSpec] = st.session_state.regressors
    st.caption(
        f"Até {MAX_REGRESSORS_PER_RUN} regressoras. Necessitam de histórico variável e "
        "cobertura futura; sem isso o candidato AutoARIMA_X fica indisponível."
    )
    with st.form("nova_regressora"):
        name = st.text_input("Nome da variável (único)", key="nr_name")
        unit = st.text_input("Unidade", key="nr_unit")
        known = st.checkbox(
            "Conhecida antecipadamente (disponível no corte)", True, key="nr_known"
        )
        fill = st.selectbox(
            "Política quando desconhecida",
            ["none", "explicit_hold"],
            format_func={
                "none": "Sem política (indisponível)",
                "explicit_hold": "Repetir último valor (explicit_hold)",
            }.get,
            key="nr_fill",
        )
        scope: dict[str, list[str]] = {}
        if dim_vals:
            sdim = st.selectbox("Escopo por dimensão", [None] + list(dim_vals))
            if sdim:
                scope = {sdim: st.multiselect("Valores", dim_vals[sdim])}
        hist_txt = st.text_area(
            "Histórico (período=valor por linha, ex.: 2026-07=1.0)", key="nr_h"
        )
        fut_txt = st.text_area("Futuro (período=valor por linha)", key="nr_f")
        if st.form_submit_button("Adicionar regressora"):
            if not name.strip():
                st.error("Informe o nome da variável.")
            elif len(regs) >= MAX_REGRESSORS_PER_RUN:
                st.error(f"Máximo de {MAX_REGRESSORS_PER_RUN} regressoras por rodada.")
            elif any(r.name == name.strip() for r in regs):
                st.warning("Regressora já existe; edite abaixo.")
            else:
                regs.append(
                    RegressorSpec(
                        regressor_id=f"reg{len(regs) + 1}",
                        name=name.strip(),
                        unit=unit,
                        scope_filters=scope,
                        known_in_advance=known,
                        fill_policy=RegressorFillPolicy(fill),
                        history_values=_parse_pv(hist_txt),
                        future_values=_parse_pv(fut_txt),
                        enabled=True,
                    )
                )
                st.session_state.regressors = regs
    for i, r in enumerate(regs):
        with st.expander(f"{r.name} (`{r.regressor_id}`)"):
            r.enabled = st.checkbox("Habilitada", value=r.enabled, key=f"ren_{i}")
            nrep = st.number_input(
                "Repetir último valor histórico por N períodos ao futuro",
                0,
                120,
                0,
                key=f"rn_{i}",
            )
            if st.button("Repetir último valor → futuro", key=f"rb_{i}"):
                r.future_values = _repeat_last(r.history_values, int(nrep or 12))
            st.caption("Grade histórico (edite as células):")
            hist_edited = st.data_editor(
                _grid(r.history_values),
                key=f"rh_{i}",
                num_rows="dynamic",
                use_container_width=True,
            )
            r.history_values = _from_grid(hist_edited)
            st.caption("Grade futuro (edite as células):")
            fut_edited = st.data_editor(
                _grid(r.future_values),
                key=f"rf_{i}",
                num_rows="dynamic",
                use_container_width=True,
            )
            r.future_values = _from_grid(fut_edited)
            st.session_state.regressors = regs
    live = [r for r in regs if r.enabled]
    if live:
        future_p = (
            _forecast_periods(st.session_state.study, 24)
            if st.session_state.study
            else []
        )
        for issue in forecast_engine.validate_regressors(live, {}, future_p):
            if issue.severity.value == "error":
                st.error(issue.message)
            else:
                st.warning(issue.message)


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
    horizon = st.number_input("Horizonte (períodos)", 1, max_h, min(12, max_h))
    if horizon > 24:
        st.warning(
            "Projeções de longo prazo têm elevada incerteza e dependem das premissas"
            " adotadas. O horizonte validado historicamente pode ser menor que o"
            " projetado. A interpretação e condução do estudo são de responsabilidade"
            " do usuário."
        )
    mode = st.selectbox(
        "Modo",
        ["fast", "advanced"],
        format_func={
            "fast": "Rápido (ETS/Theta/baselines e intermitência)",
            "advanced": "Avançado (+ CES, ARIMA e global ML)",
        }.get,
    )
    with st.expander("Opções avançadas"):
        enable_ml = st.checkbox(
            "Habilitar aprendizado global (MLForecast + LightGBM)", value=False
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
    with st.expander("Modelos para comparar (seletor SFE)", expanded=True):
        st.caption(
            "Modo comparador: roda TODOS os marcados e mostra leaderboard "
            "MAE/RMSE/WAPE/Bias por série. Nenhum vencedor automático é imposto."
        )
        probe = ForecastConfig(mode=ForecastMode(mode), enable_ml=enable_ml, seed=42)
        all_cands = forecast_engine.build_candidates(None, study, probe)
        all_aliases = [c.alias for c in all_cands]
        if enable_ml:
            for _ml in ("LightGBM", "XGBoost"):
                if _ml not in all_aliases:
                    all_aliases.append(_ml)
        default_sel = [
            a for a in all_aliases if a not in ("AutoCES", "AutoTBATS", "XGBoost")
        ]
        selected_models = st.multiselect(
            "Selecione os modelos",
            options=all_aliases,
            default=default_sel,
            format_func=lambda a: f"{forecast_engine.model_label(a)} ({a})",
            key="run_selected_models",
        )
        if not selected_models:
            st.warning("Selecione ao menos 1 modelo para executar.")
        st.caption(
            "Dia-a-dia: Média móvel 3/6/12M, Último valor, Regressão/Drift, "
            "Holt, ETS/ETS-D, ARIMA/SARIMA, TBATS. Intermitentes: Croston-SBA/TSB. "
            "Global ML: LightGBM/XGBoost (requer requirements-ml.txt)."
        )
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
            st.dataframe(summary, use_container_width=True)
            st.dataframe(compiled.head(200), use_container_width=True)
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
                "`pip install -r requirements-ml.txt` para usar o modo global;"
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


# ---------------------------------------------------------------------------
# Dashboard e exportação (P33/P34/P35)
# ---------------------------------------------------------------------------


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

    f_measure = st.selectbox("Medida", measures) if measures else None
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
        "Grão (dimensão — Etapa 2 Mapeamento B)",
        dim_names,
        index=0,
        help="Nível 1 (topo) ao Nível 5 (folha). Ex.: Classe Terap_IV, Molécula_inglês, Produto, SKU (Completa), FCC.",
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
        default=item_pool,
        help="O gráfico e a tabela refletem EXATAMENTE estes valores (soma das folhas). Ex.: N05A1 vs N05A9.",
        key="dash_items",
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
    # anexa valor do grão a cada folha
    grain_of = {nid: str(dv.get(f_grain, "")).strip() for nid, dv in leaf_dims.items()}
    preds = (
        leaf_pred.with_columns(
            pl.col("node_id")
            .map_elements(
                lambda n: grain_of.get(str(n), str(n)), return_dtype=pl.String
            )
            .alias("_grain")
        )
        .group_by(["_grain", "ds", "measure", "scenario_id", "level"])
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
    cards, metrics = d.cards, d.metrics
    node_labels = {v: v for v in f_items}

    # identificação de resultado parcial (sec 8.2; nunca preencher faltas com zero)
    partial = run_status in (RunStatus.PARTIAL, RunStatus.CANCELLED)
    if partial:
        st.warning("Estes resultados são parciais e não representam a série completa.")

    # Cards (12.1): totais do horizonte, cobertura e erro de backtest identificado.
    # Em MAT não se soma posições: mostra-se a posição final e a variação vs a
    # posição histórica equivalente, com N/D quando não há base comparável. Só
    # calculados na view "padrão" (1 grão, sem recorte de itens) — misturar
    # grãos ou recortar itens tornaria a soma/erro incoerente (sec 11).
    study = data_engine.load_run_study(conn, run_id)
    is_mat = study.mat_mode in (MatMode.DERIVED, MatMode.DIRECT)
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Séries na view", preds["node_id"].n_unique() if preds.height else 0)
    with c2:
        if cards.height:
            card = cards.to_dicts()[0]
            label = "Posição final (MAT)" if is_mat else "Total do horizonte"
            value = card.get("mat_final") if is_mat else card.get("total_forecast")
            st.metric(label, _fmt_num(value))
        else:
            st.metric(
                "Total do horizonte",
                "N/D",
                help="Disponível só com 1 grão selecionado e sem recorte de itens.",
            )
    with c3:
        if metrics.height:
            m = metrics.to_dicts()[0]
            st.metric(
                "MAE do vencedor (backtest)",
                f"{m['mae']:.2f}",
                delta=None,
                help=f"erro avaliado em {m['n_eval']} pares / {m['n_folds']} folds"
                f" (cv_horizon {m['cv_horizon']})",
            )
        else:
            st.metric("MAE do vencedor (backtest)", "N/D")
    if cards.height:
        card = cards.to_dicts()[0]
        r2, r3 = st.columns(2)
        if is_mat:
            r2.markdown(
                f"**Posição histórica equivalente:** {_fmt_num(card.get('mat_history'))}"
                f" ({_fmt_pct(card.get('mat_variation'))})"
            )
        else:
            r2.markdown(
                f"**Variação vs janela histórica equivalente:**"
                f" {_fmt_pct(card.get('variation'))}"
            )
        if card.get("mat_final") is not None and not is_mat:
            r3.markdown(
                f"**MAT derivado (visão):** {_fmt_num(card['mat_final'])}"
                f" ({_fmt_pct(card['mat_variation'])})"
            )

    # 🏆 Leaderboard comparador — todos os modelos lado a lado, sem decidir por você.
    st.subheader("🏆 Leaderboard — comparador de modelos (backtest)")
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
        lb = lb.with_columns(
            pl.col("model_alias")
            .map_elements(
                lambda a: forecast_engine.model_label(a), return_dtype=pl.String
            )
            .alias("Modelo (negócio)")
        ).select(
            [
                "Modelo (negócio)",
                "model_alias",
                "MAE_medio",
                "RMSE_medio",
                "WAPE_medio",
                "Bias_medio",
                "n_series",
            ]
        )
        st.dataframe(lb, use_container_width=True)
        st.caption(
            "MAE/RMSE na unidade da medida (menor é melhor). WAPE = erro % ponderado. "
            "Bias + = superestima. Escolha o modelo por série abaixo no gráfico."
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

    # Gráfico fiel: 1 linha de histórico + 1 de previsão (vencedor agregado)
    # por valor da dimensão, + overlay opcional por modelo (soma das folhas).
    plot_items = list(f_items)
    MAX_PLOT = 12
    if len(plot_items) > MAX_PLOT:
        st.warning(
            f"{len(plot_items)} itens — gráfico com os {MAX_PLOT} primeiros. "
            "Refine os Itens para comparar menos séries."
        )
        plot_items = plot_items[:MAX_PLOT]
    candidate_opts = (
        sorted(scores.filter(pl.col("eligible"))["model_alias"].unique().to_list())
        if scores.height
        else []
    )
    compare_models = st.multiselect(
        "Modelos no gráfico (projeções executadas)",
        candidate_opts,
        default=candidate_opts[:4],
        format_func=lambda a: f"{forecast_engine.model_label(a)} ({a})",
        help="Para cada valor + modelo, soma as folhas recalculadas sob demanda (com cache). Grãos com muitas folhas têm overlay limitado — detalhe no SKU para comparação exaustiva.",
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
        p = (
            preds.filter(pl.col("node_id") == gv).sort("ds")
            if preds.height
            else pl.DataFrame()
        )
        if p.height:
            fig.add_trace(
                go.Scatter(
                    x=p["ds"],
                    y=p["yhat"],
                    mode="lines",
                    name=f"{gv} · previsão (vencedor)",
                )
            )
    MAX_LEAVES_OVERLAY = 25
    for gv in plot_items:
        leaves = grain_to_leaves.get(gv, [])
        if not leaves or not compare_models:
            continue
        if len(leaves) > MAX_LEAVES_OVERLAY:
            st.caption(
                f"{gv}: {len(leaves)} folhas — overlay por modelo limitado a {MAX_LEAVES_OVERLAY} folhas. "
                "Use o leaderboard abaixo ou detalhe por SKU."
            )
            continue
        for m in compare_models:
            by_ds: dict = {}
            ok = True
            for leaf in leaves:
                cdf, status = data_engine.forecast_candidate_for_node(
                    conn, run_id, leaf, f_measure, m
                )
                if status != "ok" or not cdf.height:
                    ok = False
                    break
                for r in cdf.to_dicts():
                    by_ds[r["ds"]] = by_ds.get(r["ds"], 0.0) + float(r["yhat"] or 0.0)
            if ok and by_ds:
                ds_sorted = sorted(by_ds)
                fig.add_trace(
                    go.Scatter(
                        x=ds_sorted,
                        y=[by_ds[d] for d in ds_sorted],
                        mode="lines",
                        name=f"{gv} · {forecast_engine.model_label(m)}",
                        line={"dash": "dash"},
                    )
                )
            else:
                st.caption(f"{gv} · {m}: sem overlay (falha em folha).")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("📋 Tabela executiva — soma por valor da dimensão")
    table_df = (
        preds.filter(pl.col("node_id").is_in(plot_items))
        .select("node_id", "ds", "yhat")
        .sort(["node_id", "ds"])
    )
    if layout_saida == "largo" and table_df.height:
        table_df = table_df.pivot(
            on="ds", index="node_id", values="yhat", aggregate_function="first"
        ).sort("node_id")
    st.dataframe(table_df, use_container_width=True)

    with st.expander("Tabelas de diagnóstico"):
        st.caption(
            "**MAE/RMSE**: erro médio absoluto/quadrático do backtest (menor é"
            " melhor, mesma unidade da medida). **WAPE**: erro % ponderado pelo"
            " volume. **Bias**: viés médio (positivo = superestima). **Eligible**:"
            " a série teve histórico mínimo para o modelo competir. **Selected**:"
            " modelo escolhido como vencedor desta série. **Fallback_used**: o"
            " vencedor falhou e outro modelo assumiu."
        )
        st.subheader("Vencedor por série")
        if scores.height:
            st.dataframe(
                scores.with_columns(
                    pl.col("mae").round(3),
                    pl.col("rmse").round(3),
                ).head(200),
                use_container_width=True,
            )
        else:
            st.caption("Sem métricas para este recorte.")
        st.subheader("Backtest do vencedor")
        if metrics.height:
            st.dataframe(
                metrics.with_columns(pl.col(pl.Float64).round(3)),
                use_container_width=True,
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


def _ml_available() -> bool:
    try:
        import mlforecast  # noqa: F401
        import lightgbm  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Roteamento
# ---------------------------------------------------------------------------


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


if __name__ == "__main__":
    main()
