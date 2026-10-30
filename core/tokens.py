"""Токены сессии: по одному на каждый MCP-сервер (resource), с перевыпуском.

Один токен на все серверы нарушал бы привязку к адресату (RFC 8707):
клиент не должен отправлять серверу чужие для него токены, сервер
обязан такие отвергать. Поэтому на сессию — по токену на каждый ресурс;
TTL у всех один, жизнь токена — жизнь сессии.

Токен истёк к середине диалога → следующий запрос получает 401 →
клиент перевыпускает токен у издателя и повторяет; пользователь
ничего не замечает. force_short() — стендовая команда для демо:
перевыпускает все токены с TTL 1 секунда.
"""
from __future__ import annotations

import jwt as pyjwt

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config


class Tokens:
    """Кэш токенов сессии: resource_key → access token."""

    def __init__(self, user_name: str) -> None:
        self.user_name = user_name
        self._tokens: dict[str, str] = {}
        self._claims: dict[str, dict] = {}

    async def claims(self, resource_key: str = "crm") -> dict:
        """Claims токена сессии — из них собран allowlist."""
        if resource_key not in self._claims:
            await self.get(resource_key)
        return self._claims[resource_key]

    async def get(self, resource_key: str) -> str:
        """Токен для конкретного сервера; выпускается при первом запросе."""
        if resource_key not in self._tokens:
            await self._request(resource_key, ttl_sec=config.TOKEN_TTL_SEC)
        return self._tokens[resource_key]

    async def refresh(self, resource_key: str) -> str:
        """Перевыпуск после 401: старый токен больше не используется."""
        self._tokens.pop(resource_key, None)
        self._claims.pop(resource_key, None)
        token = await self.get(resource_key)
        return token

    async def force_short(self) -> None:
        """Стенд: заменить все токены на истекающие через секунду."""
        for key in list(self._tokens):
            await self._request(key, ttl_sec=1)

    async def _request(self, resource_key: str, ttl_sec: int) -> None:
        resource = config.RESOURCES[resource_key]
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            r = await client.post(
                f"{config.ISSUER_URL}/token",
                json={"resource": resource, "user": self.user_name, "ttl_sec": ttl_sec},
            )
            r.raise_for_status()
            token = r.json()["access_token"]
        self._tokens[resource_key] = token
        self._claims[resource_key] = pyjwt.decode(token, options={"verify_signature": False})
