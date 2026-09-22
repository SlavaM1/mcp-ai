import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from .logging_utils import log_event

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class MCPClientError(Exception):
    """Base exception safe to translate at the API boundary."""


class MCPConnectionError(MCPClientError):
    pass


class MCPTimeoutError(MCPClientError):
    pass


class MCPInvalidResponseError(MCPClientError):
    pass


class ToolSession(Protocol):
    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


SessionFactory = Callable[[], Any]


class MCPToolsClient:
    def __init__(
        self,
        server_url: str,
        timeout_seconds: float = 30.0,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.server_url = server_url
        self.timeout_seconds = timeout_seconds
        self._session_factory = session_factory

    @asynccontextmanager
    async def _open_session(self) -> AsyncIterator[ToolSession]:
        timeout = httpx2.Timeout(self.timeout_seconds)
        async with httpx2.AsyncClient(timeout=timeout) as http_client:
            async with streamable_http_client(
                self.server_url, http_client=http_client
            ) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self.timeout_seconds,
                ) as session:
                    await session.initialize()
                    yield session

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["ConnectedMCPTools"]:
        factory = self._session_factory or self._open_session
        log_event(logger, logging.INFO, "mcp_connecting", server_url=self.server_url)
        try:
            manager = factory()
            session = await manager.__aenter__()
        except Exception as exc:
            self._raise_translated(exc)

        log_event(logger, logging.INFO, "mcp_connected", server_url=self.server_url)
        try:
            yield ConnectedMCPTools(session)
        finally:
            try:
                await manager.__aexit__(None, None, None)
            except Exception as exc:
                self._raise_translated(exc)

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self.connect() as session:
            return await session.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self.connect() as session:
            return await session.call_tool(name, arguments)

    @staticmethod
    def _normalize_tool(tool: Any) -> dict[str, Any]:
        if hasattr(tool, "model_dump"):
            raw = tool.model_dump(mode="json", by_alias=True)
        elif isinstance(tool, dict):
            raw = tool
        else:
            raise MCPInvalidResponseError("MCP-сервер вернул неизвестный формат инструмента.")

        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise MCPInvalidResponseError("В описании MCP-инструмента отсутствует имя.")
        schema = raw.get("inputSchema", raw.get("input_schema"))
        if schema is not None and not isinstance(schema, dict):
            raise MCPInvalidResponseError("inputSchema MCP-инструмента должна быть JSON-объектом.")
        description = raw.get("description")
        return {
            "name": name,
            "description": description if isinstance(description, str) else None,
            "input_schema": schema,
        }

    @staticmethod
    def _contains_timeout(exc: BaseException) -> bool:
        if isinstance(exc, (TimeoutError, httpx2.TimeoutException)):
            return True
        if isinstance(exc, BaseExceptionGroup):
            return any(MCPToolsClient._contains_timeout(item) for item in exc.exceptions)
        return False

    @staticmethod
    def _raise_translated(exc: Exception) -> None:
        if isinstance(exc, MCPClientError):
            raise exc
        if MCPToolsClient._contains_timeout(exc):
            raise MCPTimeoutError("MCP-сервер не ответил за отведённое время.") from exc
        if isinstance(exc, ValidationError):
            raise MCPInvalidResponseError("MCP-сервер вернул некорректный ответ.") from exc
        if isinstance(exc, MCPError):
            message = str(exc)
            raise MCPConnectionError(f"Ошибка MCP-протокола: {message}") from exc
        raise MCPConnectionError("Не удалось подключиться к MCP-серверу.") from exc


class ConnectedMCPTools:
    def __init__(self, session: ToolSession) -> None:
        self._session = session

    async def list_tools(self) -> list[dict[str, Any]]:
        try:
            result = await self._session.list_tools()
        except Exception as exc:
            MCPToolsClient._raise_translated(exc)
        if not hasattr(result, "tools"):
            raise MCPInvalidResponseError("MCP-сервер вернул ответ без списка tools.")
        tools = [MCPToolsClient._normalize_tool(tool) for tool in result.tools]
        log_event(logger, logging.INFO, "mcp_tools_listed", tool_count=len(tools))
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as exc:
            MCPToolsClient._raise_translated(exc)
        raw = result.model_dump(mode="json", by_alias=True) if hasattr(result, "model_dump") else result
        if not isinstance(raw, dict):
            raise MCPInvalidResponseError("MCP-сервер вернул неизвестный формат результата.")
        if not isinstance(raw.get("content"), list):
            raise MCPInvalidResponseError("MCP-сервер вернул некорректный результат инструмента.")
        return {
            "is_error": bool(raw.get("isError", raw.get("is_error", False))),
            "content": raw.get("content", []),
            "structured_content": raw.get("structuredContent", raw.get("structured_content")),
        }
