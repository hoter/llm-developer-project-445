"""Demo-issuer: издатель JWT для контура.

Выдаёт токены, подписанные закрытым ключом (RS256), и публикует открытые
ключи (JWKS) — всё, что проверяющим нужно для самостоятельной проверки
подписи. Ключ можно сменить, не трогая потребителей: они перечитают JWKS.

Ограничение демо (проговариваем честно): полный OAuth 2.1 flow — consent,
браузерные редиректы, PKCE — продуктовая аутентификация, здесь её нет.
Backend — доверенный клиент: он просит токен для конкретного пользователя
и конкретного MCP-сервера (параметр resource, RFC 8707). Проверяющая
сторона — MCP-серверы — устроена как в проде.
"""
from __future__ import annotations

import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core import db
from core.config import ISSUER_URL, TOKEN_TTL_SEC

KID = "issuer-key-1"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_PRIVATE_PEM = _private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)

_PUBLIC_JWK: dict = jwt.algorithms.RSAAlgorithm.to_jwk(_private_key.public_key(), as_dict=True)
_PUBLIC_JWK.update({"kid": KID, "alg": "RS256", "use": "sig"})


def issue_token(*, resource: str, sub: str, role: str, scopes: list[str], ttl_sec: int) -> tuple[str, int]:
    """Токен под конкретный адресат: aud = resource (RFC 8707)."""
    now = int(time.time())
    claims = {
        "iss": ISSUER_URL,
        "sub": sub,
        "aud": resource,
        "role": role,
        "scope": " ".join(scopes),
        "iat": now,
        "exp": now + ttl_sec,
    }
    token = jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256", headers={"kid": KID})
    return token, claims["exp"]


async def token_endpoint(request: Request) -> JSONResponse:
    """POST /token — доверенный backend просит токен для пользователя.

    Тело: {"resource": "...", "user": "anna", "ttl_sec": 3600}.
    Пользователи и их роли — в таблице users (стенд, без паролей).
    """
    body = await request.json()
    user_name = body.get("user", "")
    resource = body.get("resource", "")
    ttl = int(body.get("ttl_sec", TOKEN_TTL_SEC))

    user = db.fetchone("SELECT sub, role, scopes FROM users WHERE login = %s", (user_name,))
    if user is None or not resource:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    token, exp = issue_token(
        resource=resource,
        sub=user["sub"],
        role=user["role"],
        scopes=list(user["scopes"]),
        ttl_sec=ttl,
    )
    return JSONResponse({"access_token": token, "token_type": "Bearer", "expires_at": exp})


async def jwks_endpoint(request: Request) -> JSONResponse:
    """GET /jwks.json — публикация открытых ключей издателя."""
    return JSONResponse({"keys": [_PUBLIC_JWK]})


def issuer_routes() -> list[Route]:
    return [
        Route("/token", token_endpoint, methods=["POST"]),
        Route("/jwks.json", jwks_endpoint, methods=["GET"]),
    ]
