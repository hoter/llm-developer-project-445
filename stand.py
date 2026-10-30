"""Стенд готового: все сервисы, которые студент НЕ пишет, одним процессом.

    python stand.py            # http://127.0.0.1:8100

Что внутри:

  /token, /jwks.json            demo-issuer (core/issuer.py)
  /crm/mcp /billing/mcp /kb/mcp read-MCP-серверы (services/crm|billing|kb.py)
  /mock/...                     REST-моки CRM/Billing/Tickets (services/rest.py)
  /                             чат-фронтенд (web/index.html) + страница оператора

Каждый read-MCP-сервер проверяет Bearer сам: 401 с WWW-Authenticate и
resource_metadata ставит middleware SDK по настройкам server_auth.

Перед запуском: make infra && make migrate && make seed.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from core import config
from core.issuer import issuer_routes
from services import billing, crm, kb, rest

WEB_DIR = Path(__file__).resolve().parent / "web"


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "stand"})


@asynccontextmanager
async def lifespan(app):
    """Session managers обязаны жить в event loop сервера (uvicorn)."""
    managers = [s.session_manager.run() for s in (crm.crm, billing.billing, kb.kb)]
    for m in managers:
        await m.__aenter__()
    try:
        yield
    finally:
        for m in managers:
            await m.__aexit__(None, None, None)


def build_app() -> Starlette:
    routes = [
        Route("/health", health, methods=["GET"]),
        *issuer_routes(),
        *rest.mock_routes(),
        Mount("/crm", app=crm.crm.streamable_http_app()),
        Mount("/billing", app=billing.billing.streamable_http_app()),
        Mount("/kb", app=kb.kb.streamable_http_app()),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


if __name__ == "__main__":
    uvicorn.run(
        build_app(),
        host="127.0.0.1",
        port=config.STAND_PORT,
        log_level="warning",
    )
