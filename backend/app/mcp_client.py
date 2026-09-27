import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

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


class MCPUnknownToolError(MCPClientError):
    pass


class MCPRegistryError(MCPClientError):
    def __init__(self, message: str, servers: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.servers = servers


SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
MAX_LLM_TOOL_NAME_LENGTH = 64


def validate_server_name(name: str) -> str:
    if not SAFE_NAME_PATTERN.fullmatch(name) or "__" in name or len(name) > 32:
        raise ValueError(
            "Имя MCP-сервера должно содержать только ASCII-буквы, цифры, '_' или '-', "
            "не содержать '__' и быть не длиннее 32 символов."
        )
    return name


def safe_server_url(server_url: str) -> str:
    """Return a URL suitable for logs and API metadata without credentials."""
    parsed = urlsplit(server_url)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))


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
        public_url = safe_server_url(self.server_url)
        log_event(logger, logging.INFO, "mcp_connecting", server_url=public_url)
        try:
            manager = factory()
            session = await manager.__aenter__()
        except Exception as exc:
            self._raise_translated(exc)

        log_event(logger, logging.INFO, "mcp_connected", server_url=public_url)
        try:
            yield ConnectedMCPTools(session)
        finally:
            try:
                await manager.__aexit__(None, None, None)
            except Exception as exc:
                try:
                    self._raise_translated(exc)
                except MCPClientError as cleanup_error:
                    log_event(
                        logger,
                        logging.WARNING,
                        "mcp_disconnect_failed",
                        server_url=public_url,
                        error=str(cleanup_error),
                    )

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


RegistryClientFactory = Callable[[str, str], MCPToolsClient]


class MCPRegistry:
    """Configured collection of MCP transports; connections live for one request."""

    def __init__(
        self,
        servers: dict[str, str],
        timeout_seconds: float = 30.0,
        client_factory: RegistryClientFactory | None = None,
    ) -> None:
        if not servers:
            raise ValueError("Должен быть настроен хотя бы один MCP-сервер.")
        for name, url in servers.items():
            validate_server_name(name)
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"Для MCP-сервера '{name}' задан некорректный HTTP(S) URL.")

        self._servers = dict(servers)
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory or (
            lambda _name, url: MCPToolsClient(url, self._timeout_seconds)
        )
        self._statuses = [self._new_status(name, url) for name, url in self._servers.items()]

    @property
    def server_url(self) -> str | None:
        return safe_server_url(next(iter(self._servers.values()))) if self._servers else None

    @property
    def servers(self) -> list[dict[str, Any]]:
        return [dict(status) for status in self._statuses]

    @property
    def status(self) -> str:
        healthy = sum(item["status"] == "ok" for item in self._statuses)
        if healthy == len(self._statuses):
            return "ok"
        if healthy:
            return "partial"
        return "unavailable"

    @property
    def connected(self) -> bool:
        return any(item["status"] == "ok" for item in self._statuses)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["ConnectedMCPRegistry"]:
        statuses = [self._new_status(name, url) for name, url in self._servers.items()]
        connected: dict[str, ConnectedMCPTools] = {}
        async with AsyncExitStack() as stack:
            for index, (name, url) in enumerate(self._servers.items()):
                try:
                    client = self._client_factory(name, url)
                    connected[name] = await stack.enter_async_context(client.connect())
                    statuses[index]["status"] = "connected"
                except MCPClientError as exc:
                    statuses[index]["error"] = str(exc)
                except Exception as exc:
                    error = self._translate_exception(exc)
                    statuses[index]["error"] = str(error)

            self._statuses = statuses
            if not connected:
                raise MCPRegistryError(
                    "Не удалось подключиться ни к одному настроенному MCP-серверу.",
                    self.servers,
                )

            registry = ConnectedMCPRegistry(self._servers, connected, statuses)
            try:
                yield registry
            finally:
                self._statuses = registry.servers

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self.connect() as registry:
            return await registry.list_tools()

    @staticmethod
    def _new_status(name: str, url: str) -> dict[str, Any]:
        return {
            "server_name": name,
            "server_url": safe_server_url(url),
            "status": "unavailable",
            "tool_count": 0,
            "error": None,
        }

    @staticmethod
    def _translate_exception(exc: Exception) -> MCPClientError:
        try:
            MCPToolsClient._raise_translated(exc)
        except MCPClientError as translated:
            return translated
        raise AssertionError("MCP exception translation did not raise")


