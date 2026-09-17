"""data_engine.templates — Geração do modelo de arquivo (CSV/XLSX) para download.
(extraído de data_engine.py single-file; split 16/09/2026).
"""

from __future__ import annotations

import io
import polars as pl
import openpyxl
from contracts import ExportArtifact, Layout, StudyConfig
from .dates import _period_labels



def build_template(config: StudyConfig, file_format: str) -> ExportArtifact:
    """Gera template CSV ou XLSX com exemplos sinteticos e instrucoes."""
    freq = config.source_frequency
    n = 10
    dims = [d for d in config.dimension_names]
    periods = _period_labels(config, n)

    if config.layout == Layout.LONG:
        rows = []
        for idx, (d1, d2) in enumerate(
            [("Exemplo A", "Produto X"), ("Exemplo B", "Produto Y")]
        ):
            vals = {"classe": d1, "molecula": "", "marca": d2, "ean": ""}
            for j in range(n):
                row = {
                    d: (d1 if d == "classe" else d2 if d == "marca" else "")
                    for d in dims
                }
                row["periodo"] = periods[j]
                if "unidades" in config.measures:
                    row["unidades"] = f"{10 * (idx + 1) + j}"
                if "valor" in config.measures:
                    row["valor"] = f"{(10 * (idx + 1) + j) * 1.5:.2f}"
                rows.append(row)
        cols = (
            dims
            + ["periodo"]
            + ([c for c in ("unidades", "valor") if c in config.measures])
        )
        df = pl.DataFrame(rows, schema={c: pl.String for c in cols})
        header = ";".join(cols)
        lines = [header] + [";".join(str(r[c]) for c in cols) for r in rows]
        content = "\n".join(lines)
        meta = {
            "formato": "A-longo",
            "instrucoes": "Preencha uma linha por entidade/período/medida.",
            "periodos": periods,
        }
        if file_format == "xlsx":
            return _template_xlsx(config, df, "Template longo (Formato A)", periods)
        return ExportArtifact(
            f"template_{config.name}.csv",
            "text/csv",
            content.encode("utf-8-sig"),
            10,
            meta,
        )

    # Formato B - wide
    cols = dims + ["medida"] + periods
    rows = []
    for d1, d2 in [("Exemplo A", "Produto X"), ("Exemplo B", "Produto Y")]:
        for m in config.measures:
            row = {
                d: (d1 if d == "classe" else d2 if d == "marca" else "") for d in dims
            }
            row["medida"] = m
            for j in range(n):
                row[periods[j]] = (
                    f"{10 + j}" if m == "unidades" else f"{(10 + j) * 1.5:.2f}"
                )
            rows.append(row)
    df = pl.DataFrame(rows, schema={c: pl.String for c in cols})
    meta = {
        "formato": "B-largo",
        "instrucoes": "Se houver duas medidas, uma linha por entidade/medida."
        "O formato padrão 'Mês k' também é aceito.",
        "periodos": periods,
    }
    if file_format == "xlsx":
        return _template_xlsx(config, df, "Template largo (Formato B)", periods)
    lines = [";".join(cols)] + [";".join(str(r[c]) for c in cols) for r in rows]
    return ExportArtifact(
        f"template_{config.name}.csv",
        "text/csv",
        "\n".join(lines).encode("utf-8-sig"),
        len(rows),
        meta,
    )
def _template_xlsx(
    config: StudyConfig, df: pl.DataFrame, title: str, periods: list[str]
) -> ExportArtifact:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Template"
    header = list(df.columns)
    ws.append(header)
    for row in df.rows():
        ws.append([str(v) for v in row])
    ws_inst = wb.create_sheet("Instruções")
    ws_inst.append([title])
    ws_inst.append(
        [
            "Frequência "
            f"{config.source_frequency.value}. Períodos exemplo: " + " ".join(periods)
        ]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return ExportArtifact(
        f"template_{config.name}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        buf.getvalue(),
        df.height + 1,
        {
            "formato": "B-largo" if config.layout == Layout.WIDE else "A-longo",
            "aba_instrucoes": "Instruções",
        },
    )
