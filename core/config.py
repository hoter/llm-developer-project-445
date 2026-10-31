"""Конфигурация контура: адреса сервисов, пользователи, LLM, Postgres.

Все http-клиенты контура создаются с trust_env=False: системный прокси
не должен перехватывать запросы к 127.0.0.1 (иначе 502 или зависание).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    for candidate in (ROOT / ".env",):
        if candidate.exists():
            load_dotenv(candidate)
            break


_load_env()

# ── Адреса сервисов контура ──────────────────────────────────────────────
STAND_PORT = int(os.environ.get("STAND_PORT", "8100"))
STAND_URL = f"http://127.0.0.1:{STAND_PORT}"
ISSUER_URL = STAND_URL  # издатель живёт на стенде готового

BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8000"))
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
ACTIONS_PORT = int(os.environ.get("ACTIONS_PORT", "8220"))
ACTIONS_URL = f"http://127.0.0.1:{ACTIONS_PORT}"
PDP_PORT = int(os.environ.get("PDP_PORT", "9100"))
PDP_URL = f"http://127.0.0.1:{PDP_PORT}"
GUARDRAIL_PORT = int(os.environ.get("GUARDRAIL_PORT", "8300"))
GUARDRAIL_URL = f"http://127.0.0.1:{GUARDRAIL_PORT}"

PG_DSN = os.environ.get("PG_DSN", "postgresql://operator:operator@127.0.0.1:5434/operator")

TOKEN_TTL_SEC = int(os.environ.get("TOKEN_TTL_SEC", "3600"))
CARD_TTL_SEC = int(os.environ.get("CARD_TTL_SEC", "120"))
BUDGET_RUB = float(os.environ.get("BUDGET_RUB", "50"))
LLM_PRICE_RUB_PER_1M = float(os.environ.get("LLM_PRICE_RUB_PER_1M", "60"))

# Цена за 1M токенов по фактической модели: «LLM_MODEL=60,LLM_SECONDARY_MODEL=25».
# Модель без своей цены считается по LLM_PRICE_RUB_PER_1M.
def _parse_prices(raw: str) -> dict[str, float]:
    prices: dict[str, float] = {}
    for chunk in (raw or "").split(","):
        if "=" not in chunk:
            continue
        model, _, price = chunk.partition("=")
        try:
            prices[model.strip()] = float(price)
        except ValueError:
            continue
    return prices


MODEL_PRICES: dict[str, float] = _parse_prices(os.environ.get("LLM_MODEL_PRICES", ""))


def price_of_model(model: str) -> float:
    return MODEL_PRICES.get(model, LLM_PRICE_RUB_PER_1M)


# Запасной провайдер: пул включается, когда задан LLM_SECONDARY_BASE_URL
LLM_SECONDARY_BASE_URL = os.environ.get("LLM_SECONDARY_BASE_URL", "").strip()
LLM_SECONDARY_MODEL = os.environ.get("LLM_SECONDARY_MODEL", "").strip()
LLM_FAILOVER_COOLDOWN_SEC = float(os.environ.get("LLM_FAILOVER_COOLDOWN_SEC", "60"))

# ── Ресурсы MCP-серверов (RFC 8707): canonical URI адресата токена ───────
RESOURCES = {
    "crm": f"{STAND_URL}/crm",
    "billing": f"{STAND_URL}/billing",
    "kb": f"{STAND_URL}/kb",
    "actions": f"{ACTIONS_URL}/actions",
}


@dataclass
class User:
    login: str
    sub: str
    name: str
    role: str
    scopes: list[str] = field(default_factory=list)


# Демо-пользователи стенда (паролей нет: аутентификация вне рамок проекта).
# anna — клиент с правом менять тарифы; boris — клиент только на чтение;
# olga — оператор поддержки.
USERS: dict[str, User] = {
    "anna": User("anna", "u_4f2a", "Анна", "client", ["tariffs:read", "tariffs:write", "charges:read"]),
    "boris": User("boris", "u_9b1c", "Борис", "client", ["tariffs:read", "charges:read"]),
    "olga": User("olga", "u_op7", "Ольга", "operator", ["tariffs:read", "charges:read", "support:read"]),
}

MODEL_NAME = ""


def setup_model() -> str:
    """Подключение LLM: подменяет клиент и модель по умолчанию для SDK.

    Клиент — из пула провайдеров (app.llm_pool): политика переключения живёт
    в прокси-клиенте, поэтому агенты собираются как обычно и о пуле не знают.
    """
    global MODEL_NAME
    base_url = os.environ.get("LLM_BASE_URL")
    model_name = os.environ.get("LLM_MODEL")
    if not base_url or not model_name:
        raise SystemExit("Нет LLM_BASE_URL/LLM_MODEL: скопируйте .env.example в .env и заполните")

    from agents import set_default_openai_api, set_default_openai_client, set_tracing_disabled

    from app import llm_proxy
    from app.llm_pool import setup_pool

    client = llm_proxy.make_client(setup_pool())
    set_default_openai_client(client, use_for_tracing=False)
    set_default_openai_api("chat_completions")
    set_tracing_disabled(True)
    MODEL_NAME = model_name
    return model_name
