"""Клиент guardrail-сервиса: verdikt вероятностный, решения — за кодом.

TODO(guardrail): Verdict(safe, category, available) и check(text, mode):
  POST {GUARDRAIL_URL}/check {"text", "mode": "input"|"output"}.
  Недоступность сервиса — НЕ исключение: Verdict(safe=True,
  available=False) — чтение продолжается, действия блокируются флагом
  guard_ok в политике (fail-closed).
"""
from __future__ import annotations
