"""Forecast Community - Streamlit App (ponto de entrada fino).

A interface foi fatiada em app_ui/ (shell, etapas e roteamento);
este arquivo apenas configura a pagina e chama main().
"""

import streamlit as st

st.set_page_config(page_title="Forecast Community", layout="wide")

from app_ui.main import main  # noqa: E402
from app_ui.assumptions import _from_grid  # noqa: E402,F401
from app_ui.shell import _parse_period_col  # noqa: E402,F401


if __name__ == "__main__":
    main()
