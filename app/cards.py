"""Карточки подтверждений: состояние оркестратора, не текст промпта.

ГОТОВЫЙ каркас (контракты) — не меняйте: prepare-инструмент возвращает
карточку, обёртка кладёт её в pending_cards (Postgres); TTL держит база;
ответ классифицирует код (classify_answer), не LLM. С правами решения
о карточке усиливаются политикой Dogwood (Prepare → ConfirmCard →
Execute) через app.pep — каркас остаётся.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core import config, db

YES = ("да", "ага", "давай", "конечно", "подтверждаю", "подтверди", "верно", "угу", "продолжай")
NO = ("нет", "не надо", "отмена", "отменить", "откажись", "не нужно", "стоп")
OPERATOR = ("оператор", "человека", "живой человек", "поддержку", "менеджера", "специалиста")


@dataclass
class Card:
    pending_id: str
    action: str
    details: dict
    view: dict
    session_id: str
    sub: str
    prepare_trace_id: str = ""   # trace хода, где карточка готовилась

    def public_view(self, ttl_sec: int) -> dict:
        return {**self.view, "action": self.action, "ttl_sec": ttl_sec}


def create(session, action: str, details: dict, view: dict) -> Card:
    """Новая карточка на сессию: прежняя активная сбрасывается в replaced."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "update pending_cards set state = 'replaced'"
            " where session_id = %s and state = 'pending'",
            (session.session_id,),
        )
        pending_id = f"p_{uuid.uuid4().hex[:8]}"
        expires = datetime.now(timezone.utc) + timedelta(seconds=config.CARD_TTL_SEC)
        cur.execute(
            "insert into pending_cards(pending_id, session_id, sub, action, details, card, expires_at)"
            " values (%s, %s, %s, %s, %s, %s, %s)",
            (
                pending_id, session.session_id, session.user.sub, action,
                _json(details), _json(view), expires,
            ),
        )
        conn.commit()
    card = Card(pending_id, action, details, view, session.session_id, session.user.sub)
    session.pending_card = card
    return card


def active(session) -> Card | None:
    """Живая карточка сессии (созданная этим или предыдущим ходом)."""
    if session.pending_card is not None:
        return session.pending_card
    row = db.fetchone(
        "select pending_id, action, details, card, sub from pending_cards"
        " where session_id = %s and state = 'pending' and expires_at > now()"
        " order by created_at desc limit 1",
        (session.session_id,),
    )
    if row is None:
        return None
    card = Card(
        pending_id=row["pending_id"], action=row["action"],
        details=row["details"], view=row["card"],
        session_id=session.session_id, sub=row["sub"],
    )
    session.pending_card = card
    return card


def settle(session, card: Card, state: str) -> None:
    """Закрыть карточку исходом: confirmed / rejected / expired / denied / replaced."""
    db.execute("update pending_cards set state = %s where pending_id = %s", (state, card.pending_id))
    if session.pending_card is card or session.pending_card is None:
        session.pending_card = None


def classify_answer(text: str) -> str:
    """Ответ на карточку: yes / no / operator / topic (детерминированный код)."""
    lowered = text.strip().lower()
    if any(w in lowered for w in YES):
        return "yes"
    if any(w in lowered for w in NO):
        return "no"
    if any(w in lowered for w in OPERATOR):
        return "operator"
    return "topic"  # смена темы: обслуживаем, карточка живёт


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
