"""Сборка агентов на модели из .env + учёт LLM-вызовов.

TODO(агенты):
  1. build_agent(name, instructions, tools=None, output_type=None):
     Agent из agents SDK; output_type — нестрогая схема
     (AgentOutputSchema(output_type, strict_json_schema=False)):
     строгий JSON-режим слишком требователен к малым моделям;
     модель — config.MODEL_NAME или config.setup_model().
  2. run_agent(agent, task, *, trace_id="") → {"report", "tools", "seconds"}:
     отдельный Runner.run; из result.new_items собрать tool-вызовы
     (call_id → arguments/output); report — result.final_output строкой.

TODO(аудит): в run_agent — INSERT в llm_calls: trace_id, имя агента,
  модель, токены (result.context_wrapper.usage), латентность.

TODO(failover): если llm_pool.enabled() — запуск через
  llm_pool.run_with_failover, в llm_calls — фактический провайдер.
"""
from __future__ import annotations
