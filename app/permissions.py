"""Права сессии: allowlist из claims, два барьера оркестратора.

TODO(права) — ядро шага:
  1. Константы операций:
     READ_OPS = get_my_profile, get_my_tariff, list_tariffs,
                list_my_charges, search_kb
     WRITE_OPS = prepare_tariff_change, prepare_toggle_service,
                 prepare_contacts_update (карточные — исполняет политика)
     OPERATOR_OPS = get_subscriber_overview (только operator; чужой
                    идентификатор легитимен — роль проверит сервер)
     ESCALATION_OPS = create_ticket (позвать человека можно всегда)
     IDENTITY_ARGS = user_id, account, subscriber, msisdn, sub
                    (phone не ключ: это новое значение в prepare_contacts_update)
  2. allowlist_from_claims(claims) → Allowlist(sub, role, scopes,
     read_ops, write_ops): роль даёт базовый набор, scope сужает —
     карточные действия только клиенту с tariffs:write; оператору —
     operator-инструменты и никаких write.
  3. visible_tools(allowlist, names) — барьер 1: модель видит только
     разрешённое; execute_action не виден никому.
  4. admit(allowlist, operation, args) → Decision(allowed, reason) —
     барьер 2: операция вне allowlist — отказ operation_not_allowed:<op>;
     identity-ключи в аргументах — отказ object_from_arguments:<ключ>; лимиты считает политика.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Allowlist:
    sub: str
    role: str
    scopes: list[str] = field(default_factory=list)
    read_ops: list[str] = field(default_factory=list)
    write_ops: list[str] = field(default_factory=list)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
