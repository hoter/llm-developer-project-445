"""Аудит: append-only журнал в Postgres — основа разбора инцидентов.

TODO(аудит):
  1. OUTCOMES — фиксированная таксономия исходов: executed,
     duplicate_suppressed, rejected_by_user, rejected_by_contract, expired,
     escalated, denied, guardrail_unsafe, guardrail_degraded,
     token_refreshed, server_limit, budget_exhausted, error.
  2. append(who, what, outcome, *, trace_id=None, session_id=None, args=None,
     before=None, after=None, idempotency_key=None, detail=None) — INSERT в
     audit_log. Журнал не редактируется: только append и чтение.
  3. tail(n=20, who=None) и trace(trace_id) — чтение для разбора инцидентов.

В журнал попадают и неуспешные попытки: по нему видно «кто пытался и что».
Не логируйте текст реплик целиком — журнал про действия, не про переписку.
"""
from __future__ import annotations
