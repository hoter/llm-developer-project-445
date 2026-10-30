"""Обёртки MCP-инструментов для агентов: вызов с JWT + учёт трейса.

Готовая основа: MCP-инструмент превращается в FunctionTool агента;
субъект вызова в Bearer-токене соединения; кривой JSON аргументов —
машинно-читаемый ответ модели, сессия не роняется; ошибка MCP-сервера
(MCPError, код + data с подсказками) — JSON tool-результат для модели.

Контракты: успешный prepare_* кладёт карточку в pending_cards и говорит
модели «спроси подтверждение да/нет». execute_action модель не видит
никогда: его запускает код оркестратора после явного «да».

Права — два барьера:
  wrap_tools: имена фильтруются через permissions.visible_tools (барьер 1:
    запрещённого инструмента модель не видит вовсе);
  invoke: permissions.admit(session.allowlist, name, args) перед вызовом
    (барьер 2: операция вне прав или ключ идентичности в аргументах —
    отказ {"error": "denied", "reason"} и запись в аудит).

Аудит: каждый tool-вызов модели → tool_calls (аргументы обрезаны,
исход, задержка).
"""
from __future__ import annotations

import json
import time

from agents import FunctionTool
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from app import audit, cards, permissions
from app.sessions import Session, pep_of
from core import config, db

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
        started = time.monotonic()
        outcome = "ok"
        try:
            try:
                args = json.loads(input_json) if input_json and input_json.strip() else {}
            except (TypeError, ValueError):
                outcome = "bad_arguments"
                return json.dumps(
                    {"error": "bad_arguments", "detail": "аргументы — не JSON; повтори вызов по схеме"},
                    ensure_ascii=False,
                )
            if not isinstance(args, dict):
                outcome = "bad_arguments"
                return json.dumps({"error": "bad_arguments", "detail": "аргументы — JSON-объект"},
                                  ensure_ascii=False)
            # барьер 2: тройка «субъект, операция, объект» в момент вызова
            decision = permissions.admit(session.allowlist, name, args)
            if not decision.allowed:
                outcome = "denied"
                audit.append(subject_of(session), name, "denied",
                             trace_id=session.trace_id, session_id=session.session_id,
                             args=args, detail=decision.reason)
                return json.dumps({"error": "denied", "reason": decision.reason,
                                   "detail": "эта операция недоступна пользователю"},
                                  ensure_ascii=False)
            session.note(name)
            try:
                result = await session.pool.call(session.pool.resource_of(name), name, args)
            except MCPError as e:
                outcome = getattr(e, "message", "tool_error")[:60] or "tool_error"
                data = e.data if isinstance(e.data, dict) else {}
                return json.dumps(
                    {"error": getattr(e, "message", "tool_failed")[:60], **data},
                    ensure_ascii=False,
                )
            except Exception as e:  # noqa: BLE001 — сбой сервера становится данными для модели
                outcome = "tool_failed"
                return json.dumps({"error": "tool_failed", "detail": str(e)[:160]},
                                  ensure_ascii=False)

            hook = await _card_answer(session, name, result)
            if hook is None:
                pass
            elif hook.get("error"):
                outcome = "prepare_denied"
                return json.dumps(hook, ensure_ascii=False)
            else:
                outcome = "card_created"
            return json.dumps(hook if hook is not None else result, ensure_ascii=False)
        finally:
            log_call(session, name, input_json, outcome, started)

    return FunctionTool(
        name=name,
        description=session.pool.tool_description(name),
        params_json_schema=_public_schema(session.pool.tool_schema(name)),
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


def log_call(session: Session, name: str, raw_args: str, outcome: str,
              started: float) -> None:
    """Вызов инструмента моделью → tool_calls: аргументы обрезаны, без реплик."""
    args = {"raw": (raw_args or "")[:audit.ARGS_LIMIT]}
    try:
        parsed = json.loads(raw_args or "{}")
        if isinstance(parsed, dict):
            parsed.pop("sub", None)
            args = parsed
    except (TypeError, ValueError):
        pass
    # личное не пишем: почта и телефон в аргументах маскируются
    args = {k: ("***" if k in ("email", "phone", "note") else v) for k, v in args.items()}
    latency = int((time.monotonic() - started) * 1000)
    try:
        db.execute(
            "insert into tool_calls(trace_id, tool, args, outcome, latency_ms)"
            " values (%s, %s, %s::jsonb, %s, %s)",
            (session.trace_id, name, audit.as_jsonb(args), outcome, latency),
        )
    except Exception:  # noqa: BLE001 — журнал не должен ронять ход
        pass


def subject_of(session: Session) -> str:
    """Кто вызывает: субъект из claims токена сессии."""
    allowlist = getattr(session, "allowlist", None)
    return allowlist.sub if allowlist is not None else session.user.sub


def _public_schema(schema: dict) -> dict:
    """Схема без ключей идентичности: их не задаёт модель и не видит в ней."""
    schema = dict(schema or {})
    properties = {k: v for k, v in (schema.get("properties") or {}).items()
                  if k not in permissions.IDENTITY_ARGS}
    required = [r for r in schema.get("required") or [] if r not in permissions.IDENTITY_ARGS]
    schema["properties"] = properties
    schema["required"] = required
    return schema


async def _card_answer(session: Session, name: str, result: dict) -> dict | None:
    """Успешный prepare_* → карточка подтверждения, если её разрешил PDP.

    Карточка создаётся с pending_id заранее и проводится через Prepare:
    так событие подготовки в политике несёт идентификатор. Deny — живой
    карточки нет, модель получает отказ с причиной.
    """
    action = PREPARE_ACTIONS.get(name)
    if action is None or not isinstance(result, dict) or result.get("error"):
        return None
    card = cards.create(session, action, result.get("details") or {}, result.get("view") or {})
    decision = await pep_of(session).prepare(card)
    if not decision.allowed:
        cards.settle(session, card, "denied")
        return {"error": "denied", "reason": decision.reason,
                "detail": "подготовка действия запрещена политикой прав"}
    public = card.public_view(config.CARD_TTL_SEC)
    return {
        "card_created": True,
        "pending_id": card.pending_id,
        "card": public,
        "summary": result.get("summary", ""),
        "next": "перескажи карточку и спроси подтверждение «да/нет»",
    }


def wrap_tools(session: Session, names: list[str]) -> list[FunctionTool]:
    """Собрать обёртки только для разрешённых субъекту инструментов.

    Барьер 1 — здесь: запрещённое имя не превращается в инструмент агента,
    поэтому модель его не видит.
    """
    known = set(session.pool.tool_names())
    candidates = [n for n in names if n in known and n not in EXECUTION_ONLY]
    allowed = permissions.visible_tools(session.allowlist, candidates)
    return [wrap_mcp_tool(session, n) for n in allowed]
