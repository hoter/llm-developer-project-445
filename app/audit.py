"""Аудит: append-only журнал в Postgres — основа разбора инцидентов.

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
})


def _json(value):
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
    db.execute(
        "insert into audit_log(trace_id, session_id, who, what, outcome, args, before, after,"
        " idempotency_key, detail)"
        " values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)",
        (
            trace_id, session_id, who, what, outcome,
            _json(args), _json(before), _json(after), idempotency_key,
            None if detail is None else str(detail)[:1000],
        ),
    )


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


def _plain(row: dict) -> dict:
    """Строка журнала как есть: before/after — строки, как их писал вызывающий."""
    row = dict(row)
    row["before"] = _text(row.get("before"))
    row["after"] = _text(row.get("after"))
    return row
