"""Логика действий поверх REST-моков: предусловия, карточки, исполнение.

prepare_* ничего не исполняют: проверяют предусловия по текущему
состоянию абонента и собирают карточку «с какого на какой, что изменится».
Исполнение — execute_action, его вызывает только код оркестратора после
явного «да». Субъект sub здесь — уже проверенный сервером субъект токена:
его передаёт actions/server.py, а не клиент и не модель.
"""
from __future__ import annotations

import json

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from actions.contracts import (
    ActionError,
    normalize_email,
    normalize_phone,
    normalize_service,
    normalize_tariff,
)
from core import config

SERVICE_CATALOG = ["Музыка", "Роуминг", "Антивирус"]

CARD_ACTIONS = ("tariff_change", "service_toggle", "contacts_update")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.STAND_URL, trust_env=False, timeout=10)


async def _get(path: str, **params) -> dict:
    async with _client() as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict) -> dict:
    async with _client() as c:
        r = await c.post(path, json=body)
        r.raise_for_status()
        return r.json()


FORBIDDEN_CODE = -32003  # прикладной код контура: роль не подходит


def _require_operator() -> None:
    """Роль из проверенного токена: чужую карточку читает только оператор."""
    from mcp.server.auth.middleware.auth_context import get_access_token
    from mcp.shared.exceptions import MCPError

    token = get_access_token()
    role = (token.claims or {}).get("role") if token is not None else None
    if role != "operator":
        raise MCPError(code=FORBIDDEN_CODE, message="forbidden: role required",
                       data={"error": "forbidden", "required_roles": ["operator"]})


async def _subscriber(sub: str) -> dict:
    try:
        record = await _get("/mock/crm/subscriber", sub=sub)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ActionError("not_found", "абонент не найден", sub=sub) from e
        raise
    if record.get("error"):
        raise ActionError("not_found", "абонент не найден", sub=sub)
    return record


async def _tariffs() -> list[dict]:
    return (await _get("/mock/crm/tariffs"))["tariffs"]


def _rub(value) -> int:
    return int(round(float(value)))


def _diff(before: int, after: int) -> str:
    delta = after - before
    return f"+{delta} ₽" if delta >= 0 else f"{delta} ₽"


def _price_of(catalog: list[dict], name: str) -> float:
    """Цена текущего тарифа; архивный может отсутствовать в доступных — ищем по всем."""
    row = next((r for r in catalog if r["name"] == name), None)
    return float(row["price_rub"]) if row else 0.0


# ── Подготовка: карточка без эффекта ─────────────────────────────────────

async def prepare_tariff_change(sub: str, tariff: str) -> dict:
    """Смена тарифа: проверяет текущий тариф, каталог и архивность."""
    record = await _subscriber(sub)
    catalog = await _tariffs()
    target = normalize_tariff(tariff, catalog)
    current = record["tariff"]
    if current == target:
        raise ActionError(
            "same_tariff",
            f"тариф «{target}» уже подключён",
            current=current,
            available=[r["name"] for r in catalog if r["available"] and r["name"] != current],
        )

    price_before = _rub(_price_of(catalog, current))
    row = next(r for r in catalog if r["name"] == target)
    price_after = _rub(row["price_rub"])
    view = {
        "что": "смена тарифа",
        "с": current,
        "на": target,
        "цена": f"{price_before} ₽ → {price_after} ₽/мес ({_diff(price_before, price_after)})",
        "когда": "с 1-го числа следующего расчётного периода",
    }
    summary = (
        f"Тариф: {current} → {target}. Цена: {price_before} ₽ → {price_after} ₽/мес "
        f"({_diff(price_before, price_after)}). Новый тариф действует с "
        "1-го числа следующего расчётного периода."
    )
    return {
        "ok": True,
        "action": "tariff_change",
        "details": {"tariff": target},
        "view": view,
        "summary": summary,
    }


async def prepare_toggle_service(sub: str, service: str, enabled: bool) -> dict:
    """Включение/выключение услуги: уже включена / уже выключена — отказ."""
    record = await _subscriber(sub)
    name = normalize_service(service, SERVICE_CATALOG)
    row = next((s for s in record.get("services", []) if s["service"] == name), None)
    current = bool(row["enabled"]) if row else False
    if current == bool(enabled):
        raise ActionError(
            "service_already_on" if enabled else "service_already_off",
            f"услуга «{name}» уже {'включена' if enabled else 'выключена'}",
            service=name,
            enabled=current,
        )
    price = _rub(row["price_rub"]) if row else 0
    verb = "Включение" if enabled else "Отключение"
    after = f"+{price} ₽/мес" if enabled else "списания прекратятся со следующего дня"
    view = {
        "что": f"{'подключение' if enabled else 'отключение'} услуги",
        "услуга": name,
        "было": "включена" if current else "выключена",
        "станет": "включена" if enabled else "выключена",
        "цена": after,
    }
    return {
        "ok": True,
        "action": "service_toggle",
        "details": {"service": name, "enabled": bool(enabled)},
        "view": view,
        "summary": f"{verb} услуги «{name}»: {after}.",
    }


