"""Обёртки MCP-инструментов для агентов: вызов с JWT + учёт трейса.

Готовая основа: MCP-инструмент превращается в FunctionTool агента;
субъект вызова в Bearer-токене соединения; кривой JSON аргументов —
машинно-читаемый ответ модели, сессия не роняется; ошибка MCP-сервера
(MCPError, код + data с подсказками) — JSON tool-результат для модели.

Контракты: успешный prepare_* кладёт карточку в pending_cards и говорит
модели «спроси подтверждение да/нет». execute_action модель не видит
никогда: его запускает код оркестратора после явного «да».

TODO(аудит): каждый tool-вызов модели → таблицу tool_calls
  (trace_id, tool, args — обрезанные, outcome, latency_ms).

TODO(права): два барьера —
  wrap_tools: фильтровать имена через permissions.visible_tools
  (барьер 1: модель не видит запрещённого);
  invoke: permissions.admit(session.allowlist, name, args) перед вызовом
  (барьер 2: отказ → {"error": "denied", "reason"} + запись в аудит);
  подготовленную карточку проводить через pep.prepare — Deny → карточки
  нет, модель получает отказ с reason.
"""
from __future__ import annotations

import json

from agents import FunctionTool
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from app import cards
from app.sessions import Session
from core import config

# Инструменты исполнения: их вызывает только код после подтверждения.
EXECUTION_ONLY = frozenset({"execute_action"})

# prepare_* → действие карточки
PREPARE_ACTIONS = {
    "prepare_tariff_change": "tariff_change",
    "prepare_toggle_service": "service_toggle",
    "prepare_contacts_update": "contacts_update",
}


def wrap_mcp_tool(session: Session, name: str) -> FunctionTool:
    """Обёртка одного MCP-инструмента для агента этой сессии."""
    if name in EXECUTION_ONLY:
        raise ValueError(f"{name} нельзя отдавать модели: только исполнение кодом")

    async def invoke(ctx: RunContextWrapper, input_json: str) -> str:
        try:
            args = json.loads(input_json) if input_json and input_json.strip() else {}
        except (TypeError, ValueError):
            return json.dumps(
                {"error": "bad_arguments", "detail": "аргументы — не JSON; повтори вызов по схеме"},
                ensure_ascii=False,
            )
        if not isinstance(args, dict):
            return json.dumps({"error": "bad_arguments", "detail": "аргументы — JSON-объект"},
                              ensure_ascii=False)
        # субъект подставляет код оркестратора, модель его не видит в схеме
        args.pop("sub", None)
        if _has_subject(session, name):
            args["sub"] = session.user.sub
        session.note(name)
        try:
            result = await session.pool.call(session.pool.resource_of(name), name, args)
        except MCPError as e:
            data = e.data if isinstance(e.data, dict) else {}
            return json.dumps(
                {"error": getattr(e, "message", "tool_failed")[:60], **data},
                ensure_ascii=False,
            )
        except Exception as e:  # noqa: BLE001 — сбой сервера становится данными для модели
            return json.dumps({"error": "tool_failed", "detail": str(e)[:160]}, ensure_ascii=False)

        hook = _card_answer(session, name, result)
        return json.dumps(hook if hook is not None else result, ensure_ascii=False)

    return FunctionTool(
        name=name,
        description=session.pool.tool_description(name),
        params_json_schema=_public_schema(session.pool.tool_schema(name)),
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


def _has_subject(session: Session, name: str) -> bool:
    """Инструменты action-MCP принимают sub аргументом до шага прав."""
    return session.pool.resource_of(name) == "actions"


def _public_schema(schema: dict) -> dict:
    """Схема без sub: идентичность модели не видна и не задаётся ею."""
    schema = dict(schema or {})
    properties = dict(schema.get("properties") or {})
    properties.pop("sub", None)
    required = [r for r in schema.get("required") or [] if r != "sub"]
    schema["properties"] = properties
    schema["required"] = required
    return schema


def _card_answer(session: Session, name: str, result: dict) -> dict | None:
    """Успешный prepare_* → карточка подтверждения и ответ модели."""
    action = PREPARE_ACTIONS.get(name)
    if action is None or not isinstance(result, dict) or result.get("error"):
        return None
    card = cards.create(session, action, result.get("details") or {}, result.get("view") or {})
    public = card.public_view(config.CARD_TTL_SEC)
    return {
        "card_created": True,
        "pending_id": card.pending_id,
        "card": public,
        "summary": result.get("summary", ""),
        "next": "перескажи карточку и спроси подтверждение «да/нет»",
    }


def wrap_tools(session: Session, names: list[str]) -> list[FunctionTool]:
    """Собрать обёртки для перечня имён (какие имена давать — решает код)."""
    known = set(session.pool.tool_names())
    return [wrap_mcp_tool(session, n) for n in names if n in known and n not in EXECUTION_ONLY]
