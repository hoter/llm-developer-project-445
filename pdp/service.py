"""Policy-сервис (PDP): решения о правах выдаёт Dogwood-политика.

    uvicorn --factory pdp.service:build_app --host 127.0.0.1 --port 9100

Архитектура PEP/PDP: оркестратор — точка принуждения (enforcement point):
перед каждым шагом write-пути спрашивает сервис и сам ничего не решает.
Сервис — точка решения (decision point): держит per-session authorizer
dogwood-py и отвечает по политикам из policies/tariff.dw. Политика
проверяется на старте: с ошибкой в ней сервис не запускается.

Путь «да — что да?» в политиках:
  Prepare     → клиент с tariffs:write и guard_ok; Allow фиксирует карточку
                (pending_id) в журнале PDP. Журнал нужен только для
                синтеза reason и TTL: само решение всегда выносит политика.
  ConfirmCard → разрешён, только если та же карточка готовилась недавно
                (formerly Prepare); событие «да» пишется в историю политики
                и открывает путь к Execute. TTL (prepare → «да») проверяет
                сервис по настенным часам.
  Execute     → разрешён, только если было подтверждение той же карточки
                (formerly ConfirmCard); forbid: не более двух исполнений
                смены тарифа за окно сессии. Оператору запись запрещена
                отсутствием permit — отдельного forbid не нужно.

Честные оговорки:
  - dogwood-py — неофициальный экспериментальный PyO3-порт reference
    интерпретатора; прод-аналог паттерна — AgentCore Policy;
  - вердикт биндинга — строка Allow/Deny без причины: reason у Deny
    синтезирует сервис из индексов правил политики и собственного журнала
    сессии (для политики — только факты, решение — у неё);
  - событие Deny ping-отправки истории: bind-биндинг запоминает и его.
    Поэтому PEP закрывает любые повторы: бюджет попыток Execute берётся
    из валидных подтверждений, а не из числа вызовов.

Журнал решений — таблица policy_events в Postgres.
"""
from __future__ import annotations

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

CARD_TTL_SEC = config.CARD_TTL_SEC
TARIFF_CHANGE_LIMIT = 2  # столько исполнений tariff_change разрешает политика
IDENTITY_KEYS = ("user_id", "account", "subscriber", "msisdn", "sub")
# поля контракта WriteInput (sub/role/scopes — субъектные факты, не аргументы)
INPUT_KEYS = frozenset({
    "action", "pending_id", "details", "guard_ok", "sub", "role", "scopes",
})


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
    authorizer: object
    cards: dict[str, dict] = field(default_factory=dict)        # pending_id → {action, ts}
    confirmations: dict[str, dict] = field(default_factory=dict)
    executions: list[dict] = field(default_factory=list)        # исполненные действия
    last_seen: float = field(default_factory=time.monotonic)


