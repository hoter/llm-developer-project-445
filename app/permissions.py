"""Права сессии: allowlist из claims токена, два барьера оркестратора.

Права — инженерная спецификация, а не просьба в промпте:

* allowlist строится из проверенных claims токена (sub, role, scope);
* барьер 1 (visible_tools) — при сборке агентов: запрещённого инструмента
  модель просто не видит;
* барьер 2 (admit) — в момент вызова: тройка «субъект, операция, объект»
  допускается или нет. Лимиты и подтверждения — дело политики (шаг 6).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Операции по домену. Чтение своего — всем авторизованным.
READ_OPS = (
    "get_my_profile",
    "get_my_tariff",
    "list_tariffs",
    "list_my_charges",
    "search_kb",
)
# Карточные действия: готовит клиент с правом записи, исполняет политика.
WRITE_OPS = (
    "prepare_tariff_change",
    "prepare_toggle_service",
    "prepare_contacts_update",
)
# Обзор чужого абонента: только оператор, чужой id легитимен (роль
# проверяет сервер по claims токена).
OPERATOR_OPS = ("get_subscriber_overview", "find_subscribers")
# Эскалация: позвать человека можно всегда.
ESCALATION_OPS = ("create_ticket",)
# Исполнение карточки модель не видит никогда — только код оркестратора.
EXECUTION_OPS = ("execute_action",)

# Ключи идентичности в аргументах: субъект определяется токеном, не
# аргументом. phone здесь нет: в prepare_contacts_update это новое значение.
IDENTITY_ARGS = frozenset({"user_id", "account", "subscriber", "msisdn", "sub"})

# Scope, без которого клиент не готовит действия.
WRITE_SCOPE = "tariffs:write"


@dataclass
class Allowlist:
    sub: str
    role: str
    scopes: list[str] = field(default_factory=list)
    read_ops: list[str] = field(default_factory=list)
    write_ops: list[str] = field(default_factory=list)

    def operations(self) -> set[str]:
        return set(self.read_ops) | set(self.write_ops)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def allowlist_from_claims(claims: dict) -> Allowlist:
    """Claims проверенного токена → allowlist субъекта.

    Роль даёт базовый набор, scope его сужает: карточные действия — только
    клиенту с tariffs:write; оператору — обзор абонентов и никакой записи.
    Эскалация доступна всем.
    """
    role = str(claims.get("role", "")).lower()
    scope = claims.get("scope", "")
    scopes = scope.split() if isinstance(scope, str) else list(scope or [])

    read_ops = list(READ_OPS)
    write_ops: list[str] = []
    if role == "client" and WRITE_SCOPE in scopes:
        write_ops = list(WRITE_OPS)
    elif role == "operator":
        read_ops += list(OPERATOR_OPS)
    # у неизвестной роли остаётся только базовое чтение

    return Allowlist(
        sub=str(claims.get("sub", "")),
        role=role,
        scopes=scopes,
        read_ops=read_ops + list(ESCALATION_OPS),
        write_ops=write_ops,
    )


def visible_tools(allowlist: Allowlist, names) -> list[str]:
    """Барьер 1: модель видит только разрешённое; экзекутор — никому."""
    allowed = allowlist.operations()
    return [n for n in names if n in allowed and n not in EXECUTION_OPS]


def admit(allowlist: Allowlist, operation: str, args: dict | None = None) -> Decision:
    """Барьер 2: допускать ли этот вызов этому субъекту с этими аргументами."""
    if operation in EXECUTION_OPS:
        return Decision(False, f"operation_not_allowed:{operation}")
    if operation not in allowlist.operations():
        return Decision(False, f"operation_not_allowed:{operation}")
    for key in (args or {}):
        if key in IDENTITY_ARGS:
            return Decision(False, f"object_from_arguments:{key}")
    return Decision(True, "")
