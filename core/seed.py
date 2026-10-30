"""Сид демо-данных в Postgres: пользователи, тарифы, абоненты, услуги,
списания, база знаний (статьи + векторы из снапшота).

    python -m core.seed

Пересоздаёт доменные таблицы целиком (идемпотентно): удобно и для
первого запуска, и для `make seed` в любой момент.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from core import db
from core.config import ROOT

DEMO_FILE = ROOT / "data" / "demo.yaml"
SNAPSHOT_FILE = ROOT / "kb" / "vectors.json"


def _vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def seed() -> dict:
    demo = yaml.safe_load(DEMO_FILE.read_text(encoding="utf-8"))

    with db.connect() as conn, conn.cursor() as cur:
        # домен оператора: чистая перезаливка; контурные таблицы сессий,
        # карточек и идемпотентности тоже сбрасываются — reset = исходное демо
        for table in (
            "billing_events", "tariff_change_log", "subscriber_services",
            "subscribers", "tariffs", "tickets", "kb_chunks",
            "idempotency", "pending_cards", "sessions",
            "users",
        ):
            cur.execute(f"delete from {table}")

        for t in demo["tariffs"]:
            cur.execute(
                "insert into tariffs(name, price_rub, data_gb, description, available)"
                " values (%s, %s, %s, %s, %s)",
                (t["name"], t["price_rub"], t["data_gb"], t["description"], t["available"]),
            )

        login_by_sub: dict[str, str] = {}
        # все пользователи контура (включая оператора без абонентской записи)
        for user in _users():
            cur.execute(
                "insert into users(login, sub, name, role, scopes)"
                " values (%s, %s, %s, %s, %s)"
                " on conflict (login) do update set sub = excluded.sub,"
                " role = excluded.role, scopes = excluded.scopes",
                (user["login"], user["sub"], user["name"], user["role"], user["scopes"]),
            )
        for s in demo["subscribers"]:
            cur.execute(
                "insert into subscribers(sub, login, name, phone, email, tariff, balance_rub)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (s["sub"], s["login"], s["name"], s["phone"], s.get("email"), s["tariff"], s["balance_rub"]),
            )
            login_by_sub[s["sub"]] = s["login"]
            for svc in s.get("services", []):
                price = next(
                    (c["price_rub"] for c in demo["service_catalog"] if c["name"] == svc["name"]), 0
                )
                cur.execute(
                    "insert into subscriber_services(sub, service, enabled, price_rub)"
                    " values (%s, %s, %s, %s)",
                    (s["sub"], svc["name"], svc["enabled"], price),
                )

        for c in demo["charges"]:
            cur.execute(
                "insert into billing_events(sub, month, label, amount_rub)"
                " values (%s, %s, %s, %s)",
                (c["sub"], c["month"], c["label"], c["amount_rub"]),
            )

        # база знаний: статьи + векторы из снапшота
        articles = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        for a in articles:
            cur.execute(
                "insert into kb_chunks(source, title, body, embedding)"
                " values (%s, %s, %s, %s::vector)",
                (a["source"], a["title"], a["body"], _vector_literal(a["vector"])),
            )

        conn.commit()

    return {
        "users": len(demo["subscribers"]),
        "tariffs": len(demo["tariffs"]),
        "kb_chunks": len(articles),
    }


def _users() -> list[dict]:
    from core.config import USERS

    return [
        {"login": u.login, "sub": u.sub, "name": u.name, "role": u.role, "scopes": u.scopes}
        for u in USERS.values()
    ]


if __name__ == "__main__":
    print("seed:", seed())
