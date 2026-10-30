"""Планировщик: план запусков доменных агентов до всякого исполнения.

План — структурированный выход LLM, но исполнению он не принадлежит:
гейт оркестратора оставляет только шаги известных агентов. Действия
(смена тарифа, услуги, контакты, тикет) идут через карточку подтверждения
и политику прав, а не напрямую.
"""
from __future__ import annotations

import json

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

READ_AGENTS = ("data", "kb", "support")
ACTION_AGENTS = ("tariff", "tickets")


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent: str = Field(validation_alias=AliasChoices("agent", "agentName"))
    task: str = Field(default="", validation_alias=AliasChoices("task", "job"))
    why: str = ""
    action: bool = False


class Plan(BaseModel):
    steps: list[Step] = []
    question: str | None = None
    error: str | None = Field(default=None, exclude=True)


def _plan_complete(plan: Plan) -> bool:
    """План целый: шаг без задачи — признак обрезанного JSON."""
    return bool(plan.steps) or bool(plan.question)


def parse_plan(text: str) -> Plan:
    """Текст модели → Plan: первый целый JSON-объект из ответа.

    Малая модель может «доиграть» после JSON веб-страницу, поэтому
    разбираем только объект (raw_decode сам определяет его конец).
    """
    from json import JSONDecoder

    start = text.find("{")
    if start < 0:
        raise ValueError("в ответе планировщика нет JSON")
    obj, _ = JSONDecoder().raw_decode(text[start:])
    plan = Plan.model_validate(obj)
    if not _plan_complete(plan):
        raise ValueError("план без шагов и без вопроса")
    return plan


PLANNER_PROMPT = """\
Ты — планировщик ассистента мобильного оператора. Ты не отвечаешь абоненту
и не вызываешь инструменты: ты составляешь план, каких специалистов запустить.

Каталог агентов:
- data — данные абонента: его тариф, каталог тарифов, списания и их суммы.
- kb — база знаний оператора: правила списаний, правила смены тарифа,
  условия услуг, роуминг, контакты.
- tariff — изменения тарифа, услуг и контактов абонента. Шаг с action=true.
  Агент только готовит карточку подтверждения, исполняет человек.
- tickets — обращение к оператору поддержки, когда вопрос вне тем
  оператора. Шаг с action=true.
- support — карточка абонента для оператора поддержки (только у роли
  operator): «покажи карточку», «что у абонента X». Шаг с action=false.

Правила:
1. Вопрос о данных абонента (свой тариф, свои списания, их суммы) — шаг data.
2. Вопрос о правилах, причинах и последствиях, «почему списали X» — шаг kb.
3. Вопросы «что у меня» И «почему/как» сразу — два шага: data и kb.
   Если абонент спрашивает о последствиях смены тарифа («если я перейду
   на X, когда спишется…») — шаги tariff и kb: kb объясняет правила.
4. action=true ставь только шагам tariff и tickets. У агентов data и kb
   действий нет: action=false или не указывай вовсе.
5. Название тарифа в задаче пиши строчными буквами без кавычек, например:
   tariff, task: «сменить тариф на семейный».
6. Если вопрос вне тем оператора (погода, новости, общие вопросы) — шаг
   tickets с action=true.
7. Оператор просит показать карточку другого абонента — шаг support.
8. Просьба изменить тариф, услугу или контакты («переведи меня на…»,
   «подключи», «отключи», «поменяй номер») — шаг tariff с action=true.
   Не отправляй такую просьбу в kb: агент tariff сам покажет правила
   и карточку подтверждения.
9. Если не понятно, какое действие нужно, верни пустой steps и заполни
   question — уточняющий вопрос абоненту.

Пример. Абонент: «какой у меня тариф и почему списание 440?»
Ответ: {"steps": [{"agent": "data", "task": "узнать тариф абонента и его списания",
"why": "данные абонента", "action": false}, {"agent": "kb", "task": "причины
списания 440", "why": "правила биллинга", "action": false}], "question": null}

Пример. Абонент: «переведи меня на семейный»
Ответ: {"steps": [{"agent": "tariff", "task": "сменить тариф на семейный",
"why": "просьба об изменении", "action": true}], "question": null}
"""


def build_planner():
    """Агент-планировщик со структурированным выходом Plan."""
    from app.llm import build_agent

    return build_agent("planner", PLANNER_PROMPT, output_type=Plan)


