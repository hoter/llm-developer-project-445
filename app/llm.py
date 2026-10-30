"""Сборка агентов на модели из .env + учёт LLM-вызовов.

TODO(аудит): в run_agent — INSERT в llm_calls: trace_id, имя агента,
  модель, токены (result.context_wrapper.usage), латентность.

TODO(failover): если llm_pool.enabled() — запуск через
  llm_pool.run_with_failover, в llm_calls — фактический провайдер.
"""
from __future__ import annotations

import json
import os
import time

from agents import Agent, AgentOutputSchema, ModelSettings, Runner
from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem

from core import config


def build_agent(name: str, instructions: str, tools=None, output_type=None) -> Agent:
    """Агент на модели из .env; output_type — нестрогая схема выхода."""
    model = config.MODEL_NAME or config.setup_model()
    output = None
    if output_type is not None:
        # строгий JSON-режим слишком требователен к малым моделям
        output = AgentOutputSchema(output_type, strict_json_schema=False)
        # не все OpenAI-совместимые серверы принимают response_format с
        # json_schema: по LLM_RESPONSE_FORMAT=json_object схему уводим
        # в инструкцию, а у сервера просим просто JSON-объект
        if _plain_mode():
            instructions = f"{instructions}\n\n{json_contract(output_type)}"
            output = SchemaInstructionOutput(output_type)

    # серверы вроде llama.cpp без явного tool_choice иначе не выдают
    # tool_calls вовсе
    settings = ModelSettings(tool_choice="auto") if tools and _plain_mode() else ModelSettings()

    return Agent(
        name=name,
        instructions=instructions,
        tools=list(tools or []),
        output_type=output,
        model=model,
        model_settings=settings,
    )


def _plain_mode() -> bool:
    """Сервер принимает response_format только с json_object (llama.cpp)."""
    return os.environ.get("LLM_RESPONSE_FORMAT", "auto").lower() == "json_object"


def json_contract(output_type) -> str:
    """Инструкция модели: ответить одним JSON-объектом по схеме."""
    schema = json.dumps(output_type.model_json_schema(), ensure_ascii=False, indent=2)
    return (
        "Ответь одним JSON-объектом без markdown и без текста вокруг.\n"
        f"Схема JSON:\n{schema}"
    )


class SchemaInstructionOutput(AgentOutputSchema):
    """Выход по схеме, когда сервер не умеет response_format с json_schema.

    Схема уже ушла модели инструкцией. SDK-разбор здесь не используется
    (он ждёт JSON целиком): текст отдаётся как есть, а JSON достаёт и
    проверяет вызывающий код — см. app.planner.next_plan.
    """

    def is_plain_text(self) -> bool:
        return True


def _arguments(raw_item) -> dict:
    try:
        args = json.loads(getattr(raw_item, "arguments", "") or "{}")
    except (TypeError, ValueError):
        return {}
    return args if isinstance(args, dict) else {}


def _parse_output(text) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def collect_tool_runs(result) -> list[dict]:
    """tool-вызовы хода: [{"tool", "args", "output", "sources"}, ...]."""
    calls: dict[str, dict] = {}
    runs: list[dict] = []
    for item in result.new_items:
        if isinstance(item, ToolCallItem):
            call = {"tool": getattr(item.raw_item, "name", "?"),
                    "args": _arguments(item.raw_item), "output": {}, "sources": []}
            call_id = getattr(item.raw_item, "call_id", "")
            calls[call_id] = call
            runs.append(call)
        elif isinstance(item, ToolCallOutputItem):
            raw = item.raw_item
            call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", "")
            call = calls.get(call_id)
            if call is not None:
                output = _parse_output(item.output)
                # источники базы знаний — из вызова инструмента, не из доклада
                for key in ("source", "sources", "results", "chunks", "articles"):
                    value = output.get(key) if isinstance(output, dict) else None
                    for source in _sources_of(value):
                        if source not in call["sources"]:
                            call["sources"].append(source)
                call["output"] = output
    return runs


def _sources_of(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _sources_of(item)]
    if isinstance(value, dict):
        found: list[str] = []
        for key in ("source", "source_id", "doc", "file", "id"):
            if isinstance(value.get(key), str):
                found.append(value[key])
        for key in ("results", "chunks", "articles", "items"):
            found += _sources_of(value.get(key))
        return found
    return []


async def run_agent(agent: Agent, task: str, *, trace_id: str = "") -> dict:
    """Отдельный Runner.run агента → доклад, tool-вызовы и инструменты хода."""
    started = time.monotonic()
    result = await Runner.run(agent, task, max_turns=6)
    seconds = time.monotonic() - started
    tools = collect_tool_runs(result)

    report = (result.final_output if isinstance(result.final_output, str) else "").strip()
    if not report:
        report = _final_text(result).strip()
    return {
        "report": report,
        "tools": tools,
        "seconds": round(seconds, 2),
        "usage": _usage(result),
        "messages": _final_messages(result, report),
    }


def _final_messages(result, final: str = "") -> list[str]:
    """Текстовые сообщения агента по порядку, последнее — финальный ответ."""
    from agents.items import ItemHelpers

    texts = [
        text.strip()
        for item in result.new_items
        if isinstance(item, MessageOutputItem)
        for text in [ItemHelpers.text_message_output(item)]
        if text and text.strip()
    ]
    if final and (not texts or texts[-1] != final):
        texts.append(final)
    return texts


def _final_text(result) -> str:
    from agents.items import ItemHelpers

    return ItemHelpers.text_message_outputs(
        [i for i in result.new_items if isinstance(i, MessageOutputItem)]
    )


def _usage(result) -> dict:
    usage = getattr(result.context_wrapper, "usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }
