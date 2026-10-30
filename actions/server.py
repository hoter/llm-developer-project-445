"""Action-MCP: инструменты действий.

    uvicorn actions.server:app --host 127.0.0.1 --port 8220

Модели видны prepare_tariff_change, prepare_toggle_service,
prepare_contacts_update и create_ticket; execute_action модель не видит
никогда — его вызывает код оркестратора после явного «да».

Ошибка ActionError → MCPError(code=-32002, message=code, data=hints):
подсказки (available, example, …) доезжают до модели, она исправляет
вызов или пересказывает отказ по бизнес-правилу.

Субъект действия — аргумент sub (до прав его подставляет обёртка
оркестратора, модель его не видит) или субъект Bearer-токена вызова,
если sub не передан.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.routing import Mount

from actions import service
from actions.contracts import ActionError

ACTION_ERROR_CODE = -32002
SERVER_INSTRUCTIONS = """\
Действия мобильного оператора. prepare_* ничего не исполняют: они
проверяют условия и возвращают карточку подтверждения. Исполнение
происходит только после явного «да» абонента, его запускает система.
"""

mcp = MCPServer("actions", instructions=SERVER_INSTRUCTIONS)


async def _guard(coro) -> Any:
    """Дождаться действия; ActionError → MCPError с подсказками в data."""
    try:
        return await coro
    except ActionError as e:
        raise MCPError(code=ACTION_ERROR_CODE, message=e.code, data=e.to_tool_result() or None) from e


async def _subject(sub: str, ctx: Context | None = None) -> str:
    """Субъект действия: аргумент или субъект Bearer-токена вызова.

    Вызов без sub не получает чужую идентичность: без токена субъект —
    голый идентификатор «anonymous», которого нет среди абонентов.
    Сервер пока без auth (шаг прав): подпись проверяет верификатор стенда;
    токен чужого адресата (crm) здесь тоже даёт субъект — эта дыра и
    закрывается проверкой aud на шаге прав.
    """
    if sub:
        return sub
    from core.server_auth import JwtVerifier

    request = getattr(getattr(ctx, "request_context", None), "request", None) if ctx else None
    header = request.headers.get("authorization", "") if request is not None else ""
    if not header.lower().startswith("bearer "):
        return "anonymous"
    try:
        from core.config import RESOURCES

        token = await JwtVerifier(RESOURCES["actions"]).verify_token(header[7:])
    except Exception:  # noqa: BLE001 — кривой токен или aud не тот: субъекта нет
        return "anonymous"
    return token.subject if token is not None and token.subject else "anonymous"


@mcp.tool(structured_output=False)
async def prepare_tariff_change(tariff: str, sub: str = "", ctx: Context | None = None) -> Any:
    """Подготовить смену тарифа: проверяет текущий тариф и каталог, возвращает карточку.

    Ничего не исполняет. Ошибки: unknown_tariff (с available), same_tariff,
    tariff_not_available.
    """
    return await _guard(service.prepare_tariff_change(await _subject(sub, ctx), tariff))


@mcp.tool(structured_output=False)
async def prepare_toggle_service(service_name: str, enabled: bool, sub: str = "",
                                 ctx: Context | None = None) -> Any:
    """Подготовить включение или отключение услуги оператора.

    service_name — точное название из каталога: «Музыка», «Роуминг»,
    «Антивирус». enabled: true — включить, false — отключить.
    Ничего не исполняет. Ошибки: unknown_service (с available),
    service_already_on, service_already_off.
    """
    return await _guard(service.prepare_toggle_service(await _subject(sub, ctx),
                                                      service_name, enabled))


@mcp.tool(structured_output=False)
async def prepare_contacts_update(phone: str = "", email: str = "", sub: str = "",
                                  ctx: Context | None = None) -> Any:
    """Подготовить смену контактов: телефон и/или почта, нормализуются.

    Ничего не исполняет. Ошибки: bad_phone, bad_email (с example),
    same_contacts, nothing_to_update.
    """
    return await _guard(service.prepare_contacts_update(
        await _subject(sub, ctx), phone or None, email or None))


@mcp.tool(structured_output=False)
async def create_ticket(subject: str, note: str, sub: str = "",
                        ctx: Context | None = None) -> Any:
    """Создать обращение в поддержку — эскалация без карточки подтверждения."""
    return await _guard(service.create_ticket(await _subject(sub, ctx), subject, note))


@mcp.tool(structured_output=False)
async def execute_action(action: str, details_json: str, pending_id: str = "",
                         idempotency_key: str = "", sub: str = "",
                         ctx: Context | None = None) -> Any:
    """ИСПОЛНИТЬ подготовленное действие. Только для кода оркестратора после «да»."""
    return await _guard(service.execute_action(
        await _subject(sub, ctx), action, details_json,
        pending_id=pending_id, idempotency_key=idempotency_key,
    ))


@asynccontextmanager
async def lifespan(app):
    """Session manager MCP живёт в event loop uvicorn (как на стенде)."""
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[Mount("/actions", app=mcp.streamable_http_app())],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8220, log_level="warning")