def gate(plan: Plan) -> tuple[Plan, list[dict]]:
    """Гейт плана: код решает, что из плана модели доходит до исполнения.

    Неизвестные агенты отбрасываются; у агентов чтения пометка action
    снимается — в контуре чтения действий нет по построению. Возвращает
    отфильтрованный план и список отброшенных шагов (для трейса сессии).
    """
    kept: list[Step] = []
    dropped: list[dict] = []
    for step in plan.steps:
        agent = step.agent.strip().lower()
        if agent not in READ_AGENTS + ACTION_AGENTS:
            dropped.append({"agent": step.agent, "task": step.task, "reason": "unknown_agent"})
            continue
        action = bool(step.action) and agent in ACTION_AGENTS
        if step.action and not action:
            dropped.append({"agent": agent, "task": step.task, "reason": "action_in_read_agent"})
        if not step.task:
            step.task = step.why or f"ответить на вопрос абонента ({agent})"
        kept.append(Step(agent=agent, task=step.task, why=step.why, action=action))

    # один агент — один шаг: задачи склеиваются, порядок первого появления
    merged: dict[str, Step] = {}
    for step in kept:
        prev = merged.get(step.agent)
        if prev is None:
            merged[step.agent] = step
        elif step.task and step.task not in prev.task:
            prev.task = f"{prev.task}; {step.task}"
            prev.action = prev.action or step.action
    return Plan(steps=list(merged.values()), question=plan.question), dropped


def history_message(message: str) -> dict:
    """Реплика абонента в истории планировщика."""
    return {"role": "user", "content": message}


def plan_message(plan: Plan) -> dict:
    """Сводка плана в истории: следующий ход знает, что уже делали."""
    if not plan.steps:
        return {"role": "assistant", "content": "план: шагов нет"}
    agents = ", ".join(f"{s.agent}{' (действие)' if s.action else ''}" for s in plan.steps)
    return {"role": "assistant", "content": f"план: {agents}"}


def _json_block(text: str) -> str:
    """JSON-объект из ответа модели: без ```-обвязки и текста вокруг."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


async def next_plan(planner, history: list, message: str) -> tuple[Plan, list]:
    """Один вызов планировщика по истории диалога → (Plan, история хода)."""
    from agents import Runner

    turn = [*history, history_message(message)]
    plan = None
    error = None
    try:
        plan = await _plan_once(planner, turn)
    except Exception as e:  # noqa: BLE001 — кривой план модели не роняет ход
        error = str(e)[:120]
    if plan is None:
        # второй шанс: без JSON-режима сервера модель учится по инструкции
        plain = _plain_variant(planner)
        if plain is not None:
            try:
                plan = await _plan_once(plain, turn)
            except Exception as e:  # noqa: BLE001
                error = f"{error}; retry: {str(e)[:80]}" if error else str(e)[:120]
    if plan is None:
        # редактор ответит сам, без докладов агентов
        plan = Plan(question=None, steps=[])
        plan.error = f"planner_error: {error}"
    new_history = [*history, history_message(message), plan_message(plan)]
    return plan, new_history


async def _plan_once(planner, turn: list) -> Plan:
    from agents import Runner

    result = await Runner.run(planner, turn, max_turns=4)
    plan = result.final_output
    if isinstance(plan, str):
        # сервер без response_format: SDK отдаёт текст, JSON достаём сами
        plan = parse_plan(_json_block(plan))
    elif not isinstance(plan, Plan):
        plan = Plan.model_validate(plan)
    _check_plan(plan)
    return plan


def _check_plan(plan: Plan) -> None:
    """План без шагов и без вопроса — признак сбоя модели: новый шанс."""
    if not plan.steps and not plan.question:
        raise ValueError("пустой план")


def _plain_variant(planner):
    """Копия планировщика без JSON-режима сервера (схема — в инструкции)."""
    try:
        from agents import Agent, Runner  # noqa: F401

        output = getattr(planner, "output_type", None)
        output_type = getattr(output, "output_type", None)
        if output_type is None:
            return None
        from app.llm import build_agent, json_contract
        from app.llm import SchemaInstructionOutput

        instructions = (planner.instructions or "")
        if "Схема JSON:" not in instructions:
            instructions = f"{instructions}\n\n{json_contract(output_type)}"
        variant = build_agent(planner.name, instructions, output_type=output_type)
        variant.output_type = SchemaInstructionOutput(output_type)
        return variant
    except Exception:  # noqa: BLE001 — нет повтора — работаем с первым шансом
        return None


__all__ = [
    "Plan", "Step", "READ_AGENTS", "ACTION_AGENTS", "PLANNER_PROMPT",
    "build_planner", "gate", "next_plan",
]
