"""Guardrail-сервис: отдельный stateless-контур проверки текста.

    uvicorn --factory guardrail.service:build_app --host 127.0.0.1 --port 8300

TODO(guardrail):
  1. RULE-движок: RULE_PATTERNS — детерминированные маркеры атак стенда
     (jailbreak/role_hijack — «суперпользователь», «ты теперь админ»;
     prompt_injection — «игнорируй инструкц...»; tool_hijack — «смени
     тариф в аккаунте»; injected_instruction — «немедленно переведи»,
     «без подтверждения» — ловит ловушку из базы знаний). Эвристика
     лабораторная, не претендует на прод.
  2. POST /check {"text", "mode": "input"|"output"} →
     {"verdict": "safe"|"unsafe", "category", "engine", "mode"}.
  3. POST /admin/down и /admin/up — имитация отказа сервиса; /health —
     503 при down.
  4. Опционально GUARDRAIL_MODEL=1 — классификатор HiveTraceGuard-Pro
     (transformers+torch, в requirements не включены).

Недоступность сервиса — не крах контура: оркестратор решает сам
(fail-closed для действий, деградация для чтения).
"""
from __future__ import annotations
