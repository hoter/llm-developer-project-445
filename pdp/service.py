"""Policy-сервис (PDP): решения о правах выдаёт Dogwood-политика.

    uvicorn --factory pdp.service:build_app --host 127.0.0.1 --port 9100

Архитектура PEP/PDP: оркестратор — точка принуждения (enforcement
point): перед каждым шагом write-пути спрашивает сервис; сам решений
не принимает. Сервис — точка решения (decision point): держит
per-session authorizer dogwood-py и отвечает по политикам из
policies/tariff.dw.

Путь «да — что да?» в политиках:
  Prepare     → разрешён клиенту с tariffs:write (идентичность — не
                аргумент); Allow фиксируется в журнале карточек
  ConfirmCard → разрешён, только если та же карточка (pending_id)
                недавно готовилась — formerly Prepare; событие «да»
                пишется в историю политики и открывает путь к Execute
  Execute     → разрешён, только если было подтверждение той же
                карточки — formerly ConfirmCard; forbid-лимит: не более
                двух исполнений тарифа за сессию

Субъектные факты (sub/role/scopes) сервису передаёт PEP — они взяты из
JWT, который backend уже получил у издателя (доверенный клиент).

Честные оговорки:
  - dogwood-py — неофициальный экспериментальный PyO3-порт reference
    интерпретатора; прод-аналог паттерна — AgentCore Policy;
  - живые события не несут таймстемпов (все ts=0) — окна formerly в live
    не закрываются, поэтому TTL карточки (prepare → «да») дополнительно
    проверяет сервис по wall clock; семантика окон закрыта replay-тестами;
  - вердикт биндинга — строка Allow/Deny без причины: reason у Deny
    синтезирует сервис из собственного журнала сессии.

Журнал решений — таблица policy_events в Postgres.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from dogwood import native
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core import config, db

ROOT = Path(__file__).resolve().parent
POLICY_FILE = ROOT / "policies" / "tariff.dw"
SCHEMA_FILE = ROOT / "policies" / "schema.cedarschema"

SESSION_IDLE_SEC = int(1e9)  # сессия PDP живёт вместе с сессией чата
CARD_TTL_SEC = config.CARD_TTL_SEC
TARIFF_CHANGE_LIMIT = 2  # для синтеза reason; само число считает политика
IDENTITY_KEYS = ("user_id", "account", "subscriber", "msisdn")


def _audit(session_id: str, sub: str, action: str, decision: str, reason: str) -> None:
    try:
        db.execute(
            "insert into policy_events(session_id, sub, action, decision, reason)"
            " values (%s, %s, %s, %s, %s)",
            (session_id, sub, action, decision, reason),
        )
    except Exception:  # noqa: BLE001 — журнал решений не должен валить сервис
        pass


@dataclass
class Session:
    session_id: str
    subject: dict
    authorizer: object = None
    cards: dict[str, dict] = field(default_factory=dict)        # pending_id → {action, ts}
    confirmations: dict[str, dict] = field(default_factory=dict)
    execute_requests: int = 0
    last_seen: float = field(default_factory=time.monotonic)


class PolicyService:
    """Per-session authorizer'ы + сервисные проверки, которых нет в политике."""

    def __init__(self) -> None:
        self._policy = POLICY_FILE.read_text(encoding="utf-8")
        self._schema = SCHEMA_FILE.read_text(encoding="utf-8")
        report = native.validate_policy(self._policy, self._schema)
        if not report.get("passed"):
            raise SystemExit(f"политики не прошли валидацию: {report['errors']}")
        self._sessions: dict[str, Session] = {}

    def open_session(self, session_id: str, subject: dict) -> Session:
        s = Session(
            session_id=session_id,
            subject=dict(subject),
            authorizer=native.NativeAuthorizer(self._policy, self._schema),
        )
        self._sessions[session_id] = s
        return s

    def _session(self, session_id: str) -> Session:
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(f"нет сессии {session_id} — PEP не открывал сессию")
        s.last_seen = time.monotonic()
        return s

    def _authorize(self, s: Session, action: str, payload: dict) -> dict:
        verdict = s.authorizer.authorize_request(
            f"Tel::Action::{action}", self._principal(s), self._resource(), payload
        )
        decision = "Allow" if verdict == "Allow" else "Deny"
        reason = "" if decision == "Allow" else self._explain(s, action, payload)
        _audit(s.session_id, s.subject["sub"], action, decision, reason)
        return {"decision": decision, "reason": reason}

    def prepare(self, session_id: str, payload: dict) -> dict:
        """Prepare: решение политики; Allow фиксирует карточку в журнале."""
        s = self._session(session_id)
        result = self._authorize(s, "Prepare", payload)
        if result["decision"] == "Allow":
            s.cards[payload["pending_id"]] = {
                "action": payload.get("action"), "ts": time.monotonic(),
            }
        return result

    def confirm(self, session_id: str, payload: dict) -> dict:
        """«Да» по карточке: сервисные факты → политика → событие в историю.

        Событие запроса пишется в историю политики даже при Deny — поэтому
        PDP не предлагает политике «да» без живой карточки: подтверждение
        без подготовленной карточки — не факт, и фантома в истории быть
        не должно. Живые события не несут таймстемпов, окно formerly
        Prepare в live не закрывается — время жизни карточки проверяет
        сервис.
        """
        s = self._session(session_id)
        pending_id = payload.get("pending_id")
        card = s.cards.get(pending_id)
        if card is None:
            result = {"decision": "Deny", "reason": "card_not_prepared"}
        elif time.monotonic() - card["ts"] > CARD_TTL_SEC:
            result = {"decision": "Deny", "reason": "confirmation_expired"}
        else:
            result = self._authorize(s, "ConfirmCard", payload)
            if result["decision"] == "Allow":
                s.confirmations[pending_id] = {
                    "action": payload.get("action"), "ts": time.monotonic(),
                }
        _audit(s.session_id, s.subject["sub"], "ConfirmCard", result["decision"], result["reason"])
        return result

    def execute(self, session_id: str, payload: dict) -> dict:
        """Execute: решение политики (окно ConfirmCard + лимит — forbid)."""
        s = self._session(session_id)
        s.execute_requests += 1
        return self._authorize(s, "Execute", payload)

    def _explain(self, s: Session, action: str, payload: dict) -> str:
        """Reason у Deny: биндинг отдаёт только строку, причину называет сервис."""
        if any(k in payload for k in IDENTITY_KEYS):
            return "identity_in_args"
        if not payload.get("guard_ok", False):
            return "guard_not_ok"
        if s.subject["role"] != "client":
            return "role_has_no_write"
        if "tariffs:write" not in s.subject["scopes"]:
            return "no_write_scope"
        if action == "ConfirmCard":
            if payload.get("pending_id") not in s.cards:
                return "card_not_prepared"
            return "confirmation_mismatch"
        if action == "Execute":
            if s.execute_requests > TARIFF_CHANGE_LIMIT:
                return "session_limit"
            if payload.get("pending_id") not in s.confirmations:
                return "confirmation_missing"
            return "confirmation_mismatch"
        return "deny_by_default"

    @staticmethod
    def _principal(s: Session) -> str:
        return f'Tel::OAuthUser::"{s.subject["sub"]}"'

    @staticmethod
    def _resource() -> str:
        return 'Tel::Gateway::"pdp"'


