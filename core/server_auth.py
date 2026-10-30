"""Проверка токенов на MCP-сервере: сервер не доверяет вызывающему.

Каждый MCP-сервер контура — OAuth 2.1 resource server: входящий Bearer
проверяется самим сервером — подпись по открытому ключу издателя (JWKS),
издатель (iss) и адресат (aud) должны быть своими, срок (exp) — живым.
Неверная подпись, чужой издатель, токен не для этого сервера, истёкший
срок — 401, вызов не обслуживается.

Кэш открытых ключей (JwkCache) забирает ключи из публикации издателя и
обновляет их при незнакомом kid — так смена ключа издателем не требует
перезапуска потребителей. Запрос идёт мимо системного прокси.
"""
from __future__ import annotations

import time

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from core.config import ISSUER_URL

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx


class JwkCache:
    """Открытые ключи издателя: забрать, закэшировать, обновить при смене."""

    def __init__(self, jwks_url: str) -> None:
        self.jwks_url = jwks_url
        self._keys: dict[str, dict] = {}
        self._fetched_at = 0.0

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            r = await client.get(self.jwks_url)
            r.raise_for_status()
        self._keys = {k["kid"]: k for k in r.json()["keys"] if k.get("use", "sig") == "sig"}
        self._fetched_at = time.monotonic()

    async def get(self, kid: str | None) -> dict:
        if not self._keys or (kid and kid not in self._keys):
            await self._fetch()
        if kid is None and len(self._keys) == 1:  # токен без kid, ключ один
            return next(iter(self._keys.values()))
        return self._keys.get(kid, {})  # {} → подпись не проверить → 401


class JwtVerifier(TokenVerifier):
    """TokenVerifier для MCP-сервера: aud и iss — только свои.

    Возвращает AccessToken, в котором claims токена доезжают до кода
    инструмента (get_access_token()) — субъект читается из проверенного
    токена вызова, а не из аргументов.
    """

    def __init__(self, resource: str) -> None:
        self.resource = resource
        self.jwks = JwkCache(f"{ISSUER_URL}/jwks.json")

    async def verify_bearer(self, token: str) -> dict | None:
        """Проверка одного Bearer-токена → claims или None.

        Четыре проверки: подпись по ключу из JWKS, издатель, адресат, срок.
        Любая не сошлась — None (SDK ответит 401 с resource_metadata).
        """
        try:
            header = jwt.get_unverified_header(token)
            key = await self.jwks.get(header.get("kid"))
            if not key:
                return None
            public_pem = jwt.algorithms.RSAAlgorithm.from_jwk(key)
            claims = jwt.decode(
                token,
                key=public_pem,
                algorithms=["RS256"],
                audience=self.resource,
                issuer=ISSUER_URL,
            )
        except (jwt.PyJWTError, httpx.HTTPError):
            # кривой токен или недоступна публикация ключей — вызов не обслуживается
            return None
        return claims

    async def verify_token(self, token: str) -> AccessToken | None:
        claims = await self.verify_bearer(token)
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id="demo-backend",
            scopes=claims.get("scope", "").split(),
            expires_at=claims.get("exp"),
            resource=claims.get("aud"),
            subject=claims.get("sub"),
            claims=claims,
        )


def make_mcp_server(name: str, *, resource: str, instructions: str = ""):
    """MCPServer с включённой авторизацией: resource server по спецификации.

    SDK по этим настройкам сам ставит middleware: проверка Bearer в каждом
    запросе, привязка токена к адресату (validate_token_resource), 401 с
    WWW-Authenticate resource_metadata, метаданные защищённого ресурса
    (RFC 9728).
    """
    from mcp.server import MCPServer
    from mcp.server.auth.settings import AuthSettings

    server = MCPServer(
        name=name,
        instructions=instructions or None,
        auth=AuthSettings(
            issuer_url=ISSUER_URL,
            resource_server_url=resource,
            validate_token_resource=True,
        ),
        token_verifier=JwtVerifier(resource),
    )
    return server
