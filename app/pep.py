"""PEP: оркестратор спрашивает PDP — сам решений о write-пути не принимает.

TODO(права):
  1. Decision(allowed, reason) и Pep(session_id, claims, guard_state):
     субъектные факты из проверенного токена — sub, role,
     scopes = claims["scope"].split().
  2. _open(): POST {PDP_URL}/session {"session_id", "subject"} — один раз.
  3. prepare(card) → POST /authorize {action: "Prepare"} — Allow фиксирует
     карточку в журнале PDP; Deny — карточки не существует.
  4. confirm(card) → POST /confirm {action: "ConfirmCard"} — «да»;
     TTL карточки проверяет PDP по wall clock.
  5. execute(card) → POST /authorize {action: "Execute"} — окно
     formerly ConfirmCard + forbid-лимит.
  6. input для политики: {"action": card.action, "pending_id",
     "details": json.dumps(card.details), "guard_ok": bool(guard_state),
     **subject}. Недоступный PDP — fail-closed: Decision(False,
     "pdp_unavailable").
"""
from __future__ import annotations
