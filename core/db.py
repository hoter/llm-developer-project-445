"""Postgres контура: соединение и миграции.

    python -m core.db migrate     # применить db/*.sql по порядку

Для учебного стенда — короткоживущие соединения на операцию: просто и
достаточно; в проде здесь был бы пул.
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from core.config import PG_DSN, ROOT

DB_DIR = ROOT / "db"


def connect() -> psycopg.Connection:
    return psycopg.connect(PG_DSN)


def migrate() -> list[str]:
    """Применить db/*.sql по порядку; идемпотентно (create if not exists)."""
    applied = []
    with connect() as conn, conn.cursor() as cur:
        for sql_file in sorted(DB_DIR.glob("*.sql")):
            cur.execute(sql_file.read_text(encoding="utf-8"))
            applied.append(sql_file.name)
        conn.commit()
    return applied


def _plain(row: tuple) -> tuple:
    """numeric → float: значения уходят в JSON-ответы и tool-результаты."""
    from decimal import Decimal

    return tuple(float(x) if isinstance(x, Decimal) else x for x in row)


def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, _plain(row))) for row in cur.fetchall()]


def fetchone(sql: str, params: tuple = ()) -> dict | None:
    rows = fetchall(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        for name in migrate():
            print(f"ok: {name}")
    else:
        print("usage: python -m core.db migrate")
