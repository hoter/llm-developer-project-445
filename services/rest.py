"""REST-интерфейс моков бизнес-систем (готовое).

Единственный вход для изменений данных абонента: action-MCP-инструменты
(их пишет студент) вызывают эти endpoints, страница оператора читает
тикет-очередь. Аутентификации нет — это внутренний сегмент сети стенда;
границей доверия в контуре служит action-MCP с проверкой JWT.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services import store


def _sub_of(request: Request) -> str:
    return request.query_params.get("sub", "")


async def crm_subscriber(request: Request) -> JSONResponse:
    """GET /mock/crm/subscriber?sub=... — профиль и подключённые услуги."""
    sub = _sub_of(request)
    record = store.subscriber(sub)
    if record is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({
        "sub": record["sub"],
        "name": record["name"],
        "phone": record["phone"],
        "email": record["email"],
        "tariff": record["tariff"],
        "balance_rub": float(record["balance_rub"]),
        "services": store.subscriber_services(sub),
    })


async def crm_tariffs(request: Request) -> JSONResponse:
    """GET /mock/crm/tariffs — каталог тарифов."""
    return JSONResponse({"tariffs": store.tariffs()})


async def crm_contacts(request: Request) -> JSONResponse:
    """POST /mock/crm/contacts {"sub", "phone"?, "email"?} — обновить контакты."""
    body = await request.json()
    result = store.set_contacts(
        body.get("sub", ""), phone=body.get("phone"), email=body.get("email")
    )
    return JSONResponse(result)


async def crm_toggle_service(request: Request) -> JSONResponse:
    """POST /mock/crm/toggle-service {"sub", "service", "enabled"} — услуга вкл/выкл."""
    body = await request.json()
    result = store.toggle_service(
        body.get("sub", ""), body.get("service", ""), bool(body.get("enabled", True))
    )
    return JSONResponse(result)


async def billing_charges(request: Request) -> JSONResponse:
    """GET /mock/billing/charges?sub=&limit= — последние списания."""
    limit = min(int(request.query_params.get("limit", "6")), 24)
    return JSONResponse({"charges": store.charges(_sub_of(request), limit)})


async def billing_tariff_change(request: Request) -> JSONResponse:
    """POST /mock/billing/tariff-change {"sub", "tariff", "idempotency_key"}.

    Идемпотентность и дневной лимит — на стороне бизнес-системы: даже
    скомпрометированный вызывающий не обойдёт их оркестратором.
    """
    body = await request.json()
    result = store.apply_tariff_change(
        sub=body.get("sub", ""),
        new_tariff=body.get("tariff", ""),
        idempotency_key=body.get("idempotency_key", ""),
    )
    return JSONResponse(result)


async def tickets_list(request: Request) -> JSONResponse:
    """GET /mock/tickets?sub= — очередь обращений (страница оператора)."""
    return JSONResponse({"tickets": store.tickets(_sub_of(request) or None)})


async def tickets_create(request: Request) -> JSONResponse:
    """POST /mock/tickets {"sub", "subject", "note"} — создать обращение."""
    body = await request.json()
    result = store.create_ticket(
        body.get("sub", ""), body.get("subject", ""), body.get("note", "")
    )
    return JSONResponse(result)


async def admin_reset(request: Request) -> JSONResponse:
    """POST /mock/admin/reset — вернуть демо-данные в исходное состояние."""
    from core import seed as seed_mod

    stats = seed_mod.seed()
    return JSONResponse({"ok": True, **stats})


async def admin_billing_quota(request: Request) -> JSONResponse:
    """POST /mock/admin/billing-quota {"sub", "remaining"} — остаток дневного лимита."""
    body = await request.json()
    return JSONResponse(store.set_daily_quota(body.get("sub", ""), int(body.get("remaining", 0))))


async def admin_counts(request: Request) -> JSONResponse:
    """GET /mock/admin/counts — размеры таблиц (smoke стенда)."""
    return JSONResponse(store.counts())


def mock_routes() -> list[Route]:
    return [
        Route("/mock/crm/subscriber", crm_subscriber, methods=["GET"]),
        Route("/mock/crm/tariffs", crm_tariffs, methods=["GET"]),
        Route("/mock/crm/contacts", crm_contacts, methods=["POST"]),
        Route("/mock/crm/toggle-service", crm_toggle_service, methods=["POST"]),
        Route("/mock/billing/charges", billing_charges, methods=["GET"]),
        Route("/mock/billing/tariff-change", billing_tariff_change, methods=["POST"]),
        Route("/mock/tickets", tickets_list, methods=["GET"]),
        Route("/mock/tickets/create", tickets_create, methods=["POST"]),
        Route("/mock/admin/reset", admin_reset, methods=["POST"]),
        Route("/mock/admin/billing-quota", admin_billing_quota, methods=["POST"]),
        Route("/mock/admin/counts", admin_counts, methods=["GET"]),
    ]
