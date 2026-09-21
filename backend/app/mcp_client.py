from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError


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

    async def list_tools(self) -> list[dict[str, Any]]:
        factory = self._session_factory or self._open_session
        try:
            async with factory() as session:
                result = await session.list_tools()
            if not hasattr(result, "tools"):
                raise MCPInvalidResponseError("MCP-сервер вернул ответ без списка tools.")
            return [self._normalize_tool(tool) for tool in result.tools]
        except MCPClientError:
            raise
        except (TimeoutError, httpx2.TimeoutException) as exc:
            raise MCPTimeoutError("MCP-сервер не ответил за отведённое время.") from exc
        except ValidationError as exc:
            raise MCPInvalidResponseError("MCP-сервер вернул некорректный ответ.") from exc
        except MCPError as exc:
            message = str(exc)
            if "timed out" in message.lower() or "timeout" in message.lower():
                raise MCPTimeoutError("MCP-сервер не ответил за отведённое время.") from exc
            raise MCPConnectionError(f"Ошибка MCP-протокола: {message}") from exc
        except Exception as exc:
            if self._contains_timeout(exc):
                raise MCPTimeoutError("MCP-сервер не ответил за отведённое время.") from exc
            raise MCPConnectionError("Не удалось подключиться к MCP-серверу.") from exc

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
