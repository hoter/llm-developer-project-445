"""Логика действий поверх REST-моков: предусловия, карточки, исполнение.

TODO(контракты): prepare_* ничего не исполняют — проверяют предусловия и
  собирают view для карточки («с какого на какой, что изменится»):

  prepare_tariff_change(sub, tariff_raw):
    текущий тариф → GET /mock/crm/subscriber?sub=...; каталог →
    GET /mock/crm/tariffs; normalize_tariff; available? same_tariff?
    → {"action": "tariff_change", "details": {"tariff": ...},
       "view": {...}}.
  prepare_toggle_service(sub, service, enabled): unknown_service /
    service_already_on / service_already_off.
  prepare_contacts_update(sub, phone, email): нормализация,
    nothing_to_update / same_contacts.

  execute_action(sub, action, details_json, idempotency_key) —
  ИСПОЛНЕНИЕ; вызывает только код оркестратора после «да»:
    tariff_change → POST /mock/billing/tariff-change (идемпотентность и
      дневной лимит держит сам биллинг: executed/duplicate/daily_limit);
    service_toggle → POST /mock/crm/toggle-service;
    contacts_update → POST /mock/crm/contacts.
  create_ticket(sub, subject, note) → POST /mock/tickets/create.

Все запросы — через httpx с trust_env=False. Субъект sub приходит
аргументом до прав (осознанная дыра): права её закроют.
"""
from __future__ import annotations

SERVICE_CATALOG = ["Музыка", "Роуминг", "Антивирус"]