SERVICE = PolicyService()


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "sessions": len(SERVICE._sessions)})


async def open_session(request: Request) -> JSONResponse:
    body = await request.json()
    subject = body.get("subject", {})
    session_id = body.get("session_id", "")
    if not session_id or not subject.get("sub"):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    SERVICE.open_session(session_id, subject)
    return JSONResponse({"ok": True, "session_id": session_id})


async def authorize(request: Request) -> JSONResponse:
    body = await request.json()
    action = body.get("action", "")
    if action not in ("Prepare", "Execute"):
        return JSONResponse({"error": "unknown_action"}, status_code=400)
    try:
        if action == "Prepare":
            result = SERVICE.prepare(body["session_id"], body.get("input", {}))
        else:
            result = SERVICE.execute(body["session_id"], body.get("input", {}))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(result)


async def confirm(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        result = SERVICE.confirm(body["session_id"], body.get("input", {}))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(result)


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/session", open_session, methods=["POST"]),
            Route("/authorize", authorize, methods=["POST"]),
            Route("/confirm", confirm, methods=["POST"]),
        ],
    )


if __name__ == "__main__":
    # один воркер: authorizer однопоточный (unsendable), журнал сессии живёт в процессе
    uvicorn.run(build_app(), host="127.0.0.1", port=config.PDP_PORT, log_level="warning")
