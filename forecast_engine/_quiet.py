"""Silenciamento de warnings ruidosos emitidos pelas bibliotecas nativas.

Parte do pacote forecast_engine (fatiado do single-file).

O `AutoARIMA` do statsforecast emite um `UserWarning` por ajuste quando o
otimizador do scipy nao converge ("possible convergence problem: minimize gave
code N"). Em execucao real (muitas series x folds x modelos) o aviso e reemitido
em cada processo filho do `multiprocessing` — no Windows o start method e
`spawn`, que nao herda os filtros de warnings do processo pai — e o console vira
um rio de mensagens identicas, sem valor para o usuario.

Este modulo concentra o silenciamento em dois niveis:

* `install_warning_filters()` — filtros persistentes no processo atual e, via
  `PYTHONWARNINGS`, tambem nos processos filhos criados por `spawn`
  (esse ambiente e lido pelo interpretador no startup do filho);
* `quiet_native()` — context manager usado nos pontos de ajuste/previsao.

Alem do aviso de convergencia, o scipy dispara `RuntimeWarning` de ponto
flutuante ("invalid value encountered...", "overflow encountered...") durante a
otimizacao do ARIMA. Como o numpy/scipy atribuem o warning ao arquivo chamador
(nao ao numpy), o filtro precisa citar `scipy.`/`numpy.` explicitamente.

O escopo e restrito as bibliotecas numericas de terceiros: warnings do proprio
projeto (contracts, data_engine, forecast_engine, app_ui) continuam aparecendo.
"""

from __future__ import annotations

import contextlib
import os
import warnings

_NOISY_MODULE_RE = (
    r"(statsforecast|utilsforecast|mlforecast|lightgbm|xgboost|sklearn"
    r"|scipy|numpy|numba)\."
)

_NOISY_CATEGORIES = (
    UserWarning,
    RuntimeWarning,
    FutureWarning,
    DeprecationWarning,
)

_ENV_VAR = "PYTHONWARNINGS"
_ENV_VALUE = ",".join(f"ignore::{c.__name__}" for c in _NOISY_CATEGORIES)


def _apply_filters() -> None:
    for category in _NOISY_CATEGORIES:
        warnings.filterwarnings("ignore", category=category, module=_NOISY_MODULE_RE)


def install_warning_filters() -> None:
    """Aplica os filtros no processo atual e nos processos filhos (spawn)."""
    _apply_filters()
    os.environ.setdefault(_ENV_VAR, _ENV_VALUE)


@contextlib.contextmanager
def quiet_native():
    """Silencia warnings das bibliotecas nativas durante ajuste/previsao.

    Uso tipico: envolver a construcao do `StatsForecast` e a chamada de
    `fit`/`forecast`, onde o ARIMA/ETS/TBATS emitem avisos de convergencia.
    """
    with warnings.catch_warnings():
        _apply_filters()
        yield
