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

Аудит: на каждый ход заводится trace_id; исходы пути действия пишутся в
audit_log (executed с before/after, duplicate_suppressed, rejected_by_user,
rejected_by_contract, server_limit, denied, escalated, error), запуски
агентов — в llm_calls (app.llm), вызовы инструментов моделью — в
tool_calls (app.wrap). Хронология хода: app.audit.timeline(trace_id).

TODO(права) — права: «да» → PEP: pep.confirm → pep.execute; при Deny —
  человекочитаемый отказ по reason (session_limit, confirmation_expired,
  card_not_prepared, pdp_unavailable).

TODO(guardrail) — guardrail: input-проверка реплики до всего хода (unsafe →
  отказ, запись в аудит); output-проверка ответа и карточки до показа;
  недоступный сервис → session.guard_state["ok"] = False (fail-closed
  для действий), чтение продолжается.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable

from app import audit, cards, editor, idempotency, planner, workers
from app import guardrail_client
from app.llm import run_agent
from app.sessions import pep_of
from core import config

Emit = Callable[[dict], Awaitable[None]]

UNSAFE_TEXT = ("Извините, но этот запрос похож на попытку вмешаться в работу ассистента, "
               "и я не могу его обработать. Задайте вопрос об услугах, тарифе или списаниях — "
               "или напишите оператору.")
GUARD_DOWN_TEXT = ("Сейчас не работает проверка безопасности текста, поэтому изменить данные "
                   "нельзя — могу позвать оператора, а на вопросы о тарифе и списаниях "
                   "по-прежнему отвечу.")


async def _output_check(session, text: str, what: str):
    """guardrail выход: ответ и карточка проверяются до показа."""
    verdict = await guardrail_client.check(text, mode="output")
    if not verdict.available:
        session.guard_state = {"ok": False, "category": "unavailable"}
        audit.append(_subject(session), what, "guardrail_degraded",
                     trace_id=session.trace_id, session_id=session.session_id,
                     detail=f"сервис проверки текста недоступен при проверке {what}")
        return guardrail_client.Verdict(safe=True, category="unavailable", available=False)
    if not verdict.safe:
        session.guard_state = {"ok": False, "category": verdict.category}
        audit.append(_subject(session), what, "guardrail_unsafe",
                     trace_id=session.trace_id, session_id=session.session_id,
                     detail=f"output:{verdict.category}")
    return verdict


def _card_text(card: cards.Card) -> str:
    """Текст карточки для проверки выходом: заголовок и значения."""
    parts = [str(v) for v in card.view.values() if not isinstance(v, (dict, list))]
    return " ".join([card.action, *parts, json.dumps(card.details, ensure_ascii=False)])


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
    # маркер конца потока (done) добавляет HTTP-граница, не сам ход:
    # коду и smoke он не нужен
    await _turn(session, message, emit)


