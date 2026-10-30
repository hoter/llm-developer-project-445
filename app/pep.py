"""PEP: оркестратор спрашивает PDP — сам решений о write-пути не принимает.

Цикл карточки спрашивает сервис решений трижды:

  prepare(card) → POST /authorize {action: "Prepare"}   — Allow фиксирует
                  карточку (pending_id) в журнале PDP; Deny — карточки нет;
  confirm(card) → POST /confirm {action: "ConfirmCard"} — «да» по той же
                  карточке (formerly Prepare); TTL карточки проверяет PDP
                  по настенным часам;
  execute(card) → POST /authorize {action: "Execute"}   — окно formerly
                  ConfirmCard + forbid-лимит смен тарифа.

Субъектные факты (sub, role, scopes) — из проверенных claims токена.
Недоступный PDP — fail-closed: Decision(False, "pdp_unavailable"), действие
закрыто, абонент получает понятный отказ.

В input для политики: action, pending_id, details (JSON-строка, для политики
непрозрачна), guard_ok (verdikt guardrail текущего хода: false, если
сервис проверки текста недоступен или не пропустил реплику) и субъект.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config

# reason, который PEP ставит, когда сам сервис решений недоступен
PDP_DOWN = "pdp_unavailable"


@dataclass
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class Pep:
    """Клиент PDP на одну сессию чата."""

    def __init__(self, session_id: str, subject: dict, guard_state: dict | None = None) -> None:
        self.session_id = session_id
        self.subject = {k: subject[k] for k in ("sub", "role", "scopes") if k in subject}
        # тот же словарь, что у сессии: verdikt обновляется на каждом ходу
        self.guard_state = guard_state if guard_state is not None else {}
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
        await self._post("/session", {"session_id": self.session_id, "subject": self.subject})
        self._opened = True

    def _input(self, card) -> dict:
        return {
            "action": card.action,
            "pending_id": card.pending_id,
            "details": json.dumps(card.details, ensure_ascii=False),
            "guard_ok": bool(self.guard_state.get("ok", True)),
            **self.subject,
        }

    async def _ask(self, path: str, action: str | None, card) -> Decision:
        payload = {"session_id": self.session_id, "input": self._input(card)}
        if action is not None:
            payload["action"] = action
        try:
            await self._open()
            body = await self._post(path, payload)
        except Exception:  # noqa: BLE001 — PDP недоступен: действия закрыты
            return Decision(False, PDP_DOWN)
        return Decision(str(body.get("decision", "")).lower() == "allow",
                        body.get("reason") or "")

    async def prepare(self, card) -> Decision:
        return await self._ask("/authorize", "Prepare", card)

    async def confirm(self, card) -> Decision:
        # «да»: PDP сам добавляет action=ConfirmCard в input
        return await self._ask("/confirm", None, card)

    async def execute(self, card) -> Decision:
        return await self._ask("/authorize", "Execute", card)
