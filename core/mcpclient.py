"""Клиентская сторона MCP: соединения сессии и вызовы с JWT.

На каждую сессию — свои HTTP-соединения с MCP-серверами; в каждый запрос
вкладывается Authorization: Bearer <JWT> текущего пользователя (заголовок
ставится один раз в параметры соединения — он уходит и в подключение, и в
запрос списка инструментов, и в каждый вызов).

Вызов обёрнут в политику одного повтора: транспортный сбой или 401
истёкшего токена → перевыпуск токена у издателя, переподключение,
один повтор. Ошибка роли (forbidden) — не транспортная: она возвращается
вызывающему как есть.
"""
from __future__ import annotations

import json

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from agents.mcp import MCPServerStreamableHttp
from mcp.shared.exceptions import MCPError

from core import config

FORBIDDEN_CODE = -32003


def _client_factory(headers=None, timeout=None, auth=None, **kwargs):
    """httpx-клиент соединения: мимо системного прокси (trust_env=False)."""
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False)


class McpPool:
    """Соединения сессии с набором MCP-серверов (resource_key → сервер)."""

    def __init__(self, tokens, resource_keys: tuple[str, ...] = ("crm", "billing", "kb", "actions")) -> None:
        self.tokens = tokens
        self.resource_keys = resource_keys
        self._servers: dict[str, MCPServerStreamableHttp] = {}
        self._tools: dict[str, tuple[str, object]] = {}  # имя → (resource_key, Tool)

    async def connect(self) -> None:
        for key in self.resource_keys:
            await self._connect(key)
        for key, server in self._servers.items():
            for tool in await server.list_tools():
                self._tools[tool.name] = (key, tool)

    async def _connect(self, key: str) -> MCPServerStreamableHttp:
        token = await self.tokens.get(key)
        url = self._url(key)
        server = MCPServerStreamableHttp(
            params={
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
                "httpx_client_factory": _client_factory,
            },
        )
        await server.__aenter__()
        self._servers[key] = server
        return server

    def _url(self, key: str) -> str:
        if key == "actions":
            return f"{config.ACTIONS_URL}/actions/mcp"
        return f"{config.STAND_URL}/{key}/mcp"

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def resource_of(self, name: str) -> str:
        """Какому серверу принадлежит инструмент."""
        return self._tools[name][0]

    def tool_schema(self, name: str) -> dict:
        tool = self._tools[name][1]
        schema = getattr(tool, "input_schema", None) or tool.inputSchema
        return dict(schema)

    def tool_description(self, name: str) -> str:
        return self._tools[name][1].description or name

    async def call(self, resource_key: str, tool_name: str, args: dict | None = None) -> dict:
        """Вызов инструмента с одной попыткой самовосстановления.

        Транспортная ошибка (включая 401 истёкшего токена) → refresh +
        переподключение + один повтор. Ошибки самого инструмента
        (например forbidden по роли) возвращаются вызывающему.
        """
        try:
            return await self._call_once(resource_key, tool_name, args)
        except MCPError as e:
            if getattr(e, "code", None) == FORBIDDEN_CODE:
                raise  # отказ самого инструмента (например, по роли) — не транспорт
            return await self._recover_and_retry(resource_key, tool_name, args)
        except Exception:
            return await self._recover_and_retry(resource_key, tool_name, args)

    async def _recover_and_retry(self, resource_key: str, tool_name: str, args: dict | None) -> dict:
        await self.tokens.refresh(resource_key)
        old = self._servers.pop(resource_key, None)
        if old is not None:
            try:
                await old.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — закрытие не должно ронять сессию
                pass
        await self._connect(resource_key)
        return await self._call_once(resource_key, tool_name, args)

    async def _call_once(self, resource_key: str, tool_name: str, args: dict | None) -> dict:
        result = await self._servers[resource_key].call_tool(tool_name, args or {})
        text = result.content[0].text if result.content else "{}"
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return {"raw": text}

    async def close(self) -> None:
        for server in self._servers.values():
            try:
                await server.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — закрытие не должно ронять сессию
                pass
        self._servers.clear()
