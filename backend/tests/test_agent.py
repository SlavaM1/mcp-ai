from contextlib import asynccontextmanager

import pytest

from app.agent import AgentExecutionError, ChatAgent
from app.mcp_client import MCPConnectionError


def tool(server_name, original_name):
    llm_name = f"{server_name}__{original_name}"
    return {
        "name": llm_name,
        "llm_tool_name": llm_name,
        "original_name": original_name,
        "tool_name": original_name,
        "server_name": server_name,
        "server_url": f"https://{server_name}.example.test/mcp",
        "description": original_name,
        "input_schema": {"type": "object", "properties": {}},
    }


class FakeRegistry:
    server_url = "https://weather.example.test/mcp"

    def __init__(self, tools=None, results=None):
        self.tools = tools or [tool("weather", "get_current_weather")]
        self.results = results or {
            "weather__get_current_weather": {"city": "Новосибирск", "temperature": 12.4}
        }
        self.calls = []
        server_names = list(dict.fromkeys(item["server_name"] for item in self.tools))
        self._servers = [
            {
                "server_name": name,
                "server_url": f"https://{name}.example.test/mcp",
                "status": "ok",
                "tool_count": sum(item["server_name"] == name for item in self.tools),
                "error": None,
            }
            for name in server_names
        ]

    @property
    def servers(self):
        return [dict(item) for item in self._servers]

    @asynccontextmanager
    async def connect(self):
        yield self

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        result = self.results[name]
        if isinstance(result, Exception):
            for server in self._servers:
                if server["server_name"] == name.split("__", 1)[0]:
                    server.update(status="unavailable", error=str(result))
            raise result
        return {
            "is_error": False,
            "content": [{"type": "text", "text": "ok"}],
            "structured_content": result,
        }


class FakeLLMClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def ensure_configured(self):
        pass

    async def complete(self, messages, tools):
        self.requests.append((list(messages), tools))
        return next(self.responses)


