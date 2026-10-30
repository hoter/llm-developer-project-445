"""Action-MCP: инструменты действий.

    uvicorn actions.server:app --host 127.0.0.1 --port 8220

TODO(контракты): один MCPServer (пакет mcp, пока БЕЗ auth) с инструментами:
  prepare_tariff_change(sub, tariff) / prepare_toggle_service(sub,
  service, enabled) / prepare_contacts_update(sub, phone, email) —
  для LLM; execute_action(sub, action, details_json, pending_id,
  idempotency_key) — НЕ для LLM, вызывает код оркестратора;
  create_ticket(sub, subject, note) — эскалация.
  ActionError → MCPError(code=-32002, message=code, data=hints) —
  подсказки доедут до модели. sub на этом шаге приходит аргументом,
  но подставляет его обёртка оркестратора — модель его не видит.
  Запуск: Starlette + Mount("/actions", streamable_http_app()) +
  lifespan (session manager живёт в event loop uvicorn).

TODO(права): перевод сервера под JWT —
  make_mcp_server("actions", resource=RESOURCES["actions"]) из
  core.server_auth (OAuth 2.1 resource server: Bearer в каждом вызове,
  проверка подписи/iss/aud); убрать sub из всех контрактов — субъект из
  get_access_token().subject (mcp.server.auth.middleware.auth_context).
  До этого шага аргумент sub — осознанная дыра; попытка протащить чужой
  sub станет тестом IDOR.
"""
from __future__ import annotations