async def _turn(session, message: str, emit: Emit) -> None:
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    # guardrail вход: реплика проверяется до LLM. Unsafe — вежливый отказ,
    # ход не начинается (запись уже есть в аудите пакетом ниже).
    verdict = await guardrail_client.check(message, mode="input")
    if not verdict.available:
        session.guard_state = {"ok": False, "category": "unavailable"}
        audit.append(_subject(session), "guardrail", "guardrail_degraded",
                     trace_id=session.trace_id, session_id=session.session_id,
                     detail="сервис проверки текста недоступен: действия закрыты, чтение работает")
    else:
        session.guard_state = {"ok": True, "category": verdict.category}
    if not verdict.safe:
        audit.append(_subject(session), "message", "guardrail_unsafe",
                     trace_id=session.trace_id, session_id=session.session_id,
                     detail=f"input:{verdict.category}")
        await emit({"type": "reply", "text": UNSAFE_TEXT, "sources": []})
        return

    # проверка текста не прошла (сервис недоступен): действия закрыты
    # политикой, чтение работает; на «да»/«нет» по карточке — честный отказ
    if not session.guard_state.get("ok", True):
        card = cards.active(session)
        if card is not None and _wants_card(message) and \
                cards.classify_answer(message) in ("yes", "no"):
            await emit({"type": "reply", "text": GUARD_DOWN_TEXT, "sources": []})
            return

    # живая карточка: «да», «нет» и «оператор» обслуживает код, не LLM
    card = cards.active(session)
    if card is None and cards.classify_answer(message) == "yes" and len(message.split()) <= 3:
        # повтор «да» по уже подтверждённому действию: карточка закрыта,
        # но ключ жив — исполнение проверит идемпотентность и ответит дублем
        previous = last_card(session)
        if previous is not None:
            text = await execute_pending(session, previous)
            await emit({"type": "reply", "text": text, "sources": []})
            return
        await emit({"type": "reply",
                    "text": "Подтверждать нечего: действие не подготовлено. "
                            "Напишите, что изменить, — покажу карточку.", "sources": []})
        return
    if card is not None and _wants_card(message):
        answer = cards.classify_answer(message)
        if answer == "yes":
            text = await execute_pending(session, card)
            await emit({"type": "reply", "text": text, "sources": []})
            return
        if answer == "no":
            cards.settle(session, card, "rejected")
            audit.append(_subject(session), card.action, "rejected_by_user",
                         trace_id=session.trace_id, session_id=session.session_id,
                         args=card.details, idempotency_key=f"{card.action}:{card.pending_id}",
                         detail=f"карточка {card.pending_id} отменена пользователем")
            await emit({"type": "reply", "text": "Хорошо, отменяю. Ничего не менял.", "sources": []})
            return
        if answer == "operator":
            await emit({"type": "reply", "text": await escalate(session, message, card),
                        "sources": []})
            return
        # смена темы: обычный ход, карточка остаётся живой
        if cards.active(session) is not card:
            # карточку уже обслужил код в этом ответе — второй ответ не нужен
            await emit({"type": "reply", "text": "Готово.", "sources": []})
            return

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

    # агент подготовил карточку — ход завершается ожиданием «да/нет»
    fresh = fresh_card(session, card)
    if fresh is not None:
        # guardrail выход: карточку проверяем до показа; unsafe — уничтожаем
        card_verdict = await _output_check(session, _card_text(fresh), "card")
        if not card_verdict.safe:
            cards.settle(session, fresh, "denied")
            await emit({"type": "reply", "text": UNSAFE_TEXT, "sources": []})
            return
        audit.append(_subject(session), fresh.action, "awaiting_confirmation",
                     trace_id=session.trace_id, session_id=session.session_id,
                     args=fresh.details,
                     detail=f"подготовлена карточка {fresh.pending_id}, ждём «да»")
        await emit({"type": "card", "pending_id": fresh.pending_id,
                    "card": fresh.public_view(config.CARD_TTL_SEC)})
        return

    # агент эскалации создал обращение — в журнал, даже без карточки
    for run in runs:
        if run.get("agent") != "tickets":
            continue
        for call in run.get("tools", []):
            output = call.get("output") or {}
            if call.get("tool") == "create_ticket" and output.get("ticket_id"):
                audit.append(_subject(session), "create_ticket", "escalated",
                             trace_id=session.trace_id, session_id=session.session_id,
                             args={"via": "tickets"}, after={"ticket_id": output["ticket_id"]},
                             idempotency_key=f"ticket:{output['ticket_id']}")

    # агент не подготовил карточку: говорим исход прямо, а не доклад агента
    blocked = refused(runs)
    if blocked is not None:
        audit.append(_subject(session), "action_prepare", "rejected_by_contract",
                     trace_id=session.trace_id, session_id=session.session_id,
                     detail=blocked[:200])
        await emit({"type": "reply", "text": blocked, "sources": []})
        return

    # агент звал prepare повторно (карточка уже готова), но так и не написал
    # ответ: карточку всё равно показываем — пересказ модели здесь не нужен
    if fresh is None and session.pending_card is not None and any(
            call.get("tool") in ("prepare_tariff_change", "prepare_toggle_service",
                                 "prepare_contacts_update")
            for run in runs for call in run.get("tools", [])):
        fresh = session.pending_card
        await emit({"type": "card", "pending_id": fresh.pending_id,
                    "card": fresh.public_view(config.CARD_TTL_SEC)})
        return

    edited = await run_agent(editor.build_editor(session), editor.editor_task(message, runs),
                             trace_id=session.trace_id)
    text = editor.pick_reply(edited, _sources(runs))
    verdict = await _output_check(session, text, "reply")
    if not verdict.safe:
        text = UNSAFE_TEXT
    await emit({"type": "reply", "text": text, "sources": _sources(runs)})


