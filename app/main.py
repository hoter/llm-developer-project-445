"""Backend + оркестратор: HTTP-граница чата.

    uvicorn app.main:app --host 127.0.0.1 --port 8000

  POST /api/session                      {"user": "anna"} → открыть сессию
  POST /api/chat                         {"session_id", "message"} → SSE-поток хода
  POST /api/cards/{pending_id}/decision  подтверждение карточки (с контрактов)

События SSE хода: plan / card / reply / error / done.

TODO(агенты):
  1. lifespan: подключить LLM (config.setup_model(); в бонусе failover —
     llm_pool.setup_pool(), если задан LLM_SECONDARY_BASE_URL).
  2. CORS: allow_origins=["*"] — фронтенд живёт на другом порту стенда.
  3. GET /api/health.
  4. POST /api/session: sessions.open_session(login); ответ: session_id,
     name, role, visible_tools — инструменты allowlist сессии
     (с правами — permissions.visible_tools(...), до них — весь пул).
  5. POST /api/chat: события хода через asyncio.Queue гонятся
     StreamingResponse'ом (media_type="text/event-stream", формат
     'data: {json}\\n\\n'); сбой хода — событие error, не падение потока.

TODO(контракты): POST /api/cards/{pending_id}/decision — кнопка карточки:
  «да» → orchestrator.execute_pending, «нет» → отмена.
"""
from __future__ import annotations
