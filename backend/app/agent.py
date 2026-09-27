import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from .llm_client import (
    LLMConfigurationError,
    LLMError,
    LLMInvalidResponseError,
    OpenAICompatibleClient,
)
from .logging_utils import log_event
from .mcp_client import ConnectedMCPRegistry, MCPClientError, MCPRegistry, MCPTimeoutError
from .models import ChatMessage, MessageRole

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SYSTEM_PROMPT = """Ты полезный русскоязычный ассистент. Учитывай историю диалога.
Доступные инструменты предоставлены несколькими MCP-серверами динамически и имеют имена с
пространством имён сервера. Сам решай, нужен ли инструмент: вызывай его только для получения
актуальных или внешних данных, а на обычные вопросы отвечай без инструмента. Если задача зависит от
нескольких источников или этапов, вызывай инструменты последовательно в порядке зависимостей.
Используй фактический structured_content результата предыдущего вызова в аргументах следующего,
в том числе при передаче данных между разными серверами; не подменяй эти данные догадками. После
всех необходимых вызовов сформулируй понятный итоговый ответ и не показывай сырой JSON. Ошибку
одного инструмента учитывай как контролируемый результат и по возможности продолжай задачу."""


@dataclass
class AgentResult:
    content: str
    tools: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    servers: list[dict[str, Any]]