def refused(runs: list[dict]) -> str | None:
    """Короткий ответ на отказ business-rule: модель не должна «исполнять» его.

    Ошибки валидации (unknown_tariff и т. п.) сюда не попадают: их агент
    пересказывает с вариантами, а исполнять там нечего.
    """
    messages = {
        "service_already_on": "Услуга уже включена — ничего не менял.",
        "service_already_off": "Услуга уже выключена — ничего не менял.",
        "same_tariff": "Такой тариф уже подключён — ничего не менял.",
        "tariff_not_available": "Этот тариф архивный: подключить его нельзя. "
                                "Выберите из доступных.",
        "same_contacts": "Такие контакты уже стоят в карточке — ничего не менял.",
    }
    for run in runs:
        for call in run.get("tools", []):
            output = call.get("output") if isinstance(call.get("output"), dict) else {}
            error = output.get("error")
            if error in messages:
                return messages[error]
        # prepare_* прошёл проверки, но политика прав отказала подготовке
        if run.get("agent") == "tariff" and not any(
                call.get("tool", "").startswith("prepare_") and
                (call.get("output") or {}).get("card_created") for call in run.get("tools", [])):
            for call in run.get("tools", []):
                output = call.get("output") or {}
                if output.get("error") == "denied":
                    return _deny_text(output.get("reason") or "")
    return None


def _wants_card(message: str) -> bool:
    """Короткая реплика отвечает на карточку; длинная — новая просьба.

    «да», «нет», «позови оператора» — ответ карточке; «какой у меня тариф?
    И ещё поменяй…» — смена темы, карточка остаётся живой. «нет, переведи
    на игровой» — новая просьба, а не отказ.
    """
    if len(message.split()) <= 5:
        return True
    words = set(message.lower().replace("ё", "е").split())
    return bool(words & {"да", "нет", "оператор", "подтверждаю", "отмена", "отменить"})


def last_card(session) -> cards.Card | None:
    """Последняя карточка сессии, исполнение которой уже подтверждено.

    «Да» по ней — повтор: исполнение ведёт idempotency (ключ тот же) и
    отвечает «уже исполнена», а не запускает действие заново.
    """
    return getattr(session, "confirmed_card", None)


def fresh_card(session, before) -> cards.Card | None:
    """Карточка, подготовленная этим ходом (не та, что была до него)."""
    card = cards.active(session)
    if card is None:
        return None
    if before is not None and card.pending_id == before.pending_id:
        return None
    return card


def _outcome_of(session, card, stored) -> str:
    """Исход исполнения с учётом того, что эффект уже сохранён в idempotency.

    Биллинг может вернуть executed с ключом, который уже записан у нас
    (тот же pending_id после reset), а CRM отвечает toggled/updated.
    Значение живёт в outcome, а результат-успех подтверждаем по состоянию.
    """
    result = stored.result
    outcome = result.get("outcome")
    if outcome in ("toggled", "updated", "executed"):
        return "executed"
    if outcome == "duplicate" or stored.duplicate:
        before, after = result.get("before"), result.get("after")
        if before is not None and after is not None and before != after:
            return "executed"
        return "duplicate"
    return outcome or "error"


