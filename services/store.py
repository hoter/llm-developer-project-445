"""Состояние моков бизнес-систем: SQL-слой поверх Postgres.

В проде это CRM, биллинг и тикет-система с собственными хранилищами.
Один и тот же слой обслуживает read-MCP-инструменты и REST-интерфейс
моков; эффект действий сразу виден в read-запросах.
"""
from __future__ import annotations

import yaml

from core import db
from core.config import ROOT

DEMO_FILE = ROOT / "data" / "demo.yaml"

DAILY_TARIFF_CHANGE_LIMIT = 3


def _demo() -> dict:
    return yaml.safe_load(DEMO_FILE.read_text(encoding="utf-8"))


# ── CRM ──────────────────────────────────────────────────────────────────

def subscriber(sub: str) -> dict | None:
    return db.fetchone("SELECT * FROM subscribers WHERE sub = %s", (sub,))


def subscriber_services(sub: str) -> list[dict]:
    return db.fetchall(
        "SELECT service, enabled, price_rub FROM subscriber_services WHERE sub = %s ORDER BY service",
        (sub,),
    )


def tariffs() -> list[dict]:
    return db.fetchall("SELECT * FROM tariffs ORDER BY price_rub")


def tariff(name: str) -> dict | None:
    return db.fetchone("SELECT * FROM tariffs WHERE name = %s", (name,))


def set_contacts(sub: str, *, phone: str | None, email: str | None) -> dict:
    row = db.fetchone(
        "update subscribers set"
        " phone = coalesce(%s, phone),"
        " email = coalesce(%s, email)"
        " where sub = %s returning *",
        (phone, email, sub),
    )
    return {"outcome": "updated" if row else "not_found", "phone": row["phone"] if row else None,
            "email": row["email"] if row else None}


def toggle_service(sub: str, service: str, enabled: bool) -> dict:
    row = db.fetchone(
        "update subscriber_services set enabled = %s"
        " where sub = %s and service = %s returning service, enabled",
        (enabled, sub, service),
    )
    if row is None:
        return {"outcome": "unknown_service"}
    return {"outcome": "toggled", "service": row["service"], "enabled": row["enabled"]}


# ── Billing ──────────────────────────────────────────────────────────────

def charges(sub: str, limit: int = 6) -> list[dict]:
    return db.fetchall(
        "SELECT month, label, amount_rub FROM billing_events"
        " WHERE sub = %s ORDER BY month DESC, id DESC LIMIT %s",
        (sub, limit),
    )


def tariff_changes_today(sub: str) -> int:
    row = db.fetchone(
        "SELECT count(*) AS n FROM tariff_change_log"
        " WHERE sub = %s AND created_at::date = current_date",
        (sub,),
    )
    return int(row["n"])


def apply_tariff_change(*, sub: str, new_tariff: str, idempotency_key: str) -> dict:
    """Исполнение смены тарифа: идемпотентно, с дневным лимитом.

    Исходы: executed / duplicate / daily_limit / not_found / same_tariff /
    unknown_tariff / tariff_not_available. Каркас вызова — всегда код
    оркестратора, LLM этого интерфейса не видит.
    """
    with db.connect() as conn, conn.cursor() as cur:
        known = cur.execute(
            "SELECT result FROM idempotency WHERE key = %s", (idempotency_key,)
        ).fetchone()
        if known:
            return {**known[0], "outcome": "duplicate"}

        record = cur.execute("SELECT tariff FROM subscribers WHERE sub = %s", (sub,)).fetchone()
        if record is None:
            return {"outcome": "not_found"}
        current = record[0]
        if current == new_tariff:
            return {"outcome": "same_tariff", "current": current}

        target = cur.execute(
            "SELECT price_rub, available FROM tariffs WHERE name = %s", (new_tariff,)
        ).fetchone()
        if target is None:
            return {"outcome": "unknown_tariff"}
        if not target[1]:
            return {"outcome": "tariff_not_available", "tariff": new_tariff}

        if tariff_changes_today(sub) >= DAILY_TARIFF_CHANGE_LIMIT:
            return {"outcome": "daily_limit", "limit": DAILY_TARIFF_CHANGE_LIMIT}

        cur_price = cur.execute(
            "SELECT price_rub FROM tariffs WHERE name = %s", (current,)
        ).fetchone()[0]

        cur.execute("UPDATE subscribers SET tariff = %s WHERE sub = %s", (new_tariff, sub))
        cur.execute(
            "INSERT INTO tariff_change_log(sub, before, after, idempotency_key)"
            " VALUES (%s, %s, %s, %s)",
            (sub, current, new_tariff, idempotency_key),
        )
        view = {
            "before": current,
            "after": new_tariff,
            "price_before": cur_price,
            "price_after": target[0],
        }
        cur.execute(
            "INSERT INTO idempotency(key, sub, action, result) VALUES (%s, %s, %s, %s)",
            (idempotency_key, sub, "tariff_change", __import__("json").dumps(view, ensure_ascii=False)),
        )
        conn.commit()
        return {"outcome": "executed", **view}


def set_daily_quota(sub: str, remaining: int) -> dict:
    """Стендовая команда: выставить остаток дневного лимита смен."""
    used = max(0, DAILY_TARIFF_CHANGE_LIMIT - remaining)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM tariff_change_log"
            " WHERE sub = %s AND created_at::date = current_date",
            (sub,),
        )
        for _ in range(used):
            cur.execute(
                "INSERT INTO tariff_change_log(sub, before, after) VALUES (%s, '—', '—')",
                (sub,),
            )
        conn.commit()
    return {"sub": sub, "changes_today": tariff_changes_today(sub)}


# ── Tickets ──────────────────────────────────────────────────────────────

def create_ticket(sub: str, subject: str, note: str) -> dict:
    row = db.fetchone(
        "insert into tickets(id, sub, subject, note)"
        " values ('t_' || lpad((floor(random() * 1679616))::text, 4, '0'), %s, %s, %s)"
        " returning id",
        (sub, subject, note),
    )
    return {"ticket_id": row["id"], "status": "open"}


def tickets(sub: str | None = None) -> list[dict]:
    if sub:
        rows = db.fetchall(
            "SELECT id, sub, subject, note, status,"
            " to_char(created_at, 'YYYY-MM-DD HH24:MI') AS created_at"
            " FROM tickets WHERE sub = %s ORDER BY created_at DESC",
            (sub,),
        )
    else:
        rows = db.fetchall(
            "SELECT id, sub, subject, note, status,"
            " to_char(created_at, 'YYYY-MM-DD HH24:MI') AS created_at"
            " FROM tickets ORDER BY created_at DESC"
        )
    return rows


def counts() -> dict:
    out = {}
    for table in ("subscribers", "tariffs", "billing_events", "kb_chunks", "tickets", "audit_log"):
        row = db.fetchone(f"SELECT count(*) AS n FROM {table}")
        out[table] = int(row["n"])
    return out