async def prepare_contacts_update(sub: str, phone: str | None = None,
                                  email: str | None = None) -> dict:
    """Смена контактов: нормализация, новые значения, ничего не менять — отказ."""
    if not phone and not email:
        raise ActionError(
            "nothing_to_update",
            "не указано, что менять: нужен телефон и/или почта",
        )
    record = await _subscriber(sub)
    new_phone = normalize_phone(phone) if phone else None
    new_email = normalize_email(email) if email else None
    same = (new_phone in (None, record["phone"])) and (new_email in (None, record["email"]))
    if same:
        raise ActionError(
            "same_contacts",
            "указанные контакты уже стоят в карточке",
            phone=record["phone"],
            email=record["email"],
        )

    view: dict = {"что": "смена контактов"}
    changes = []
    if new_phone and new_phone != record["phone"]:
        view["телефон"] = f"{record['phone']} → {new_phone}"
        changes.append(f"телефон {record['phone']} → {new_phone}")
    if new_email and new_email != record["email"]:
        view["почта"] = f"{record['email']} → {new_email}"
        changes.append(f"почта {record['email']} → {new_email}")
    details = {k: v for k, v in (("phone", new_phone), ("email", new_email)) if v}
    return {
        "ok": True,
        "action": "contacts_update",
        "details": details,
        "view": view,
        "summary": "Меняются контакты: " + "; ".join(changes) + ".",
    }


# ── Исполнение: только по решению оркестратора ───────────────────────────

RESULT_MESSAGES = {
    "executed": "Готово",
    "duplicate": "уже исполнена",
    "daily_limit": "дневной лимит смен тарифа исчерпан",
    "same_tariff": "тариф уже подключён",
    "tariff_not_available": "тариф недоступен",
    "not_found": "абонент не найден",
    "unknown_service": "услуга не найдена",
}


def _humanize(result: dict) -> str:
    """Короткое сообщение о результате — его дочитывает оркестратор."""
    outcome = result.get("outcome", "error")
    if outcome == "executed":
        if "after" in result:
            return f"Готово: тариф {result['before']} → {result['after']}."
        return "Готово: контакты обновлены."
    if outcome == "toggled":
        state = "включена" if result.get("enabled") else "выключена"
        return f"Готово: услуга «{result.get('service')}» {state}."
    if outcome == "updated":
        return "Готово: контакты обновлены."
    if outcome == "duplicate":
        return "Операция уже исполнена, повторно не выполняется."
    if outcome == "daily_limit":
        return f"Готово не выполнено: дневной лимит смен тарифа ({result.get('limit')}) исчерпан."
    return RESULT_MESSAGES.get(outcome, f"действие отклонено: {outcome}")


async def execute_action(sub: str, action: str, details_json: str | dict,
                         pending_id: str = "", idempotency_key: str = "") -> dict:
    """Исполнение подготовленного действия: executed / duplicate / отказ.

    tariff_change → биллинг (идемпотентность и дневной лимит держит он),
    service_toggle и contacts_update → CRM. Ключ передаётся в биллинг,
    чтобы второй барьер работал и там.
    """
    details = details_json if isinstance(details_json, dict) else _parse_details(details_json)
    if action == "tariff_change":
        record = await _subscriber(sub)
        catalog = await _tariffs()
        target = normalize_tariff(str(details.get("tariff", "")), catalog)
        result = await _post("/mock/billing/tariff-change", {
            "sub": sub, "tariff": target, "idempotency_key": idempotency_key,
        })
        result.setdefault("before", record["tariff"])
    elif action == "service_toggle":
        name = normalize_service(str(details.get("service", "")), SERVICE_CATALOG)
        result = await _post("/mock/crm/toggle-service", {
            "sub": sub, "service": name, "enabled": bool(details.get("enabled")),
        })
    elif action == "contacts_update":
        result = await _post("/mock/crm/contacts", {"sub": sub, **details})
    else:
        raise ActionError("unknown_action", f"неизвестное действие «{action}»",
                          available=list(CARD_ACTIONS))

    return {
        **result,
        "action": action,
        "pending_id": pending_id,
        "idempotency_key": idempotency_key,
        "message": _humanize(result),
    }


def _parse_details(details_json: str) -> dict:
    try:
        value = json.loads(details_json or "{}")
    except (TypeError, ValueError) as e:
        raise ActionError("bad_details", "details_json — не JSON") from e
    if not isinstance(value, dict):
        raise ActionError("bad_details", "details_json должен быть JSON-объектом")
    return value


async def subscriber_overview(sub: str, subscriber_id: str) -> dict:
    """Обзор абонента по id (оператор): обзор из CRM плюс контакты и услуги из профиля.

    Роль проверяет сервер по claims токена: чужой идентификатор легитимен
    только оператору.
    """
    _require_operator()
    try:
        rec = await _get("/mock/crm/subscriber", sub=subscriber_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ActionError("not_found", "абонент не найден", sub=subscriber_id) from e
        raise
    return {
        "subscriber_id": rec["sub"],
        "name": rec["name"],
        "phone": rec["phone"],
        "email": rec.get("email"),
        "tariff": rec["tariff"],
        "balance_rub": rec.get("balance_rub"),
        "services": [dict(row) for row in rec.get("services", [])],
    }


async def create_ticket(sub: str, subject: str, note: str) -> dict:
    """Эскалация: обращение в поддержку без карточки (риск низкий)."""
    subject = str(subject or "").strip()
    note = str(note or "").strip()
    if not subject:
        raise ActionError("empty_ticket", "у обращения должна быть тема")
    await _subscriber(sub)
    result = await _post("/mock/tickets/create", {
        "sub": sub, "subject": subject[:120], "note": note or subject,
    })
    return {**result, "message": f"Обращение создано: {result['ticket_id']}."}