async def execute_pending(session, card: cards.Card) -> str:
    """«Да» по карточке: PEP → идемпотентное исполнение → человекочитаемый исход.

    Ключ f"{action}:{pending_id}" — одно «да» — одна операция; повтор
    получает сохранённый результат. Неудачный исход ключ не занимает.
    Исполнение пишется в trace_id того хода, в котором пришло «да»; ход
    подготовки связан с ним session_id и idempotency_key.
    """
    # PEP: «да» → ConfirmCard → Execute. Решает политика; Deny (включая
    # недоступный PDP) закрывает карточку и не доходит до исполнения.
    # Своей трассировки у исполнения нет: оно принадлежит ходу, в котором
    # пришло «да» (вызванному кодом или кнопкой карточки), и разбор по
    # trace_id видит сразу и политику, и executed с before/after.
    pep = pep_of(session)
    confirm = await pep.confirm(card)
    if confirm.allowed:
        execute = await pep.execute(card)
    else:
        execute = confirm
    if not execute.allowed:
        await _deny_card(session, card, execute.reason)
        return _deny_text(execute.reason)
    # политика проверяет guard_ok по тому, что PEP положил в input; проверка
    # исполнения не должна зависеть от порядка шагов в контуре: если текстовый
    # фильтр не прошёл, у политики спрашиваем Execute с guard_ok=false —
    # решение всё равно её (guard_not_ok), а не оркестратора
    if not session.guard_state.get("ok", True):
        execute = await pep.execute(card, guard_ok=False)
        if not execute.allowed:
            await _deny_card(session, card, execute.reason)
            return _deny_text(execute.reason)

    before = await _state_of(session, card)
    key = f"{card.action}:{card.pending_id}"

    async def run() -> dict:
        return await call_action(session, "execute_action", {
            "action": card.action,
            "details_json": json.dumps(card.details, ensure_ascii=False),
            "pending_id": card.pending_id,
            "idempotency_key": key,
        })

    try:
        stored = await idempotency.execute(key, _subject(session), card.action, run)
    except Exception as e:  # noqa: BLE001 — сбой исполнения: карточка живёт, ключ не занят
        audit.append(_subject(session), card.action, "error", trace_id=session.trace_id,
                     session_id=session.session_id, args=card.details,
                     idempotency_key=key, detail=str(e)[:200])
        return "Не удалось исполнить действие, попробуйте ответить «да» ещё раз."

    result = stored.result
    after = await _state_of(session, card)
    outcome = _outcome_of(session, card, stored)
    if stored.duplicate or outcome == "duplicate":
        cards.settle(session, card, "confirmed")
        session.confirmed_card = card
        audit.append(_subject(session), card.action, "duplicate_suppressed",
                     trace_id=session.trace_id, session_id=session.session_id,
                     args=card.details, before=before, after=after,
                     idempotency_key=key, detail="карточка уже исполнена")
        return "Эта карточка уже исполнена — второй раз ничего не меняю."

    if outcome == "executed":
        cards.settle(session, card, "confirmed")
        session.confirmed_card = card
        audit.append(_subject(session), card.action, "executed",
                     trace_id=session.trace_id, session_id=session.session_id,
                     args=card.details, before=before, after=after, idempotency_key=key)
        return result.get("message") or "Готово."

    if outcome == "daily_limit":
        cards.settle(session, card, "denied")
        audit.append(_subject(session), card.action, "server_limit", trace_id=session.trace_id,
                     session_id=session.session_id, args=card.details,
                     idempotency_key=key, detail=str(result)[:200])
        limit = result.get("limit", 3)
        return (f"Пока не получилось: дневной лимит смен тарифа ({limit} за сутки) исчерпан. "
                "Попробуйте завтра или напишите оператору.")

    # прочий отказ бизнес-правила: карточка закрыта, ключ не занят
    cards.settle(session, card, "denied")
    audit.append(_subject(session), card.action, "rejected_by_contract", trace_id=session.trace_id,
                 session_id=session.session_id, args=card.details,
                 idempotency_key=key, detail=str(result)[:200])
    return result.get("message") or "Действие отклонено."


