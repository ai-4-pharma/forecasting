"""api.state — conexao DuckDB compartilhada e registro de rodadas em memoria (S2.1).

Uma unica conexao serve o processo da API (padrao diferente do app_ui, que abre
uma conexao por sessao Streamlit); por isso toda operacao de escrita/leitura no
banco passa por `db_lock()` para serializar acesso entre threads de rodada.
`RUNS` guarda só estado de execucao (progresso, cancelamento, thread) — nunca
regra de negocio, que fica inteiramente em data_engine/forecast_engine.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import data_engine

_LOCAL_BASE = Path(__file__).resolve().parent.parent
LOCAL_DIR = _LOCAL_BASE / ".local"
LOCAL_DIR.mkdir(exist_ok=True)
DB_PATH = LOCAL_DIR / "forecast.duckdb"

_conn: object | None = None
_conn_lock = threading.Lock()

RUNS: dict[str, dict[str, Any]] = {}
RUNS_LOCK = threading.Lock()


def get_connection() -> object:
    """Conexao DuckDB unica do processo, criada e migrada na 1a chamada."""
    global _conn
    if _conn is None:
        conn = data_engine.open_database(DB_PATH)
        data_engine.initialize_database(conn)
        data_engine.mark_interrupted_runs(conn)
        _conn = conn
    return _conn


def db_lock() -> threading.Lock:
    """Lock a segurar em toda leitura/escrita na conexao compartilhada."""
    return _conn_lock
