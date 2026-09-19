"""api.main — app FastAPI (S2.1): expõe o motor via HTTP e serve a tela única.

`python -m api` sobe o servidor local e abre o navegador; sem deploy remoto.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .datasets import router as datasets_router
from .exports import router as exports_router
from .models import router as models_router
from .results import router as results_router
from .runs import router as runs_router
from .studies import router as studies_router

app = FastAPI(title="Forecasting API")
app.include_router(datasets_router)
app.include_router(runs_router)
app.include_router(results_router)
app.include_router(exports_router)
app.include_router(studies_router)
app.include_router(models_router)

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if _WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")


def _open_browser(url: str) -> None:
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()


def main() -> None:
    import uvicorn

    url = "http://127.0.0.1:8765"
    _open_browser(url)
    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
