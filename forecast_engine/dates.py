"""Frequencias, sazonalidade e aritmetica de periodos.

Parte do pacote forecast_engine (fatiado do single-file).
"""

from __future__ import annotations

import datetime as dt

from contracts import (
    SourceFrequency,
    CVWindow,
)


# ---------------------------------------------------------------------------
# Janelas CV (P16)
# ---------------------------------------------------------------------------


def _freq_str(freq: SourceFrequency) -> str:
    """Strings de frequência aceitas pelo StatsForecast 2.x (offset polars)."""
    if freq in (SourceFrequency.MONTHLY, SourceFrequency.MAT):
        return "1mo"
    if freq == SourceFrequency.QUARTERLY:
        return "3mo"
    return "1y"


def _pd_freq_str(freq: SourceFrequency) -> str:
    """Strings de frequência para MLForecast (pandas)."""
    if freq in (SourceFrequency.MONTHLY, SourceFrequency.MAT):
        return "MS"
    if freq == SourceFrequency.QUARTERLY:
        return "QS"
    return "YS"


def _season_length(freq: SourceFrequency) -> int:
    """Comprimento do ciclo sazonal por frequência (doc 5.3)."""
    if freq in (SourceFrequency.MONTHLY, SourceFrequency.MAT):
        return 12
    if freq == SourceFrequency.QUARTERLY:
        return 4
    return 1


def _effective_season_length(config, freq: SourceFrequency) -> int:
    """Ciclo sazonal efetivo: escolha do usuário (`config.season_length`) ou
    o padrão da frequência (`_season_length`).

    `None`/`0`/inválido caem no padrão — nunca devolve ciclo < 1.
    """
    try:
        sl = getattr(config, "season_length", None)
    except Exception:  # noqa: BLE001
        sl = None
    if sl is None:
        return _season_length(freq)
    try:
        sl = int(sl)
    except Exception:  # noqa: BLE001
        return _season_length(freq)
    return sl if sl >= 1 else _season_length(freq)


def _freq_from_windows(
    windows: list[CVWindow], default: SourceFrequency = SourceFrequency.MONTHLY
) -> SourceFrequency:
    """Inferência da frequência de modelagem a partir das janelas de CV."""
    if not windows or len(windows[0].eval_dates) < 2:
        return default
    d1, d2 = windows[0].eval_dates[0], windows[0].eval_dates[1]
    months = (d2.year - d1.year) * 12 + (d2.month - d1.month)
    if months == 3:
        return SourceFrequency.QUARTERLY
    if months == 12:
        return SourceFrequency.YEARLY
    return default


def _add_period(d: dt.date, freq: SourceFrequency, k: int) -> dt.date:
    if freq in (SourceFrequency.MONTHLY, SourceFrequency.MAT):
        idx = d.year * 12 + (d.month - 1) + k
        return dt.date(idx // 12, idx % 12 + 1, 1)
    if freq == SourceFrequency.QUARTERLY:
        q = (d.month - 1) // 3
        idx = d.year * 4 + q + k
        return dt.date(idx // 4, (idx % 4) * 3 + 1, 1)
    return dt.date(d.year + k, 1, 1)


# ---------------------------------------------------------------------------
# Previsão final e intervalos (P18)
# ---------------------------------------------------------------------------


def _gen_future_dates(last_ds: dt.date, freq: SourceFrequency, h: int) -> list[dt.date]:
    return [_add_period(last_ds, freq, i) for i in range(1, h + 1)]
