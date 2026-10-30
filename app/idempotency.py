"""Идемпотентность исполнения на стороне оркестратора.

Ключ в оркестраторе — f"{action}:{pending_id}": одно «да» — одна операция.
Для смены тарифа дедупликация есть и на стороне биллинга — два барьера;
для услуг и контактов этот слой единственный.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core import db


@dataclass
class StoredResult:
    duplicate: bool
    result: dict


async def execute(key: str, sub: str, action: str,
                  fn: Callable[[], Awaitable[dict]]) -> StoredResult:
    """Исполнить операцию один раз на ключ.

    Ключ с сохранённым результатом → повтор не исполняет операцию
    (duplicate=True, сохранённый результат). Неудачный исход (error или
    outcome кроме executed/duplicate) ключ не занимает — сбой не должен
    запирать повторную попытку. Успех → INSERT ... ON CONFLICT DO NOTHING
    (гонка двух «да»: второй получит сохранённый результат).
    """
    key = str(key).strip()
    if not key:
        raise ValueError("ключ идемпотентности пустой")

    stored = db.fetchone("select sub, action, result from idempotency where key = %s", (key,))
    if stored is not None and stored["sub"] == sub and stored["action"] == action:
        return StoredResult(duplicate=True, result=dict(stored["result"] or {}))
    # чужой ключ (например, «smoke:…» из проверки) — перезаписываем своим

    result = await fn()

    if not _is_success(result):
        return StoredResult(duplicate=False, result=result)

    payload = json.dumps(result, ensure_ascii=False, default=str)
    db.execute(
        "insert into idempotency(key, sub, action, result) values (%s, %s, %s, %s::jsonb)"
        " on conflict (key) do update set sub = excluded.sub, action = excluded.action,"
        " result = excluded.result"
        " where idempotency.sub <> excluded.sub or idempotency.action <> excluded.action",
        (key, sub, action, payload),
    )
    # гонка: кто-то успел записать раньше — отдаём его результат
    winner = db.fetchone("select sub, action, result from idempotency where key = %s", (key,))
    if winner is not None and (winner["sub"], winner["action"]) != (sub, action):
        return StoredResult(duplicate=True, result=dict(winner["result"] or {}))
    return StoredResult(duplicate=False, result=result)


def _is_success(result: dict) -> bool:
    """Успех занимает ключ: executed и duplicate (эффект уже был)."""
    if not isinstance(result, dict) or result.get("error"):
        return False
    return result.get("outcome") in (None, "executed", "duplicate", "updated", "toggled", "ticket")
