"""data_engine.dates — Helpers de datas, períodos e grid temporal.
(extraído de data_engine.py single-file; split 16/09/2026).
"""

from __future__ import annotations

import re
from datetime import date
from contracts import SourceFrequency, StudyConfig


def _shift_period(d: date, freq: SourceFrequency, k: int) -> date:
    year = d.year
    month = d.month
    if freq == SourceFrequency.MONTHLY or freq == SourceFrequency.MAT:
        total = year * 12 + (month - 1) + k
        return date(total // 12, total % 12 + 1, 1)
    if freq == SourceFrequency.QUARTERLY:
        q = (month - 1) // 3
        total = year * 4 + q + k
        return date(total // 4, (total % 4) * 3 + 1, 1)
    return date(year + k, 1, 1)


def _period_labels(study: StudyConfig, n: int) -> list[str]:
    """Rótulos de período no formato de origem (ex.: 2026-07)."""
    end = study.history_end or date(2026, 8, 1)
    out: list[str] = []
    for i in range(n):
        d = _shift_period(end, study.source_frequency, -i)
        out.append(_period_label(d, study.source_frequency))
    out.reverse()
    return out


def _period_label(d: date, freq: SourceFrequency) -> str:
    if freq == SourceFrequency.MONTHLY or freq == SourceFrequency.MAT:
        return f"{d.year:04d}-{d.month:02d}"
    if freq == SourceFrequency.QUARTERLY:
        return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"
    return f"{d.year:04d}"


def _parse_period(text: str, fmt: str, freq: SourceFrequency) -> date | None:
    """Converte rotulo de periodo em data do primeiro dia do periodo."""
    text = str(text).strip()
    if fmt == "YYYY-MM" or fmt is None:
        try:
            return date(int(text[:4]), int(text[5:7]), 1)
        except (ValueError, IndexError):
            return None
    if fmt == "YYYYMM":
        try:
            return date(int(text[:4]), int(text[4:6]), 1)
        except (ValueError, IndexError):
            return None
    if fmt == "YYYY-Qn":
        try:
            return date(int(text[:4]), (int(text[6]) - 1) * 3 + 1, 1)
        except (ValueError, IndexError):
            return None
    if fmt == "YYYY":
        try:
            return date(int(text), 1, 1)
        except ValueError:
            return None
    return None


def parse_date_main(text: str, fmt: str, freq: SourceFrequency) -> date | None:
    return _parse_period(text, fmt, freq)


def _parse_period_col(col_name: str) -> str | None:
    """Tenta converter o nome da coluna num período ISO YYYY-MM-01 (data completa,
    exigida por `date.fromisoformat` em `data_engine.normalize_file`).
    Aceita: YYYYMM, YYYY-MM, YYYY/MM, YYYY-Qn, YYYY, datetime serializado
    (ex.: "2016-06-01 00:00:00"), número serial (ex.: "201606.0") e MM/YYYY.
    Retorna None se não reconhecer. (movida de app_ui/shell.py em S2.2 para
    ser reaproveitada por `autodetect_mapping`.)
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


def build_time_grid(study: StudyConfig) -> list[date]:
    """Grade temporal de períodos completos até history_end."""
    n = study.history_periods
    end = study.history_end or date(2026, 8, 1)
    grid: list[date] = []
    for k in range(n, 0, -1):
        grid.append(_shift_period(end, study.source_frequency, -(k - 1)))
    return grid
