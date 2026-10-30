"""Action-MCP: инструменты действий.

    uvicorn actions.server:app --host 127.0.0.1 --port 8220

Сервер ресурса по JWT (core/server_auth): каждый вызов несёт Bearer-токен,
сервер проверяет подпись по JWKS, издателя, аудиторию и срок и читает
субъекта из проверенного токена. Аргумента sub в контрактах нет —
подменять нечего.

Модели видны prepare_tariff_change, prepare_toggle_service,
prepare_contacts_update и create_ticket; execute_action не отдаётся
модели — его вызывает код оркестратора после явного «да».

Ошибка ActionError → MCPError(code=-32002, message=code, data=hints);
роль не подходит (клиент не читает чужую карточку) → MCPError(-32003).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.routing import Mount

from actions import service
from actions.contracts import ActionError
from core.config import RESOURCES
from core.server_auth import make_mcp_server

ACTION_ERROR_CODE = -32002
FORBIDDEN_CODE = -32003
SERVER_INSTRUCTIONS = """\
Действия мобильного оператора. prepare_* ничего не исполняют: они
проверяют условия и возвращают карточку подтверждения. Исполнение
происходит только после явного «да» абонента, его запускает система.
"""

mcp = make_mcp_server("actions", resource=RESOURCES["actions"],
                      instructions=SERVER_INSTRUCTIONS)


def _subject() -> str:
    """Субъект из проверенного токена вызова (не из аргументов)."""
    token = get_access_token()
    if token is None or not token.subject:
        raise MCPError(code=FORBIDDEN_CODE, message="forbidden: token required",
                       data={"error": "forbidden"})
    return token.subject


async def _guard(coro) -> Any:
    """Дождаться действия; ActionError → MCPError с подсказками в data."""
    try:
        return await coro
    except ActionError as e:
        raise MCPError(code=ACTION_ERROR_CODE, message=e.code, data=e.to_tool_result() or None) from e


@mcp.tool(structured_output=False)
async def prepare_tariff_change(tariff: str) -> Any:
    """Подготовить смену тарифа: проверяет текущий тариф и каталог, возвращает карточку.

    Ничего не исполняет. Ошибки: unknown_tariff (с available), same_tariff,
    tariff_not_available. Абонент определяется токеном вызова.
    """
    return await _guard(service.prepare_tariff_change(_subject(), tariff))


@mcp.tool(structured_output=False)
async def prepare_toggle_service(service_name: str, enabled: bool) -> Any:
    """Подготовить включение или отключение услуги оператора.

    service_name — точное название из каталога: «Музыка», «Роуминг»,
    «Антивирус». enabled: true — включить, false — отключить.
    Ничего не исполняет. Ошибки: unknown_service (с available),
    service_already_on, service_already_off.
    """
    return await _guard(service.prepare_toggle_service(_subject(), service_name, enabled))


@mcp.tool(structured_output=False)
async def prepare_contacts_update(phone: str = "", email: str = "") -> Any:
    """Подготовить смену контактов: телефон и/или почта, нормализуются.

    Ничего не исполняет. Ошибки: bad_phone, bad_email (с example),
    same_contacts, nothing_to_update.
    """
    return await _guard(service.prepare_contacts_update(_subject(), phone or None, email or None))


@mcp.tool(structured_output=False)
async def create_ticket(subject: str, note: str) -> Any:
    """Создать обращение в поддержку — эскалация без карточки подтверждения."""
    return await _guard(service.create_ticket(_subject(), subject, note))


@mcp.tool(structured_output=False)
async def get_subscriber_overview(subscriber_id: str) -> Any:
    """Обзор абонента (только оператор поддержки): имя, тариф, контакты, услуги."""
    return await _guard(service.subscriber_overview(_subject(), subscriber_id))


@mcp.tool(structured_output=False)
async def execute_action(action: str, details_json: str, pending_id: str = "",
                         idempotency_key: str = "") -> Any:
    """ИСПОЛНИТЬ подготовленное действие. Только для кода оркестратора после «да»."""
    return await _guard(service.execute_action(
        _subject(), action, details_json,
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
