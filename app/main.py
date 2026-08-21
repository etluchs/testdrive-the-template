"""FastAPI entrypoint.

Routes are deliberately thin: read input, call ``app.logic``, render a
template. No integration code, no data shaping, no hand-written HTML.
"""

from __future__ import annotations

from pathlib import Path

from appkit import auth
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import logic

BASE_DIR = Path(__file__).parent

app = FastAPI(title="UZH Requests")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

APP_NAME = "Requests"
NAV_ITEMS = [{"href": "/", "label": "Requests"}]


def context(request: Request, **extra) -> dict:
    """Common template context: signed-in user and page chrome."""
    return {
        "user": auth.user(request),
        "app_name": APP_NAME,
        "nav_items": NAV_ITEMS,
        "current_path": request.url.path,
        **extra,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    rows = logic.list_requests()
    return templates.TemplateResponse(
        request,
        "index.html",
        context(
            request,
            title="Requests",
            columns=logic.COLUMNS,
            rows=rows,
            summary=logic.summarize(rows),
            list_name=logic.REQUESTS_LIST,
        ),
    )


@app.post("/mail-summary", response_class=HTMLResponse)
def mail_summary(request: Request, recipient: str = Form(...)) -> HTMLResponse:
    try:
        summary = logic.send_summary(recipient)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "_summary.html",
            context(request, error=str(exc), recipient=recipient),
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "_summary.html",
        context(request, summary=summary, recipient=recipient),
    )


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
