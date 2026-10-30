"""MCP Billing: списания абонента (готовый read-сервер стенда).

Исполнение операций биллинга здесь не выставляется: write-путь контура
идёт через action-MCP (его пишет студент) поверх REST-моков, где стоят
идемпотентность и дневной лимит.
"""
from __future__ import annotations

from mcp.server.auth.middleware.auth_context import get_access_token

from core.config import RESOURCES
from core.server_auth import make_mcp_server
from services import store

billing = make_mcp_server(
    "billing",
    resource=RESOURCES["billing"],
    instructions="Списания абонента. Субъект определяется токеном вызова.",
)


def _subject() -> str:
    at = get_access_token()
    assert at is not None, "инструмент вызван без аутентифицированного токена"
    return at.subject


@billing.tool()
def list_my_charges(limit: int = 6) -> dict:
    """Последние списания вызывающего абонента: месяц, строка счёта, сумма."""
    sub = _subject()
    months = store.charges(sub, max(1, min(limit, 24)))
    return {"subscriber_id": sub, "charges": months}
