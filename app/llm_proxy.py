"""Клиент пула: то, чем пул встраивается в SDK «на уровне клиента».

Агенты и модели SDK ходят в OpenAI через AsyncOpenAI; здесь это тот же
клиент, но `chat.completions.create` у него проходит через политику
переключения app.llm_pool. Модель из конфига подменяется на фактическую
модель провайдера, поэтому второй провайдер может быть другой моделью —
агентов пересобирать не нужно.

Всё остальное (base_url, api_key, прочие ручки) — у основного клиента:
SDK по base_url отличает официальный OpenAI от совместимого сервера.
"""
from __future__ import annotations

import json

from openai import AsyncOpenAI

from app.llm_pool import Pool


class _CompletionsProxy:
    """Замена chat.completions: каждый запрос идёт через пул."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._seen: set[str] = set()

    async def create(self, **kwargs):
        # модель из аргументов SDK заменяем моделью фактического провайдера;
        # повтор одного и того же вызова у провайдера — частый симптом того,
        # что модель «зациклилась», отражаем его полем response.retried
        key = json.dumps({"model": kwargs.get("model"),
                          "messages": kwargs.get("messages"),
                          "tools": kwargs.get("tools")}, ensure_ascii=False, sort_keys=True,
                         default=str)
        result = await self._pool.call(
            lambda client, model: client.chat.completions.create(**{**kwargs, "model": model})
        )
        retried = key in self._seen
        self._seen.add(key)
        if len(self._seen) > 64:
            self._seen.clear()
        try:
            result.retried = retried
        except (AttributeError, ValueError):  # ответ может быть неизменяемым
            pass
        return result

    def __getattr__(self, item):
        return getattr(self._pool.primary.client.chat.completions, item)


class _ChatProxy:
    def __init__(self, pool: Pool) -> None:
        self.completions = _CompletionsProxy(pool)
        self._pool = pool

    def __getattr__(self, item):
        return getattr(self._pool.primary.client.chat, item)


class PoolClient(AsyncOpenAI):
    """AsyncOpenAI с чатом через пул; остальное — основной провайдер."""

    def __init__(self, pool: Pool) -> None:
        primary = pool.primary.client
        super().__init__(base_url=primary.base_url, api_key=pool.primary.api_key, max_retries=0)
        self.pool = pool
        self.chat = _ChatProxy(pool)  # type: ignore[assignment]


def make_client(pool: Pool) -> PoolClient:
    return PoolClient(pool)
