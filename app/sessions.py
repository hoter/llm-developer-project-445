"""Сессии чата: identity, соединения с MCP, история планировщика.

Одна сессия = один пользователь чата. При открытии сессии backend —
доверенный клиент — выпускает у издателя токены для всех MCP-серверов
(по одному на resource, RFC 8707) и держит открытые соединения.

Готово — в контрактах меняется одна строка (resource_keys), в правах вы ДОПОЛНИТЕ Session полями
claims / allowlist / pep / guard_state: claims токена определяют allowlist
(app.permissions), pep — клиент PDP (app.pep, лениво), guard_state —
verdikt guardrail хода. Там же функция pep_of(session) — отдаёт PEP
сессии, создавая его при первом обращении (её зовёт оркестратор и smoke).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.permissions import allowlist_from_claims
from core import config, db
from core.mcpclient import McpPool
from core.tokens import Tokens


@dataclass
class Session:
    login: str
    user: config.User
    tokens: Tokens
    pool: McpPool
    allowlist: object | None = None              # права из claims (app.permissions)
    thread: list = field(default_factory=list)   # история планировщика
    trace: list[str] = field(default_factory=list)
    trace_id: str = ""                           # сквозной id текущего хода
    planner: object | None = None                # собирается лениво на первом ходе
    pending_card: object | None = None           # живая карточка подтверждения
    pep: object | None = None                    # клиент PDP (лениво, app.pep)
    confirmed_card: object | None = None         # карточка, исполненная в сессии
    session_id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:8]}")

    def note(self, tool: str) -> None:
        self.trace.append(tool)


SESSIONS: dict[str, Session] = {}


async def open_session(login: str) -> Session:
    user = config.USERS.get(login)
    if user is None:
        raise KeyError(f"неизвестный пользователь: {login}")

    tokens = Tokens(login)
    pool = McpPool(tokens, resource_keys=("crm", "billing", "kb", "actions"))
    await pool.connect()

    # allowlist строится из claims токена, проверенного издателем, а не
    # из локальной конфигурации пользователя
    claims = await tokens.claims("actions")
    allowlist = allowlist_from_claims(claims)

    session = Session(login=login, user=user, tokens=tokens, pool=pool, allowlist=allowlist)
    db.execute(
        "insert into sessions(session_id, login, sub) values (%s, %s, %s)",
        (session.session_id, login, user.sub),
    )
    SESSIONS[session.session_id] = session
    return session


def may_write(user: config.User) -> bool:
    """Клиент с правом на тарифы может готовить действия; оператор — нет."""
    return user.role == "client" and "tariffs:write" in user.scopes


def get(session_id: str) -> Session | None:
    return SESSIONS.get(session_id)


def pep_of(session: Session):
    """PEP сессии: создаётся при первом обращении; субъект — из claims токена."""
    from app.pep import Pep

    pep = session.pep
    if pep is None:
        allowlist = session.allowlist
        pep = Pep(session.session_id, {
            "sub": allowlist.sub,
            "role": allowlist.role,
            "scopes": list(allowlist.scopes),
        })
        session.pep = pep
    return pep
