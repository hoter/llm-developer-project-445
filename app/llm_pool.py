"""Пул провайдеров модели: переключение без пересборки агентов.

Агенты собираются через app.llm.build_agent и берут модель из конфига;
пул встраивается на уровне клиента: core.config.setup_model кладёт его в
SDK через set_default_openai_client, и агенты о пуле ничего не знают.

Политика переключения — три правила:

1. Повторяем только то, что имеет смысл повторять: 429, 5xx, таймаут и
   сетевой сбой уходят к следующему провайдеру. 400, 401, 403 — ошибка
   самого вызова, у другого провайдера она будет той же, поэтому
   возвращаются вызывающему.
2. Провайдер, вернувший 429 или 5xx, исключается из пула на
   LLM_FAILOVER_COOLDOWN_SEC секунд: контур не долбит упавший сервер.
3. У запасного провайдера своя модель: фактический провайдер и модель
   пишутся в llm_calls, стоимость считается по цене фактической модели.

Пул обходится один раз: каждая попытка — следующий провайдер, без
повторов внутри одного провайдера.
"""
from __future__ import annotations

import contextvars
import os
import time
from dataclasses import dataclass, field

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx
# оба семейства: проект ходит через httpx2, но SDK и чужие клиенты — через httpx
import httpx as _httpx_plain

import openai
from openai import AsyncOpenAI

from core import config

# сетевые сбои, после которых есть смысл спросить другого провайдера —
# как из httpx/httpx2, так и обёрнутые SDK в свои APITimeoutError/APIConnectionError
RETRY_NETWORK_ERRORS = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TransportError,
    _httpx_plain.ConnectError, _httpx_plain.ConnectTimeout, _httpx_plain.ReadTimeout,
    _httpx_plain.TransportError,
    openai.APITimeoutError, openai.APIConnectionError,
)
# после этих ответов провайдер отдыхает
COOLDOWN_STATUS = {429}


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    api_key: str
    price_rub_per_1m: float
    client: AsyncOpenAI | None = None
    blocked_until: float = 0.0

    def available(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) >= self.blocked_until

    def block(self, seconds: float) -> None:
        self.blocked_until = time.monotonic() + seconds


@dataclass
class Usage:
    """Итог одного вызова модели: фактический провайдер и переключения."""

    provider: str = ""
    model: str = ""
    price_rub_per_1m: float = 0.0
    fallbacks: list[dict] = field(default_factory=list)   # почему ушли с провайдера
    calls: int = 0

    def with_provider(self, provider: Provider) -> "Usage":
        return Usage(provider=provider.name, model=provider.model,
                     price_rub_per_1m=provider.price_rub_per_1m,
                     fallbacks=self.fallbacks, calls=self.calls)

    def cost_rub(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens + output_tokens) / 1_000_000 * self.price_rub_per_1m


# последний вызов в текущем контексте (ходе): llm.py читает его после Runner.run
CURRENT: contextvars.ContextVar[Usage] = contextvars.ContextVar("llm_usage", default=Usage())


def _make_client(provider: Provider) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=provider.base_url,
        api_key=provider.api_key,
        # повторы — забота пула, а не SDK
        max_retries=0,
        http_client=httpx.AsyncClient(trust_env=False, timeout=180),
    )


class Pool:
    def __init__(self, providers: list[Provider], cooldown_sec: float) -> None:
        self.providers = providers
        self.cooldown_sec = cooldown_sec
        for p in providers:
            p.client = _make_client(p)

    @property
    def primary(self) -> Provider:
        return self.providers[0]

    def enabled(self) -> bool:
        return len(self.providers) > 1

    def usage(self) -> Usage:
        return CURRENT.get()

    def _queue(self, started_at: float) -> list[Provider]:
        """Провайдеры, доступные на момент начала вызова, — по кругу."""
        available = [p for p in self.providers if p.available(started_at)]
        # все на паузе: пробуем всех по одному разу, начиная с основного —
        # иначе ход просто не состоялся бы
        return available or [p for p in self.providers if p.available()] or list(self.providers)

    async def call(self, fn):
        """Выполнить fn(client, model) с политикой переключения.

        Возвращает результат; итог (фактический провайдер, переключения)
        остаётся в CURRENT, его читает app.llm для llm_calls.
        """
        started_at = time.monotonic()
        usage = Usage()
        errors: list[str] = []
        queue = self._queue(started_at)
        # обход пула ровно один раз
        for provider in queue:
            usage.calls += 1
            try:
                result = await fn(provider.client, provider.model)
            except Exception as e:  # noqa: BLE001 — решаем по типу ошибки
                if not self.should_failover(e):
                    # 400/401/403 и прочие ошибки вызова: не повторяем
                    CURRENT.set(usage.with_provider(provider))
                    raise
                if self.should_cooldown(e):
                    provider.block(self.cooldown_sec)
                error_text = self.error_text(e)
                usage.fallbacks.append({"provider": provider.name, "error": error_text})
                errors.append(f"{provider.name}: {error_text}")
                continue
            CURRENT.set(usage.with_provider(provider))
            return result

        CURRENT.set(usage)
        raise FailoverExhausted("все провайдеры модели недоступны: " + "; ".join(errors))

    @staticmethod
    def should_failover(error: Exception) -> bool:
        """Правило 1: повтор — только 429, 5xx, таймаут и сетевой сбой."""
        if isinstance(error, openai.APIStatusError):
            status = error.status_code
            return status == 429 or status >= 500
        return isinstance(error, RETRY_NETWORK_ERRORS)

    @staticmethod
    def should_cooldown(error: Exception) -> bool:
        """Правило 2: пауза провайдеру за 429 и 5xx."""
        return isinstance(error, openai.APIStatusError) and (
            error.status_code in COOLDOWN_STATUS or error.status_code >= 500
        )

    @staticmethod
    def error_text(error: Exception) -> str:
        if isinstance(error, openai.APIStatusError):
            return f"http {error.status_code}"
        return type(error).__name__


class FailoverExhausted(RuntimeError):
    """Все провайдеры пула ответили отказом, который стоит повторить."""


def _provider_from_env(prefix: str, name: str, default_model: str = "") -> Provider | None:
    base_url = os.environ.get(f"{prefix}BASE_URL", "").strip()
    if not base_url:
        return None
    model = os.environ.get(f"{prefix}MODEL", "") or default_model
    return Provider(
        name=name,
        base_url=base_url,
        model=model,
        api_key=os.environ.get(f"{prefix}API_KEY", "none") or "none",
        price_rub_per_1m=config.price_of_model(model),
    )


POOL: Pool | None = None


def setup_pool() -> Pool:
    """Пул из .env: основной + запасной (если задан LLM_SECONDARY_BASE_URL)."""
    global POOL
    primary = _provider_from_env("LLM_", "primary") or Provider(
        name="primary", base_url="", model="", api_key="none",
        price_rub_per_1m=config.LLM_PRICE_RUB_PER_1M,
    )
    providers = [primary]
    secondary = _provider_from_env("LLM_SECONDARY_", "secondary", primary.model)
    if secondary is not None:
        providers.append(secondary)
    cooldown = float(os.environ.get("LLM_FAILOVER_COOLDOWN_SEC", "60"))
    POOL = Pool(providers, cooldown)
    return POOL


def enabled() -> bool:
    return POOL is not None and POOL.enabled()