async def _state_of(session, card: cards.Card) -> str | None:
    """Снимок изменяемого значения для журнала: тариф, услуга или контакты."""
    try:
        record = await session.pool.call("crm", "get_my_profile", {})
    except Exception:  # noqa: BLE001 — журнал не должен ронять исполнение
        return None
    if card.action == "tariff_change":
        return record.get("tariff")
    if card.action == "service_toggle":
        name = card.details.get("service")
        row = next((s for s in record.get("services", []) if s["service"] == name), None)
        return f"{name}: {'вкл' if row and row['enabled'] else 'выкл'}"
    if card.action == "contacts_update":
        return f"{record.get('phone')} / {record.get('email')}"
    return None


def _subject(session) -> str:
    """Кто действует: субъект из проверенных claims токена сессии."""
    allowlist = getattr(session, "allowlist", None)
    return allowlist.sub if allowlist is not None and allowlist.sub else session.user.sub


async def call_action(session, tool: str, args: dict) -> dict:
    """Вызов action-MCP из кода.

    Субъекта в аргументах нет: сервер берёт его из Bearer-токена
    соединения, поэтому подставить чужой sub в вызов невозможно.
    """
    return await session.pool.call("actions", tool, dict(args))


async def _deny_card(session, card: cards.Card, reason: str) -> None:
    cards.settle(session, card, "denied")
    audit.append(_subject(session), card.action, "denied", trace_id=session.trace_id,
                 session_id=session.session_id, args=card.details, detail=reason)


def _deny_text(reason: str) -> str:
    texts = {
        "confirmation_expired": "Карточка устарела — повторите запрос, и я подготовлю её заново.",
        "session_limit": ("Пока не получилось: в этой сессии уже две смены тарифа, "
                          "а больше политика (лимит сессии) не разрешает. Напишите оператору, "
                          "если нужно поменять ещё раз."),
        "confirmation_missing": "Действие не было подтверждено — подготовьте карточку заново.",
        "card_not_prepared": "Карточка не найдена или истекла — повторите запрос.",
        "pdp_unavailable": "Сервис прав недоступен: действия сейчас закрыты, попробуйте позже.",
        "guard_not_ok": GUARD_DOWN_TEXT,
        "no_write_scope": "Изменения тарифа недоступны: у аккаунта нет права записи.",
        "role_has_no_write": "У вашей роли нет прав на изменения — обратитесь к оператору.",
    }
    return texts.get(reason, "Действие запрещено: проверка прав не пройдена.")


async def escalate(session, message: str, card: cards.Card | None = None) -> str:
    """Эскалация: обращение в поддержку без карточки (риск низкий, но в аудит)."""
    if card is not None:
        cards.settle(session, card, "rejected")
        audit.append(_subject(session), card.action, "rejected_by_user",
                     trace_id=session.trace_id, session_id=session.session_id,
                     args=card.details, detail="заменена обращением к оператору")
    subject = "Обращение из чата"
    note = message[:500]
    try:
        result = await call_action(session, "create_ticket", {
            "subject": subject, "note": note,
        })
    except Exception as e:  # noqa: BLE001 — сбой эскалации не роняет ход
        audit.append(_subject(session), "create_ticket", "error", trace_id=session.trace_id,
                     session_id=session.session_id, detail=str(e)[:200])
        return "Не удалось создать обращение. Попробуйте позже."
    ticket = result.get("ticket_id", "")
    # в журнал — факт обращения и номер, не текст реплики абонента
    audit.append(_subject(session), "create_ticket", "escalated", trace_id=session.trace_id,
                 session_id=session.session_id,
                 args={"subject": subject}, after={"ticket_id": ticket},
                 idempotency_key=f"ticket:{ticket}")
    return (f"Передал ваш вопрос оператору поддержки, обращение {ticket}. "
            "Ответ придёт на почту из карточки.")
