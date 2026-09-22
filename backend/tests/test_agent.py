from contextlib import asynccontextmanager

import pytest

from app.agent import AgentExecutionError, ChatAgent


TOOL = {
    "name": "get_current_weather",
    "description": "Current weather",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


class FakeMCPClient:
    server_url = "http://weather-mcp.test/mcp"

    def __init__(self):
        self.calls = []

    @asynccontextmanager
    async def connect(self):
        yield self

    async def list_tools(self):
        return [TOOL]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {
            "is_error": False,
            "content": [{"type": "text", "text": "weather"}],
            "structured_content": {"city": "Новосибирск", "temperature": 12.4},
        }


class FakeLLMClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def ensure_configured(self):
        pass

    async def complete(self, messages, tools):
        self.requests.append((messages, tools))
        return next(self.responses)


@pytest.mark.asyncio
async def test_agent_discovers_calls_tool_and_returns_final_answer():
    mcp = FakeMCPClient()
    llm = FakeLLMClient(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "get_current_weather",
                            "arguments": '{"city":"Новосибирск"}',
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "Сейчас 12,4 °C.", "tool_calls": []},
        ]
    )

    result = await ChatAgent(mcp, llm, max_tool_calls=4).run([], "Какая погода?")

    assert result.content == "Сейчас 12,4 °C."
    assert mcp.calls == [("get_current_weather", {"city": "Новосибирск"})]
    assert result.tool_calls[0]["result"]["structured_content"]["temperature"] == 12.4
    assert llm.requests[0][1][0]["function"]["parameters"] == TOOL["input_schema"]
    assert llm.requests[1][0][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_ordinary_question_does_not_call_tool():
    mcp = FakeMCPClient()
    llm = FakeLLMClient(
        [{"role": "assistant", "content": "Два плюс два — четыре.", "tool_calls": []}]
    )

    result = await ChatAgent(mcp, llm, max_tool_calls=4).run([], "Сколько будет 2+2?")

    assert result.content == "Два плюс два — четыре."
    assert mcp.calls == []
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_agent_rejects_tool_not_returned_by_mcp():
    mcp = FakeMCPClient()
    llm = FakeLLMClient(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-unsafe",
                        "type": "function",
                        "function": {"name": "delete_database", "arguments": "{}"},
                    }
                ],
            },
            {"role": "assistant", "content": "Этот инструмент недоступен.", "tool_calls": []},
        ]
    )

    result = await ChatAgent(mcp, llm, max_tool_calls=4).run([], "Удали данные")

    assert mcp.calls == []
    assert "не предоставлен" in result.tool_calls[0]["error"]


@pytest.mark.asyncio
async def test_tool_call_batch_is_rejected_before_partial_execution():
    mcp = FakeMCPClient()
    tool_call = {
        "type": "function",
        "function": {"name": "get_current_weather", "arguments": '{"city":"Омск"}'},
    }
    llm = FakeLLMClient(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", **tool_call},
                    {"id": "call-2", **tool_call},
                ],
            }
        ]
    )

    with pytest.raises(AgentExecutionError, match="превысил"):
        await ChatAgent(mcp, llm, max_tool_calls=1).run([], "Сравни погоду")

    assert mcp.calls == []
