"""app_ui.shell — Wizard UI: estado de sessao, navegacao/gates, headers, DB e helpers comuns.
(extraido de app.py single-file; split 16/09/2026).
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import streamlit as st

APP_VERSION = "0.3.0"
_LOCAL_BASE = Path(__file__).resolve().parent.parent  # raiz do forecasting
LOCAL_DIR = _LOCAL_BASE / ".local"
LOCAL_DIR.mkdir(exist_ok=True)
DB_PATH = LOCAL_DIR / "forecast.duckdb"

_logger = logging.getLogger("forecast_community")
if not _logger.handlers:
    _logger.setLevel(logging.ERROR)
    _handler = RotatingFileHandler(
        LOCAL_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_handler)

import data_engine
import forecast_engine

# Eventos consumidos por avanço do gerador na Etapa 4. A fase de previsão final
# emite um evento por modelo concluído (feedback fino em métodos lentos), então o
# lote precisa ser maior para não multiplicar o número de reruns da página.
EVENTS_PER_RUN = 150
MODEL_EXPLANATIONS = {
    "Naive": ("Último valor", "Repete o comportamento mais recente."),
    "MediaMovel3": ("Média móvel de 3 períodos", "Suaviza oscilações muito recentes."),
    "MediaMovel6": ("Média móvel de 6 períodos", "Suaviza o histórico de curto prazo."),
    "MediaMovel12": (
        "Média móvel de 12 períodos",
        "Usa uma referência anual de nível médio.",
    ),
    "HistoricAverage": ("Média histórica", "Usa o nível médio observado no histórico."),
    "SeasonalNaive": ("Sazonal", "Repete o padrão típico do mesmo período do ciclo."),
    "RegLinearDrift": (
        "Tendência",
        "Projeta a direção de crescimento ou queda observada.",
    ),
    "Holt": ("Holt", "Equilibra nível atual e tendência recente."),
    "HoltDamped": (
        "Holt amortecido",
        "Projeta tendência, reduzindo seu efeito gradualmente.",
    ),
    "AutoETS": ("ETS", "Combina automaticamente nível, tendência e sazonalidade."),
    "ETS_Damped": ("ETS amortecido", "ETS com tendência suavizada no longo prazo."),
    "AutoTheta": ("Theta", "Método estatístico robusto para séries regulares."),
    "CrostonSBA": (
        "Croston-SBA",
        "Indicado para demanda intermitente, com muitos zeros.",
    ),
    "TSB": ("TSB", "Indicado para demanda esporádica e possíveis descontinuidades."),
    "AutoARIMA": (
        "ARIMA automático",
        "Busca padrões temporais mais complexos no histórico.",
    ),
    "AutoCES": ("CES automático", "Alternativa estatística avançada de suavização."),
    "AutoTBATS": ("TBATS automático", "Útil quando a sazonalidade é mais complexa."),
    "AutoARIMA_X": (
        "ARIMA com variáveis externas",
        "Usa variáveis futuras conhecidas, como preço ou campanha.",
    ),
}
STEPS = ["Arquivo", "Mapeamento", "Qualidade", "Previsão", "Resultados"]


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


def _model_name(alias: str) -> str:
    """Nome de negócio de um candidato, com fallback para o motor."""
    return MODEL_EXPLANATIONS.get(alias, (forecast_engine.model_label(alias), ""))[0]


def _model_explanation(alias: str) -> str:
    return MODEL_EXPLANATIONS.get(alias, ("", "Método estatístico disponível."))[1]


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


# _parse_period_col vive em data_engine.dates (S2.2); reexportado aqui para não
# quebrar `app_ui.mapping` e demais importadores existentes.
from data_engine.dates import _parse_period_col  # noqa: E402,F401