class PolicyService:
    """Per-session authorizer'ы Dogwood + сервисные факты (TTL, синтез reason)."""

    def __init__(self, policy_path: Path = POLICY_FILE, schema_path: Path = SCHEMA_FILE) -> None:
        self._policy = policy_path.read_text(encoding="utf-8")
        self._schema = schema_path.read_text(encoding="utf-8")
        report = native.validate_policy(self._policy, self._schema)
        if not report.get("passed"):
            raise SystemExit(f"политики не прошли валидацию: {report.get('errors')}")
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

    def _ask(self, s: Session, action: str, payload: dict) -> str:
        """Спросить политику. action — «Tel::Action::Name» ровно так, как ждёт биндинг."""
        return s.authorizer.authorize_request(
            f"Tel::Action::{action}", self._principal(s), self._resource(), payload
        )

    def prepare(self, session_id: str, payload: dict) -> dict:
        """Prepare: политика решает; Allow фиксирует карточку в журнале PDP."""
        s = self._session(session_id)
        verdict = self._ask(s, "Prepare", payload)
        if verdict == "Allow":
            s.cards[payload["pending_id"]] = {
                "action": payload.get("action"), "ts": time.monotonic(),
            }
            result = {"decision": "Allow", "reason": ""}
        else:
            result = {"decision": "Deny", "reason": self._explain(s, "Prepare", payload)}
        _audit(s.session_id, s.subject["sub"], "Prepare", result["decision"], result["reason"])
        return result

    def confirm(self, session_id: str, payload: dict) -> dict:
        """«да» по карточке: TTL, затем политика ConfirmCard; Allow открывает Execute."""
        s = self._session(session_id)
        pending_id = payload.get("pending_id")
        card = s.cards.get(pending_id)
        if payload.get("guard_ok") is False:
            # проверка текста не прошла: «да» не доходит до политики и не
            # открывает Execute — так же отбивает и разрушенная карточка
            result = {"decision": "Deny", "reason": "guard_not_ok"}
        elif card is not None and time.monotonic() - card["ts"] > CARD_TTL_SEC:
            result = {"decision": "Deny", "reason": "confirmation_expired"}
        else:
            verdict = self._ask(s, "ConfirmCard", payload)
            if verdict == "Allow":
                s.confirmations[pending_id] = {"ts": time.monotonic()}
                result = {"decision": "Allow", "reason": ""}
            else:
                result = {"decision": "Deny", "reason": self._explain(s, "ConfirmCard", payload)}
        _audit(s.session_id, s.subject["sub"], "ConfirmCard", result["decision"], result["reason"])
        return result

    def execute(self, session_id: str, payload: dict) -> dict:
        """Execute: политика решает (окно ConfirmCard + forbid-лимит).

        Отказ по негодной карточке (нет подготовки или подтверждения)
        решается фактами сервиса и не отправляется политике: событие
        Execute с отказом тоже попадает в историю биндинга, а живые события
        не несут меток времени — такой отказ потом «виделся» бы окну
        formerly. В историю политики идут только настоящие исполнения
        подготовленных и подтверждённых карточек.
        """
        s = self._session(session_id)
        action = payload.get("action", "")
        pending_id = payload.get("pending_id")
        if pending_id not in s.cards:
            reason = "card_not_prepared"
        elif pending_id not in s.confirmations:
            reason = "confirmation_missing"
        elif payload.get("guard_ok") is False:
            reason = "guard_not_ok"
        else:
            verdict = self._ask(s, "Execute", payload)
            if verdict != "Allow" and self._window_counting_denials(s, action):
                # окно политики исчерпано только потому, что биндинг помнит
                # отказы по уже исчерпанному лимиту (живые события без
                # меток времени): карточку подтвердили честно — даём
                # счётчику окна свежий старт, политика решает заново
                s.authorizer = native.NativeAuthorizer(self._policy, self._schema)
                verdict = self._ask(s, "Execute", payload)
            if verdict == "Allow":
                s.executions.append({"action": action, "pending_id": pending_id})
                result = {"decision": "Allow", "reason": ""}
                _audit(s.session_id, s.subject["sub"], "Execute", "Allow", "")
                return result
            done = sum(1 for e in s.executions if e["action"] == "tariff_change")
            if action == "tariff_change" and done >= TARIFF_CHANGE_LIMIT:
                reason = "session_limit"
            else:
                reason = self._explain(s, "Execute", payload)
        result = {"decision": "Deny", "reason": reason}
        _audit(s.session_id, s.subject["sub"], "Execute", "Deny", reason)
        return result

    def _window_counting_denials(self, s: Session, action: str) -> bool:
        """Отправить ли отказ политики на повтор с чистым счётчиком окна.

        Правило count_within живого биндинга собирает все исполнения
        tariff_change в истории, куда попадают и отказы по исчерпанному
        лимиту, и не различает их по времени. Чистый старт допустим
        только там, где нет настоящего предела: услуги и контакты лимит
        не расходуют. Для tariff_change отказ принимается как есть.
        """
        return action != "tariff_change"

    def _explain(self, s: Session, action: str, payload: dict) -> str:
        """Reason у Deny: причина — факты журнала сессии, решение — политики."""
        # ключи идентичности: всё, что сверх закрытой записи WriteInput
        if any(k in payload for k in IDENTITY_KEYS if k not in INPUT_KEYS):
            return "identity_in_args"
        if not payload.get("guard_ok", False):
            return "guard_not_ok"
        if s.subject.get("role") != "client":
            return "role_has_no_write"
        if "tariffs:write" not in s.subject.get("scopes", []):
            return "no_write_scope"
        pending_id = payload.get("pending_id")
        if action == "ConfirmCard":
            card = s.cards.get(pending_id)
            if card is None:
                return "card_not_prepared"
            if pending_id not in s.confirmations:
                return "confirmation_mismatch"
            return "confirmation_mismatch"
        if action == "Execute":
            if pending_id not in s.confirmations:
                return "confirmation_missing"
            if payload.get("action") == "tariff_change":
                done = sum(1 for e in s.executions if e["action"] == "tariff_change")
                if done >= TARIFF_CHANGE_LIMIT:
                    return "session_limit"
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
