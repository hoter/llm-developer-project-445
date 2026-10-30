"""Аудит: append-only журнал в Postgres — основа разбора инцидентов.

Модуль умеет только дописывать и читать: обновления и удаления нет.
Виды исходов зафиксированы в OUTCOMES; неизвестный исход пишется как
error с пометкой, а не роняет ход.

Трейс хода лежит в трёх таблицах, связанных trace_id (и session_id для
диалога):
  audit_log  — исходы контура: действия (before/after), отказы, эскалации;
  llm_calls  — запуски агентов: имя, модель, токены, задержка;
  tool_calls — вызовы инструментов моделью: args, outcome, задержка.
Разбор инцидента — запрос по trace_id в chronological порядке (trace()).

В журнал попадают и неуспешные попытки: по нему видно «кто пытался и что».
Не логируйте текст реплик целиком — журнал про действия, не про переписку.
"""
from __future__ import annotations

import json

from core import db

OUTCOMES = frozenset({
    "executed", "duplicate_suppressed", "rejected_by_user", "rejected_by_contract",
    "expired", "escalated", "denied", "guardrail_unsafe", "guardrail_degraded",
    "token_refreshed", "server_limit", "budget_exhausted", "error",
    # подготовка карточки: действие ещё не исполнено, ждём «да»
    "awaiting_confirmation",
})

# сколько знаков аргументов инструмента хранить в tool_calls
ARGS_LIMIT = 500


def as_jsonb(value):
    """Значение для jsonb-колонки; строки, которые уже JSON, идут как есть."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "{[" or stripped[:1] == '"':
            try:
                json.loads(stripped)
                return value
            except ValueError:
                pass
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, default=str)


def _text(value):
    """Значение jsonb → строка для чтения (before/after в журнале)."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def append(who: str, what: str, outcome: str, *, trace_id: str | None = None,
           session_id: str | None = None, args=None, before=None, after=None,
           idempotency_key: str | None = None, detail: str | None = None) -> None:
    """Дописать запись в журнал. Журнал не редактируется — только append."""
    if outcome not in OUTCOMES:
        detail = f"{detail or ''} (code {outcome})".strip()
        outcome = "error"
    try:
        db.execute(
            "insert into audit_log(trace_id, session_id, who, what, outcome, args, before, after,"
            " idempotency_key, detail)"
            " values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)",
            (
                trace_id, session_id, who, what, outcome,
                as_jsonb(args), as_jsonb(before), as_jsonb(after), idempotency_key,
                None if detail is None else str(detail)[:1000],
            ),
        )
    except Exception as e:  # noqa: BLE001 — журнал не должен ронять ход
        print(f"audit: запись {what}/{outcome} не сохранена: {e}", flush=True)


def tail(n: int = 20, who: str | None = None) -> list[dict]:
    """Последние записи (для разбора инцидентов), новые первыми."""
    if who:
        rows = db.fetchall(
            "select * from audit_log where who = %s order by id desc limit %s", (who, n),
        )
        return [_plain(row) for row in rows]
    rows = db.fetchall("select * from audit_log order by id desc limit %s", (n,))
    return [_plain(row) for row in rows]


def trace(trace_id: str) -> list[dict]:
    """Все записи одного хода по сквозному идентификатору."""
    return [_plain(row) for row in db.fetchall(
        "select * from audit_log where trace_id = %s order by id", (trace_id,),
    )]


def timeline(trace_id: str) -> list[dict]:
    """Хронология хода из трёх журналов: audit_log, llm_calls, tool_calls.

    Записи LLM и инструментов хранят id в пределах своей таблицы, поэтому
    порядок между таблицами — по времени (ts, затем порядок вставки).
    """
    events: list[dict] = []
    for row in trace(trace_id):
        events.append({
            "at": row["ts"], "kind": "audit", "what": row.get("what"),
            "outcome": row.get("outcome"), "who": row.get("who"),
            "before": row.get("before"), "after": row.get("after"),
            "idempotency_key": row.get("idempotency_key"), "detail": row.get("detail"),
        })
    for row in db.fetchall("select * from llm_calls where trace_id = %s", (trace_id,)):
        events.append({
            "at": row["ts"], "kind": "llm", "what": row.get("agent"),
            "model": row.get("model"),
            "tokens": {"input": row.get("input_tokens"), "output": row.get("output_tokens")},
            "latency_ms": row.get("latency_ms"),
        })
    for row in db.fetchall("select * from tool_calls where trace_id = %s", (trace_id,)):
        events.append({
            "at": row["ts"], "kind": "tool", "what": row.get("tool"),
            "args": row.get("args"), "outcome": row.get("outcome"),
            "latency_ms": row.get("latency_ms"),
        })
    events.sort(key=lambda e: (e["at"], _kind_order(e["kind"])))
    return events


def _kind_order(kind: str) -> int:
    return {"tool": 0, "llm": 1, "audit": 2}.get(kind, 3)


def _plain(row: dict) -> dict:
    """Строка журнала как есть: before/after — строки, как их писал вызывающий."""
    row = dict(row)
    row["before"] = _text(row.get("before"))
    row["after"] = _text(row.get("after"))
    return row
