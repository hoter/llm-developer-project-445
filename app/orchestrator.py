"""Оркестратор хода: план → гейт → доменные агенты → редактор → SSE.

Ход — один запрос абонента; оркестратор управляет запуском агентов и
собирает события хода для фронтенда.

TODO(контракты) — карточка действия:
  - в начале хода: cards.active(session) → cards.classify_answer(message):
    «да» → execute_pending, «нет» → settle("rejected") + «отменяю»,
    «оператор» → escalate, смена темы → обычный ход с живой карточкой;
  - после агентов: свежая карточка → событие {"type": "card"} и ход
    завершается (ждём подтверждение);
  - execute_pending(session, card): settle("confirmed"), ключ
    f"{action}:{pending_id}", idempotency.execute → вызов MCP execute_action
    → человекочитаемый ответ (executed / duplicate / daily_limit / отказ).

TODO(аудит) — аудит: audit.append на все исходы пути действия
  (executed с before/after, duplicate_suppressed, rejected_by_user,
  server_limit, escalated).

TODO(права) — права: «да» → PEP: pep.confirm → pep.execute; при Deny —
  человекочитаемый отказ по reason (session_limit, confirmation_expired,
  card_not_prepared, pdp_unavailable).

TODO(guardrail) — guardrail: input-проверка реплики до всего хода (unsafe →
  отказ, запись в аудит); output-проверка ответа и карточки до показа;
  недоступный сервис → session.guard_state["ok"] = False (fail-closed
  для действий), чтение продолжается.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from app import editor, planner, workers
from app.llm import run_agent

Emit = Callable[[dict], Awaitable[None]]


def _sources(runs: list[dict]) -> list[str]:
    """Источники базы знаний из tool-результатов (search_kb → results[].source)."""
    found: list[str] = []
    for run in runs:
        if run.get("agent") != "kb":
            continue
        for call in run.get("tools", []):
            for source in call.get("sources", []) or []:
                if source not in found:
                    found.append(source)
    return found


def _plan_event(plan, dropped: list[dict]) -> dict:
    event = {
        "type": "plan",
        "agents": [step.agent for step in plan.steps],
        "steps": [{"agent": s.agent, "task": s.task, "action": s.action} for s in plan.steps],
        "dropped": dropped,
    }
    if getattr(plan, "error", None):
        event["error"] = plan.error
    return event


async def run_turn(session, message: str, emit: Emit) -> None:
    """Один ход: план → гейт → агенты → редактор; события уходят через emit."""
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    if session.planner is None:
        session.planner = planner.build_planner()
    plan, session.thread = await planner.next_plan(session.planner, session.thread, message)
    plan, dropped = planner.gate(plan)
    for step in dropped:
        session.note(f"dropped:{step['agent']}:{step['reason']}")

    await emit(_plan_event(plan, dropped))

    # уточняющий вопрос — только когда агентов не выбрали вовсе
    if plan.question and not plan.steps:
        await emit({"type": "reply", "text": plan.question, "sources": []})
        return

    built = workers.build_workers(session, tuple(s.agent for s in plan.steps))
    runs: list[dict] = []
    for step in plan.steps:
        agent = built.get(step.agent)
        if agent is None:
            continue
        result = await run_agent(agent, step.task, trace_id=session.trace_id)
        runs.append({"agent": step.agent, "task": step.task, **result})

    edited = await run_agent(editor.build_editor(session), editor.editor_task(message, runs),
                             trace_id=session.trace_id)
    text = editor.pick_reply(edited, _sources(runs))
    await emit({"type": "reply", "text": text, "sources": _sources(runs)})
