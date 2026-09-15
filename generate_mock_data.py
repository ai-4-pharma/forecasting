"""Forecast Community - gerador de dados sinteticos e benchmark reproduzivel.

Cria fixtures deterministicas (semente fixa) para os testes dos gates e para
o benchmark da secao 12.3. Nunca requer dados reais ou servico externo.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import polars as pl


def _monthly_dates(history_end: dt.date, n: int) -> list[dt.date]:
    out = []
    for k in range(n, 0, -1):
        i = history_end.year * 12 + (history_end.month - 1) - (k - 1)
        out.append(dt.date(i // 12, i % 12 + 1, 1))
    return out


def make_long(
    history_end: dt.date = dt.date(2026, 8, 1),
    n_periods: int = 60,
    n_entities: int = 20,
    seed: int = 42,
    measures=("unidades", "valor"),
    classes=("Classe A", "Classe B"),
    molecules=("Mol A", "Mol B"),
    brands=("Marca X", "Marca Y"),
) -> pl.DataFrame:
    """Gera CSV no formato A (longo) com tendência + sazonalidade mensal."""
    rng = np.random.default_rng(seed)
    dates = _monthly_dates(history_end, n_periods)
    rows: list[dict] = []
    for e in range(n_entities):
        classe = classes[e % len(classes)]
        onda = molecules[e % len(molecules)]
        marca = brands[e % len(brands)]
        ean = f"0123456789{1021 + e:04d}" if e < 9000 else f"9{e:012d}"
        base_u = 100 + e * 3 + rng.normal(0, 5)
        for j, d in enumerate(dates):
            seasonal = (np.sin(2 * np.pi * j / 12) + 1) * 0.25 * base_u
            unidades = max(
                0, int(base_u + seasonal + (0.3 + e * 0.05) * j + rng.normal(0, 8))
            )
            valor = unidades * (1.5 + 0.01 * (j % 6))
            row = {
                "classe": classe,
                "molecula": onda,
                "marca": marca,
                "ean": ean,
                "periodo": d.strftime("%Y-%m"),
            }
            if "unidades" in measures:
                row["unidades"] = unidades
            if "valor" in measures:
                row["valor"] = round(valor, 2)
            rows.append(row)
    return pl.DataFrame(rows)


def make_wide(
    history_end: dt.date = dt.date(2026, 8, 1),
    n_periods: int = 60,
    n_entities: int = 20,
    seed: int = 42,
    measures=("unidades",),
) -> pl.DataFrame:
    """Gera CSV no formato B (largo): entidade/medida + colunas 'Mes k'."""
    rng = np.random.default_rng(seed + 1)
    dates = _monthly_dates(history_end, n_periods)
    rows: list[dict] = []
    for e in range(n_entities):
        for m in measures:
            row = {
                "classe": f"Classe {e % 2}",
                "marca": f"Marca {e % 3}",
                "ean": f"0123456789{1021 + e:04d}",
                "medida": m,
            }
            for k, d in enumerate(dates):
                base = 40 + e * 2 + rng.normal(0, 3)
                v = base + (np.sin(2 * np.pi * k / 12) + 1) * 8 + 0.2 * k
                row[f"Mes {n_periods - k}"] = (
                    int(v) if m == "unidades" else round(v * 1.6, 2)
                )
            rows.append(row)
    return pl.DataFrame(rows)


def make_intermittent(
    history_end: dt.date = dt.date(2026, 8, 1), n_periods: int = 60, seed: int = 7
) -> pl.DataFrame:
    """Série intermitente (muitos zeros/picos) para teste de CrostonSBA/TSB."""
    rng = np.random.default_rng(seed)
    dates = _monthly_dates(history_end, n_periods)
    rows = []
    for e in range(5):
        for j, d in enumerate(dates):
            if rng.random() < 0.45:
                y = rng.integers(1, 40)
            else:
                y = 0
            rows.append(
                {
                    "classe": f"C{e}",
                    "marca": f"M{e}",
                    "ean": f"9{e:012d}",
                    "periodo": d.strftime("%Y-%m"),
                    "unidades": y,
                }
            )
    return pl.DataFrame(rows)


def make_constant(
    history_end: dt.date = dt.date(2026, 8, 1), value: float = 42.0, n_periods: int = 60
) -> pl.DataFrame:
    """Série constante."""
    dates = _monthly_dates(history_end, n_periods)
    return pl.DataFrame(
        {
            "classe": "C",
            "marca": "M",
            "ean": "0123456789012",
            "periodo": [d.strftime("%Y-%m") for d in dates],
            "unidades": [value] * n_periods,
        }
    )


def make_all_zero(
    history_end: dt.date = dt.date(2026, 8, 1), n_periods: int = 60
) -> pl.DataFrame:
    dates = _monthly_dates(history_end, n_periods)
    return pl.DataFrame(
        {
            "classe": "C",
            "marca": "M",
            "ean": "0123456789013",
            "periodo": [d.strftime("%Y-%m") for d in dates],
            "unidades": [0] * n_periods,
        }
    )


def make_negative(
    history_end: dt.date = dt.date(2026, 8, 1), n_periods: int = 60
) -> pl.DataFrame:
    """Série com negativos aprovados (venda líquida), para teste do piso."""
    dates = _monthly_dates(history_end, n_periods)
    vals = [100 + 2 * j for j in range(n_periods)]
    vals[10] = -30
    vals[11] = -12
    return pl.DataFrame(
        {
            "classe": "C",
            "marca": "M",
            "ean": "0123456789014",
            "periodo": [d.strftime("%Y-%m") for d in dates],
            "unidades": vals,
        }
    )


def make_short(
    history_end: dt.date = dt.date(2026, 8, 1), n_periods: int = 5
) -> pl.DataFrame:
    """Histórico curto (insuficiente para CV)."""
    dates = _monthly_dates(history_end, n_periods)
    return pl.DataFrame(
        {
            "classe": "C",
            "marca": "M",
            "ean": "0123456789015",
            "periodo": [d.strftime("%Y-%m") for d in dates],
            "unidades": [10 * (j + 1) for j in range(n_periods)],
        }
    )


def make_benchmark(
    history_end: dt.date = dt.date(2026, 8, 1),
    n_entities: int = 5000,
    n_periods: int = 60,
    seed: int = 42,
) -> pl.DataFrame:
    """Benchmark reproduzivel 5000 × 60 (secao 12.3)."""
    return make_long(
        history_end=history_end,
        n_periods=n_periods,
        n_entities=n_entities,
        seed=seed,
        brands=[f"Marca {i % 50}" for i in range(50)],
    )


def to_csv(df: pl.DataFrame, path: Path, sep: str = ";") -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    body = df.write_csv(separator=sep)
    Path(path).write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))


if __name__ == "__main__":
    outdir = Path("exports")
    to_csv(make_long(), outdir / "mock_long.csv")
    to_csv(make_wide(), outdir / "mock_wide.csv")
    to_csv(make_intermittent(), outdir / "mock_intermitente.csv")
    to_csv(make_benchmark(), outdir / "mock_benchmark_5000.csv")
    print("Fixtures geradas em", outdir.resolve())