def tool_call(call_id, name, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


@pytest.mark.asyncio
async def test_agent_discovers_calls_namespaced_tool_and_records_route():
    registry = FakeRegistry()
    llm = FakeLLMClient(
        [
            tool_call(
                "call-1",
                "weather__get_current_weather",
                '{"city":"Новосибирск"}',
            ),
            {"role": "assistant", "content": "Сейчас 12,4 °C.", "tool_calls": []},
        ]
    )

    result = await ChatAgent(registry, llm, max_tool_calls=12).run([], "Какая погода?")

    assert result.content == "Сейчас 12,4 °C."
    assert registry.calls == [
        ("weather__get_current_weather", {"city": "Новосибирск"})
    ]
    assert result.tool_calls[0] == {
        "id": "call-1",
        "name": "get_current_weather",
        "server_name": "weather",
        "server_url": "https://weather.example.test/mcp",
        "tool_name": "get_current_weather",
        "llm_tool_name": "weather__get_current_weather",
        "arguments": {"city": "Новосибирск"},
        "result": {
            "is_error": False,
            "content": [{"type": "text", "text": "ok"}],
            "structured_content": {"city": "Новосибирск", "temperature": 12.4},
        },
        "error": None,
        "duration_ms": result.tool_calls[0]["duration_ms"],
        "sequence": 1,
    }
    assert llm.requests[0][1][0]["function"]["name"] == "weather__get_current_weather"
    assert llm.requests[1][0][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_ordinary_question_does_not_call_tool():
    registry = FakeRegistry()
    llm = FakeLLMClient(
        [{"role": "assistant", "content": "Два плюс два — четыре.", "tool_calls": []}]
    )

    result = await ChatAgent(registry, llm, max_tool_calls=12).run([], "Сколько будет 2+2?")

    assert result.content == "Два плюс два — четыре."
    assert registry.calls == []


@pytest.mark.asyncio
async def test_unknown_namespaced_tool_is_controlled_and_never_routed():
    registry = FakeRegistry()
    llm = FakeLLMClient(
        [
            tool_call("call-unsafe", "unknown__delete_database", "{}"),
            {"role": "assistant", "content": "Этот инструмент недоступен.", "tool_calls": []},
        ]
    )

    result = await ChatAgent(registry, llm, max_tool_calls=12).run([], "Удали данные")

    assert registry.calls == []
    assert "не предоставлен" in result.tool_calls[0]["error"]
    assert result.tool_calls[0]["server_name"] is None


@pytest.mark.asyncio
async def test_invalid_arguments_are_returned_to_llm_as_tool_error():
    registry = FakeRegistry()
    llm = FakeLLMClient(
        [
            tool_call("call-invalid", "weather__get_current_weather", "not-json"),
            {"role": "assistant", "content": "Аргументы некорректны.", "tool_calls": []},
        ]
    )

    result = await ChatAgent(registry, llm, max_tool_calls=12).run([], "Какая погода?")

    assert registry.calls == []
    assert result.tool_calls[0]["arguments"] == {}
    assert "некорректные аргументы" in result.tool_calls[0]["error"]


@pytest.mark.asyncio
async def test_runtime_mcp_failure_is_returned_to_llm_and_status_is_retained():
    registry = FakeRegistry(
        results={"weather__get_current_weather": MCPConnectionError("weather offline")}
    )
    llm = FakeLLMClient(
        [
            tool_call("call-down", "weather__get_current_weather", {"city": "Омск"}),
            {"role": "assistant", "content": "Погода временно недоступна.", "tool_calls": []},
        ]
    )

    result = await ChatAgent(registry, llm, max_tool_calls=12).run([], "Какая погода?")

    assert result.content == "Погода временно недоступна."
    assert result.tool_calls[0]["error"] == "weather offline"
    assert result.servers[0]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_four_server_sequence_passes_actual_results_between_calls():
    forecast = {
        "requested_city": "Стамбул",
        "city": "Istanbul",
        "forecast": [{"date": "2026-09-27", "avg_temperature_c": 22}],
    }
    conversion = {
        "amount": "100000",
        "from_currency": "RUB",
        "to_currency": "TRY",
        "rate": "0.42",
        "converted_amount": "42000.00",
    }
    analysis = {
        "destination": "Стамбул",
        "weather": {"avg_temperature_c": 22},
        "budget": {"converted_amount": "42000.00", "destination_currency": "TRY"},
    }
    saved = {"saved": True, "path": "/data/reports/trip-report-istanbul.md"}
    tools = [
        tool("weather", "get_weather_forecast"),
        tool("currency", "convert_currency"),
        tool("travel", "analyze_trip"),
        tool("report", "save_trip_report"),
    ]
    registry = FakeRegistry(
        tools=tools,
        results={
            "weather__get_weather_forecast": forecast,
            "currency__convert_currency": conversion,
            "travel__analyze_trip": analysis,
            "report__save_trip_report": saved,
        },
    )
    llm = FakeLLMClient(
        [
            tool_call(
                "call-1",
                "weather__get_weather_forecast",
                {"city": "Стамбул", "days": 3},
            ),
            tool_call(
                "call-2",
                "currency__convert_currency",
                {"amount": 100000, "from_currency": "RUB", "to_currency": "TRY"},
            ),
            tool_call(
                "call-3",
                "travel__analyze_trip",
                {
                    "destination": "Стамбул",
                    "weather_data": forecast,
                    "budget_data": conversion,
                },
            ),
            tool_call(
                "call-4",
                "report__save_trip_report",
                {"trip_analysis": analysis, "format": "markdown"},
            ),
            {"role": "assistant", "content": "Отчёт сохранён.", "tool_calls": []},
        ]
    )

    result = await ChatAgent(registry, llm, max_tool_calls=12).run([], "Сделай отчёт")

    assert registry.calls[2][1]["weather_data"] == forecast
    assert registry.calls[2][1]["budget_data"] == conversion
    assert registry.calls[3][1]["trip_analysis"] == analysis
    assert [call["server_name"] for call in result.tool_calls] == [
        "weather",
        "currency",
        "travel",
        "report",
    ]
    assert result.tool_calls[-1]["result"]["structured_content"] == saved


@pytest.mark.asyncio
async def test_trip_analysis_without_report_does_not_call_report_server():
    forecast = {"city": "Istanbul", "forecast": [{"date": "2026-09-27"}]}
    conversion = {"amount": "100000", "converted_amount": "42000.00"}
    analysis = {"destination": "Стамбул", "weather": {}, "budget": {}}
    tools = [
        tool("weather", "get_weather_forecast"),
        tool("currency", "convert_currency"),
        tool("travel", "analyze_trip"),
        tool("report", "save_trip_report"),
    ]
    registry = FakeRegistry(
        tools=tools,
        results={
            "weather__get_weather_forecast": forecast,
            "currency__convert_currency": conversion,
            "travel__analyze_trip": analysis,
        },
    )
    llm = FakeLLMClient(
        [
            tool_call("call-1", "weather__get_weather_forecast", {"city": "Стамбул", "days": 3}),
            tool_call(
                "call-2",
                "currency__convert_currency",
                {"amount": 100000, "from_currency": "RUB", "to_currency": "TRY"},
            ),
            tool_call(
                "call-3",
                "travel__analyze_trip",
                {
                    "destination": "Стамбул",
                    "weather_data": forecast,
                    "budget_data": conversion,
                },
            ),
            {"role": "assistant", "content": "Анализ готов без файла.", "tool_calls": []},
        ]
    )

    result = await ChatAgent(registry, llm, max_tool_calls=12).run([], "Файл не сохраняй")

    assert result.content == "Анализ готов без файла."
    assert [name for name, _ in registry.calls] == [
        "weather__get_weather_forecast",
        "currency__convert_currency",
        "travel__analyze_trip",
    ]


@pytest.mark.asyncio
async def test_max_tool_calls_is_rejected_before_partial_batch_execution():
    registry = FakeRegistry()
    requested = tool_call(
        "unused",
        "weather__get_current_weather",
        {"city": "Омск"},
    )["tool_calls"][0]
    llm = FakeLLMClient(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {**requested, "id": f"call-{index}"}
                    for index in range(1, 14)
                ],
            }
        ]
    )

    with pytest.raises(AgentExecutionError, match="превысил") as raised:
        await ChatAgent(registry, llm, max_tool_calls=12).run([], "Сравни погоду")

    assert raised.value.category == "tool_limit"
    assert registry.calls == []