class ConnectedMCPRegistry:
    def __init__(
        self,
        server_urls: dict[str, str],
        sessions: dict[str, ConnectedMCPTools],
        statuses: list[dict[str, Any]],
    ) -> None:
        self._server_urls = server_urls
        self._sessions = sessions
        self._statuses = statuses
        self._routes: dict[str, dict[str, Any]] = {}

    @property
    def servers(self) -> list[dict[str, Any]]:
        return [dict(status) for status in self._statuses]

    @property
    def status(self) -> str:
        healthy = sum(item["status"] == "ok" for item in self._statuses)
        if healthy == len(self._statuses):
            return "ok"
        if healthy:
            return "partial"
        return "unavailable"

    async def list_tools(self) -> list[dict[str, Any]]:
        self._routes = {}
        tools: list[dict[str, Any]] = []
        status_by_name = {item["server_name"]: item for item in self._statuses}

        for server_name, session in self._sessions.items():
            status = status_by_name[server_name]
            try:
                discovered = await session.list_tools()
                normalized = self._namespace_tools(server_name, discovered)
            except MCPClientError as exc:
                status.update(status="unavailable", tool_count=0, error=str(exc))
                continue
            except Exception as exc:
                error = MCPRegistry._translate_exception(exc)
                status.update(status="unavailable", tool_count=0, error=str(error))
                continue

            status.update(status="ok", tool_count=len(normalized), error=None)
            for tool in normalized:
                self._routes[tool["name"]] = tool
            tools.extend(normalized)

        if not any(item["status"] == "ok" for item in self._statuses):
            raise MCPRegistryError(
                "Ни один MCP-сервер не вернул доступный каталог инструментов.",
                self.servers,
            )
        return tools

    def route_metadata(self, llm_tool_name: str) -> dict[str, Any] | None:
        route = self._routes.get(llm_tool_name)
        return dict(route) if route else None

    async def call_tool(self, llm_tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        route = self._routes.get(llm_tool_name)
        if route is None:
            raise MCPUnknownToolError(
                f"Инструмент «{llm_tool_name}» отсутствует в обнаруженном MCP-каталоге."
            )

        server_name = route["server_name"]
        session = self._sessions[server_name]
        try:
            return await session.call_tool(route["original_name"], arguments)
        except MCPClientError as exc:
            self._mark_runtime_failure(server_name, exc)
            raise
        except Exception as exc:
            error = MCPRegistry._translate_exception(exc)
            self._mark_runtime_failure(server_name, error)
            raise error from exc

    def _namespace_tools(
        self,
        server_name: str,
        discovered: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tool in discovered:
            original_name = tool.get("name")
            if not isinstance(original_name, str) or not SAFE_NAME_PATTERN.fullmatch(original_name):
                raise MCPInvalidResponseError(
                    f"MCP-сервер '{server_name}' вернул небезопасное имя инструмента."
                )
            llm_tool_name = f"{server_name}__{original_name}"
            if len(llm_tool_name) > MAX_LLM_TOOL_NAME_LENGTH:
                raise MCPInvalidResponseError(
                    f"Имя инструмента MCP-сервера '{server_name}' слишком длинное для LLM API."
                )
            if llm_tool_name in seen or llm_tool_name in self._routes:
                raise MCPInvalidResponseError(
                    f"MCP-сервер '{server_name}' вернул повторяющееся имя инструмента."
                )
            seen.add(llm_tool_name)
            normalized.append(
                {
                    "name": llm_tool_name,
                    "llm_tool_name": llm_tool_name,
                    "original_name": original_name,
                    "tool_name": original_name,
                    "server_name": server_name,
                    "server_url": safe_server_url(self._server_urls[server_name]),
                    "description": tool.get("description"),
                    "input_schema": tool.get("input_schema"),
                }
            )
        return normalized

    def _mark_runtime_failure(self, server_name: str, exc: MCPClientError) -> None:
        for status in self._statuses:
            if status["server_name"] == server_name:
                status.update(status="unavailable", error=str(exc))
                return


MCPToolsRegistry = MCPRegistry
