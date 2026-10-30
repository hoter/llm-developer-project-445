"""Контракты действий: схемы, нормализация, таксономия ошибок.

TODO(контракты):
  1. ActionError(code, message, **hints) — отказ действия:
     to_tool_result() → {"error": code, "detail": message, **hints}.
  2. BUSINESS_RULE_CODES = same_tariff, tariff_not_available,
     service_already_on/off, same_contacts — повторять бессмысленно.
  3. Нормализация «как услышали» → точное значение:
     normalize_tariff(raw, catalog) — unknown_tariff с подсказкой
     available=[...]; normalize_service(raw, catalog) — unknown_service;
     normalize_phone — 11 цифр → bad_phone с примером; normalize_email.
"""
from __future__ import annotations

from pydantic import BaseModel

BUSINESS_RULE_CODES = frozenset()


class ActionError(Exception):
    pass
