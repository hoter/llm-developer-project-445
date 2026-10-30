"""Backend + оркестратор: HTTP-граница чата.

    uvicorn app.main:app --host 127.0.0.1 --port 8000

  POST /api/session                      {"user": "anna"} → открыть сессию
  POST /api/chat                         {"session_id", "message"} → SSE-поток хода
  POST /api/cards/{pending_id}/decision  подтверждение карточки (с контрактов)

События SSE хода: plan / card / reply / error / done.

TODO(контракты): POST /api/cards/{pending_id}/decision — кнопка карточки:
  «да» → orchestrator.execute_pending, «нет» → отмена.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request

from app import audit, cards, orchestrator, permissions, planner
from app.sessions import get, open_session
from core import config


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.setup_model()
    yield


app = FastAPI(title="AI-оператор мобильного оператора", lifespan=lifespan)

# фронтенд живёт на другом порту стенда (:8100)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — кривое тело → пустой объект
        return {}
    return body if isinstance(body, dict) else {}


def _visible_tools(session) -> list[str]:
    """Барьер 1 как его видит фронтенд: только разрешённые субъекту имена."""
    return permissions.visible_tools(session.allowlist, session.pool.tool_names())


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "api"})


@app.post("/api/session")
async def create_session(request: Request) -> JSONResponse:
    body = await _json_body(request)
    login = str(body.get("user", "")).strip()
    try:
        session = await open_session(login)
    except KeyError:
        return JSONResponse({"error": "unknown_user", "detail": login}, status_code=400)
    except Exception as e:  # noqa: BLE001 — стенд недоступен: сессия не открывается
        return JSONResponse({"error": "stand_unavailable", "detail": str(e)[:160]}, status_code=503)

    if session.planner is None:
        # планировщик собирается на первом ходе: сборка локальная, без модели
        session.planner = planner.build_planner()

    return JSONResponse({
        "session_id": session.session_id,
        "name": session.user.name,
        "role": session.user.role,
        "visible_tools": _visible_tools(session),
    })


@app.post("/api/cards/{pending_id}/decision")
async def card_decision(pending_id: str, request: Request) -> JSONResponse:
    """Кнопка карточки: «да» → исполнение, «нет» → отмена (решение принимает код)."""
    body = await _json_body(request)
    answer = str(body.get("answer", "")).strip().lower()
    session = get(str(body.get("session_id", "")))
    if session is None:
        return JSONResponse({"error": "session_not_found"}, status_code=404)
    card = cards.active(session)
    if card is None or card.pending_id != pending_id:
        return JSONResponse({"error": "card_not_found", "detail": "карточка истекла или заменена"},
                            status_code=404)

    if answer in ("no", "cancel", "отмена"):
        cards.settle(session, card, "rejected")
        audit.append(session.user.sub, card.action, "rejected_by_user",
                     session_id=session.session_id, args=card.details)
        return JSONResponse({"outcome": "rejected", "text": "Хорошо, отменяю. Ничего не менял."})
    if answer not in ("yes", "да"):
        return JSONResponse({"error": "bad_answer", "detail": "answer: yes | no"}, status_code=400)

    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"
    text = await orchestrator.execute_pending(session, card)
    return JSONResponse({"text": text})


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await _json_body(request)
    session_id = str(body.get("session_id", ""))
    message = str(body.get("message", "")).strip()

    async def stream():
        try:
            session = get(session_id)
            if session is None:
                yield _sse({"type": "error", "detail": "сессия не найдена: откройте чат заново"})
                yield _sse({"type": "done"})
                return
            if not message:
                yield _sse({"type": "error", "detail": "пустое сообщение"})
                yield _sse({"type": "done"})
                return

            queue: asyncio.Queue[dict | None] = asyncio.Queue()

            async def emit(event: dict) -> None:
                await queue.put(event)

            async def work() -> None:
                try:
                    await orchestrator.run_turn(session, message, emit)
                except Exception as e:  # noqa: BLE001 — сбой хода — событие error
                    await queue.put({"type": "error", "detail": repr(e)[:200]})
                finally:
                    await queue.put(None)  # конец потока

            task = asyncio.create_task(work())
            try:
                while (event := await queue.get()) is not None:
                    yield _sse(event)
            finally:
                if not task.done():
                    task.cancel()
        except Exception as e:  # noqa: BLE001 — сбой хода не роняет поток
            yield _sse({"type": "error", "detail": repr(e)[:200]})
        yield _sse({"type": "done"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
