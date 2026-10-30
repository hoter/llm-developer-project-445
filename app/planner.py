"""Планировщик: план запусков доменных агентов до всякого исполнения.

План — структурированный выход LLM, но исполнению он не принадлежит:
гейт оркестратора оставляет только шаги известных агентов. Действия
(смена тарифа, услуги, контакты, тикет) идут через карточку подтверждения
и политику прав, а не напрямую.

TODO(агенты):
  1. Схемы Step и Plan (pydantic):
     - Step: agent (алиас agentName), task (алиас job), why, action: bool
       — алиасы нужны: модели отвечают своим диалектом имён;
     - Plan: steps: list[Step], question: str | None (уточняющий вопрос,
       если данных для плана мало).
  2. PLANNER_PROMPT — каталог агентов и правила планирования:
     вопрос о данных абонента — один шаг data; правила и последствия —
     шаг kb; «почему списание» — оба; изменения (смена тарифа, услуги,
     контакты) — шаг tariff с action=true; вне тем оператора — tickets
     с action=true; названия тарифов — строчными без кавычек.
  3. build_planner() — агент со структурированным выходом Plan
     (app.llm.build_agent(..., output_type=Plan)).
  4. gate(plan) — гейт: action-шаги только tariff/tickets, у read-агентов
     action снимается, неизвестные агенты отбрасываются (причина — в трейс).
  5. next_plan(planner, history, message) — один вызов Runner.run
     (через пул llm_pool, если он включён) → (Plan, история хода).
"""
from __future__ import annotations

from pydantic import BaseModel

READ_AGENTS = ("data", "kb")
ACTION_AGENTS = ("tariff", "tickets")


class Step(BaseModel):
    agent: str
    task: str


class Plan(BaseModel):
    steps: list[Step] = []
