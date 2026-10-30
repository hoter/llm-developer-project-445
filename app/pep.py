"""PEP: оркестратор спрашивает PDP — сам решений о write-пути не принимает.

Пока политики шага прав не написаны, PDP существует, но ничего не
разрешает и не пишет в журнал решений. Такой ответ («PDP жив, политик
на это действие нет») — не отказ прав: оркестратор обслуживает карточку
сам, как на шаге контрактов. Отказ прав — это Deny с причиной или
недоступный PDP (fail-closed, action "pdp_unavailable"): тогда действие
закрыто.

TODO(права): политики и полный цикл —
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

import json
from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config

# action в policy_events, который ставит PEP, когда PDP отключён
PDP_DOWN = "pdp_unavailable"
# так PDP (шаг прав) называет Prepare такому действию, если политик ещё нет
NO_POLICY = "no_policy"


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    # True — решение пришло от политики (включая Deny); False — политик нет
    by_policy: bool = False

    def __bool__(self) -> bool:
        return self.allowed


# Причины Deny, которые означают «политики на это действие ещё нет»
# (их ставит deny by default в PDP без политик шага прав)
NO_POLICY_REASONS = frozenset({"no_policy", "no_applicable_policy", "deny_by_default"})


class Pep:
    """Клиент PDP на одну сессию чата."""

    def __init__(self, session_id: str, subject: dict) -> None:
        self.session_id = session_id
        self.subject = subject
        self._opened = False

    @property
    def url(self) -> str:
        return config.PDP_URL

    async def _post(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            r = await client.post(f"{self.url}{path}", json=payload)
            r.raise_for_status()
            return r.json()

    async def _open(self) -> None:
        if self._opened:
            return
        try:
            await self._post("/session", {"session_id": self.session_id, "subject": self.subject})
            self._opened = True
        except Exception:  # noqa: BLE001 — PDP не принимает сессию: политик нет
            pass

    def _input(self, card) -> dict:
        return {
            "action": card.action,
            "pending_id": card.pending_id,
            "details": json.dumps(card.details, ensure_ascii=False),
            "guard_ok": True,
            **self.subject,
        }

    async def _decision(self, body: dict) -> Decision:
        decision = str(body.get("decision", "")).lower()
        reason = body.get("reason") or ""
        if decision != "allow" and reason == PDP_DOWN:
            return Decision(False, PDP_DOWN, by_policy=True)
        if reason in NO_POLICY_REASONS:
            # PDP жив, но политики на действие ещё нет (шаг прав):
            # карточку обслуживает оркестратор, как без прав
            return Decision(True, "", by_policy=False)
        return Decision(decision == "allow",
                        reason or ("allowed" if decision == "allow" else "denied"),
                        by_policy=True)

    async def _call(self, path: str, action: str | None, card) -> Decision:
        payload = {"session_id": self.session_id, "input": self._input(card)}
        if action is not None:
            payload["action"] = action
        try:
            await self._open()
            body = await self._post(path, payload)
        except Exception:  # noqa: BLE001 — PDP недоступен: политик нет
            return Decision(False, PDP_DOWN, by_policy=False)
        return await self._decision(body)

    async def prepare(self, card) -> Decision:
        return await self._call("/authorize", "Prepare", card)

    async def confirm(self, card) -> Decision:
        # «да» по карточке: тело {session_id, input}; действие — ConfirmCard
        payload = {"session_id": self.session_id, "action": "ConfirmCard", "input": self._input(card)}
        try:
            await self._open()
            body = await self._post("/confirm", payload)
        except Exception:  # noqa: BLE001
            return Decision(False, PDP_DOWN, by_policy=False)
        return await self._decision(body)

    async def execute(self, card) -> Decision:
        return await self._call("/authorize", "Execute", card)