class AgentExecutionError(Exception):
    def __init__(
        self,
        message: str,
        category: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        servers: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.tools = tools or []
        self.tool_calls = tool_calls or []
        self.servers = servers or []


class ChatAgent:
    def __init__(
        self,
        mcp_registry: MCPRegistry,
        llm_client: OpenAICompatibleClient,
        max_tool_calls: int,
    ) -> None:
        self._mcp_registry = mcp_registry
        self._llm_client = llm_client
        self._max_tool_calls = max_tool_calls

    async def run(
        self,
        history: list[ChatMessage],
        user_content: str,
    ) -> AgentResult:
        tools: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        servers: list[dict[str, Any]] = []
        try:
            self._llm_client.ensure_configured()
            async with self._mcp_registry.connect() as mcp_session:
                tools = await mcp_session.list_tools()
                servers = mcp_session.servers
                allowed_tools = {tool["name"]: tool for tool in tools}
                llm_tools = [self._to_llm_tool(tool) for tool in tools]
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *[
                        {"role": message.role.value, "content": message.content}
                        for message in history
                        if message.role in (MessageRole.USER, MessageRole.ASSISTANT)
                    ],
                    {"role": "user", "content": user_content},
                ]
                unavailable = [
                    item["server_name"]
                    for item in servers
                    if item.get("status") == "unavailable"
                ]
                if unavailable:
                    messages.insert(
                        1,
                        {
                            "role": "system",
                            "content": (
                                "Сейчас недоступны MCP-серверы: "
                                + ", ".join(unavailable)
                                + ". Если запрос требует их инструментов, явно сообщи пользователю "
                                "об ограничении; не имитируй отсутствующие результаты."
                            ),
                        },
                    )

                while True:
                    assistant = await self._llm_client.complete(messages, llm_tools)
                    requested_calls = assistant.get("tool_calls") or []
                    if not requested_calls:
                        content = assistant.get("content")
                        if not isinstance(content, str) or not content.strip():
                            raise AgentExecutionError(
                                "LLM не сформировала итоговый ответ.",
                                "llm_invalid_response",
                                tools=tools,
                                tool_calls=calls,
                            )
                        return AgentResult(
                            content=content.strip(),
                            tools=tools,
                            tool_calls=calls,
                            servers=mcp_session.servers,
                        )

                    normalized_calls = [self._normalize_tool_call(item) for item in requested_calls]
                    if len(calls) + len(normalized_calls) > self._max_tool_calls:
                        log_event(
                            logger,
                            logging.ERROR,
                            "agent_tool_limit_exceeded",
                            max_tool_calls=self._max_tool_calls,
                        )
                        raise AgentExecutionError(
                            "Агент превысил допустимое число последовательных вызовов инструментов.",
                            "tool_limit",
                            tools=tools,
                            tool_calls=calls,
                        )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": assistant.get("content"),
                            "tool_calls": normalized_calls,
                        }
                    )
                    for tool_call in normalized_calls:
                        call_record, tool_message = await self._execute_tool_call(
                            mcp_session,
                            tool_call,
                            allowed_tools,
                        )
                        call_record["sequence"] = len(calls) + 1
                        calls.append(call_record)
                        messages.append(tool_message)
                        servers = mcp_session.servers
        except AgentExecutionError as exc:
            if not exc.tools:
                exc.tools = tools
            if not exc.tool_calls:
                exc.tool_calls = calls
            if not exc.servers:
                exc.servers = servers
            raise
        except LLMConfigurationError as exc:
            raise AgentExecutionError(
                str(exc),
                "llm_configuration",
                tools=tools,
                tool_calls=calls,
                servers=servers,
            ) from exc
        except LLMInvalidResponseError as exc:
            raise AgentExecutionError(
                str(exc),
                "llm_invalid_response",
                tools=tools,
                tool_calls=calls,
                servers=servers,
            ) from exc
        except LLMError as exc:
            raise AgentExecutionError(
                str(exc),
                "llm_unavailable",
                tools=tools,
                tool_calls=calls,
                servers=servers,
            ) from exc
        except MCPClientError as exc:
            error_servers = getattr(exc, "servers", None) or self._mcp_registry.servers
            raise AgentExecutionError(
                str(exc),
                "mcp_timeout" if isinstance(exc, MCPTimeoutError) else "mcp_unavailable",
                tools=tools,
                tool_calls=calls,
                servers=error_servers,
            ) from exc

    async def _execute_tool_call(
        self,
        mcp_session: ConnectedMCPRegistry,
        tool_call: dict[str, Any],
        allowed_tools: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        function = tool_call["function"]
        llm_tool_name = function["name"]
        call_id = tool_call["id"]
        route = allowed_tools.get(llm_tool_name)
        original_name = None if route is None else route.get("original_name", route.get("tool_name"))
        server_name = None if route is None else route.get("server_name")
        server_url = None if route is None else route.get("server_url")
        error: str | None = None
        result: dict[str, Any] | None = None
        started = time.monotonic()

        try:
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError):
            arguments = {}
            error = "LLM передала некорректные аргументы инструмента."

        if error is None and route is None:
            error = f"Инструмент «{llm_tool_name}» не предоставлен подключёнными MCP-серверами."

        log_event(logger, logging.INFO, "llm_tool_selected", tool_name=llm_tool_name)
        if error is None:
            log_event(logger, logging.INFO, "mcp_tool_call_started", tool_name=llm_tool_name)
            try:
                result = await mcp_session.call_tool(llm_tool_name, arguments)
                if result["is_error"]:
                    error = self._tool_error_text(result)
            except MCPClientError as exc:
                error = str(exc)

        duration_ms = round((time.monotonic() - started) * 1000)
        log_event(
            logger,
            logging.INFO,
            "mcp_tool_call_finished",
            tool_name=llm_tool_name,
            duration_ms=duration_ms,
            outcome="error" if error else "ok",
        )
        record = {
            "id": call_id,
            "name": original_name or llm_tool_name,
            "server_name": server_name,
            "server_url": server_url,
            "tool_name": original_name,
            "llm_tool_name": llm_tool_name,
            "arguments": arguments,
            "result": result,
            "error": error,
            "duration_ms": duration_ms,
        }
        tool_payload = {"error": error} if error else result
        return record, {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(tool_payload, ensure_ascii=False),
        }

    @staticmethod
    def _to_llm_tool(tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description") or "MCP tool",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }

    @staticmethod
    def _normalize_tool_call(raw: Any) -> dict[str, Any]:
        try:
            call_id = raw["id"]
            function = raw["function"]
            name = function["name"]
            arguments = function.get("arguments", "{}")
            if not all(isinstance(value, str) and value for value in (call_id, name)):
                raise TypeError
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            if not isinstance(arguments, str):
                raise TypeError
            return {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        except (KeyError, TypeError, AttributeError) as exc:
            raise AgentExecutionError(
                "LLM вернула некорректное описание вызова инструмента.",
                "llm_invalid_response",
            ) from exc

    @staticmethod
    def _tool_error_text(result: dict[str, Any]) -> str:
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                return str(item["text"])
        return "MCP-инструмент завершился контролируемой ошибкой."
