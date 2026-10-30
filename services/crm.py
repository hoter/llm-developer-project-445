"""MCP CRM: карточка абонента и тарифы (готовый read-сервер стенда).

Ключевой приём сервера: субъект операции читается из проверенного токена
вызова (get_access_token() → subject/claims), а не из аргументов.
Идентификатора пользователя нет в контракте инструментов — подменить
нечего, поле отсутствует: структурная защита от IDOR.

Инструмент оператора (get_subscriber_overview) — единственное место, где
чужой идентификатор легитимен: он доступен только роли operator, и это
проверяет сервер по role из claims.
"""
from __future__ import annotations

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError

from core.config import RESOURCES
from core.server_auth import make_mcp_server
from services import store

FORBIDDEN_CODE = -32003  # прикладной код стенда: роль не подходит

crm = make_mcp_server(
    "crm",
    resource=RESOURCES["crm"],
    instructions="Карточка абонента и тарифы. Субъект определяется токеном вызова.",
)


def _subject() -> tuple[str, dict]:
    at = get_access_token()
    assert at is not None, "инструмент вызван без аутентифицированного токена"
    return at.subject, at.claims


def _require_role(*roles: str) -> tuple[str, dict]:
    sub, claims = _subject()
    if claims.get("role") not in roles:
        raise MCPError(
            code=FORBIDDEN_CODE,
            message="forbidden: role required",
            data={"error": "forbidden", "required_roles": list(roles), "sub": sub},
        )
    return sub, claims


@crm.tool()
def get_my_profile() -> dict:
    """Имя, телефон, почта, тариф и подключённые услуги вызывающего абонента (по токену вызова)."""
    sub, _ = _subject()
    record = store.subscriber(sub)
    if record is None:
        return {"error": "not_found"}
    return {
        "subscriber_id": sub,
        "name": record["name"],
        "phone": record["phone"],
        "email": record["email"],
        "tariff": record["tariff"],
        "balance_rub": float(record["balance_rub"]),
        "services": store.subscriber_services(sub),
    }


@crm.tool()
def get_my_tariff() -> dict:
    """Текущий тариф вызывающего абонента: название, цена, что входит."""
    sub, _ = _subject()
    record = store.subscriber(sub)
    if record is None:
        return {"error": "not_found"}
    tariff = store.tariff(record["tariff"]) or {}
    return {
        "subscriber_id": sub,
        "tariff": record["tariff"],
        "price_rub": tariff.get("price_rub"),
        "data_gb": tariff.get("data_gb"),
        "includes": tariff.get("description"),
    }


@crm.tool()
def list_tariffs() -> dict:
    """Каталог тарифов, доступных для подключения (публичные данные)."""
    return {"tariffs": [t for t in store.tariffs() if t["available"]]}


@crm.tool()
def get_subscriber_overview(subscriber_id: str) -> dict:
    """Обзор абонента для оператора поддержки (role=operator).

    Единственный инструмент с чужим идентификатором в аргументах:
    доступ проверяет сервер по роли из claims токена.
    """
    _sub, _claims = _require_role("operator")
    record = store.subscriber(subscriber_id)
    if record is None:
        return {"error": "not_found"}
    return {"subscriber_id": subscriber_id, "name": record["name"], "tariff": record["tariff"]}
