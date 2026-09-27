from contextlib import asynccontextmanager

from app.main import app, get_llm_client, get_mcp_registry
from app.mcp_client import MCPConnectionError, MCPRegistry


def raw_tool(name):
    return {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }


class FakeConnectedTools:
    def __init__(self, server_name, tools, results, calls):
        self.server_name = server_name
        self.tools = tools
        self.results = results
        self.calls = calls

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((self.server_name, name, arguments))
        result = self.results[(self.server_name, name)]
        return {
            "is_error": False,
            "content": [{"type": "text", "text": "ok"}],
            "structured_content": result,
        }


class FakeTransportClient:
    def __init__(self, name, definition, results, calls):
        self.name = name
        self.definition = definition
        self.results = results
        self.calls = calls

    @asynccontextmanager
    async def connect(self):
        if isinstance(self.definition, Exception):
            raise self.definition
        yield FakeConnectedTools(self.name, self.definition, self.results, self.calls)


def make_registry(definitions, results=None):
    calls = []
    results = results or {}

    def factory(name, _url):
        return FakeTransportClient(name, definitions[name], results, calls)

    registry = MCPRegistry(
        {name: f"https://{name}.example.test/mcp" for name in definitions},
        client_factory=factory,
    )
    registry.calls = calls
    return registry


class FakeLLMClient:
    def __init__(self):
        self.request_count = 0

    def ensure_configured(self):
        pass

    async def complete(self, messages, tools):
        self.request_count += 1
        if self.request_count == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-api-1",
                        "type": "function",
                        "function": {
                            "name": "knowledge__ask_question",
                            "arguments": '{"question":"Ответь"}',
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "Итоговый ответ: 42.", "tool_calls": []}


class MultiToolLLMClient:
    def __init__(self):
        self.request_count = 0
        self.forecast = {"city": "Moscow", "forecast": [{"date": "2026-09-26"}]}
        self.analysis = {"city": "Moscow", "temperature": {"avg_c": 8}}

    def ensure_configured(self):
        pass

    async def complete(self, messages, tools):
        calls = [
            ("weather__get_weather_forecast", {"city": "Москва", "days": 2}),
            ("analytics__analyze_weather", {"weather_data": self.forecast}),
            ("storage__save_weather_report", {"analysis": self.analysis, "format": "markdown"}),
        ]
        if self.request_count < len(calls):
            name, arguments = calls[self.request_count]
            self.request_count += 1
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-api-{self.request_count}",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                ],
            }
        return {"role": "assistant", "content": "Отчёт сохранён.", "tool_calls": []}


def test_api_creates_session(client):
    response = client.post("/api/sessions", json={"title": "Тестовый чат"})

    assert response.status_code == 201
    assert response.json()["title"] == "Тестовый чат"
    assert response.json()["messages"] == []


def test_agent_message_rejects_whitespace(client):
    created = client.post("/api/sessions", json={"title": "Пустой запрос"}).json()

    response = client.post(
        f"/api/sessions/{created['id']}/messages",
        json={"message": "   "},
    )

    assert response.status_code == 422
    assert client.get(f"/api/sessions/{created['id']}").json()["messages"] == []


def test_status_and_list_return_partial_registry_catalog(client):
    registry = make_registry(
        {
            "knowledge": [raw_tool("ask_question")],
            "broken": MCPConnectionError("offline"),
        }
    )
    app.dependency_overrides[get_mcp_registry] = lambda: registry
    try:
        status = client.get("/api/mcp/status")
        assert status.status_code == 200
        assert status.json()["status"] == "partial"
        assert status.json()["connected"] is True
        assert status.json()["tool_count"] == 1
        assert status.json()["servers"][1]["error"] == "offline"

        created = client.post("/api/sessions", json={"title": "Новый MCP-чат"}).json()
        response = client.post(
            f"/api/sessions/{created['id']}/tools/list",
            json={"message": "Покажи инструменты"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "partial"
        assert body["tools"][0]["name"] == "knowledge__ask_question"
        assert body["tools"][0]["original_name"] == "ask_question"
        messages = body["session"]["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[-1]["mcp_data"]["servers"][1]["status"] == "unavailable"
    finally:
        app.dependency_overrides.clear()


def test_status_returns_unavailable_when_all_servers_are_down(client):
    registry = make_registry(
        {
            "one": MCPConnectionError("one offline"),
            "two": MCPConnectionError("two offline"),
        }
    )
    app.dependency_overrides[get_mcp_registry] = lambda: registry
    try:
        response = client.get("/api/mcp/status")

        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert response.json()["status"] == "unavailable"
        assert response.json()["tool_count"] == 0
        assert [server["status"] for server in response.json()["servers"]] == [
            "unavailable",
            "unavailable",
        ]
    finally:
        app.dependency_overrides.clear()


def test_agent_exchange_persists_servers_and_namespaced_route_metadata(client):
    registry = make_registry(
        {"knowledge": [raw_tool("ask_question")]},
        {("knowledge", "ask_question"): {"answer": "42"}},
    )
    app.dependency_overrides[get_mcp_registry] = lambda: registry
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient()
    try:
        created = client.post("/api/sessions", json={"title": "Агент"}).json()
        response = client.post(
            f"/api/sessions/{created['id']}/messages",
            json={"message": "Ответь через инструмент"},
        )

        assert response.status_code == 200
        body = response.json()
        call = body["session"]["messages"][-1]["mcp_data"]["tool_calls"][0]
        assert call["name"] == "ask_question"
        assert call["tool_name"] == "ask_question"
        assert call["llm_tool_name"] == "knowledge__ask_question"
        assert call["server_name"] == "knowledge"
        assert call["arguments"] == {"question": "Ответь"}
        assert call["result"]["structured_content"] == {"answer": "42"}
        assert body["servers"][0]["status"] == "ok"

        history = client.get(f"/api/sessions/{created['id']}").json()
        saved = history["messages"][-1]["mcp_data"]
        assert saved["tool_calls"][0]["id"] == "call-api-1"
        assert saved["servers"][0]["server_name"] == "knowledge"
    finally:
        app.dependency_overrides.clear()


def test_multiple_server_calls_are_saved_in_order_and_old_payload_still_loads(client):
    forecast = {"city": "Moscow", "forecast": [{"date": "2026-09-26"}]}
    analysis = {"city": "Moscow", "temperature": {"avg_c": 8}}
    registry = make_registry(
        {
            "weather": [raw_tool("get_weather_forecast")],
            "analytics": [raw_tool("analyze_weather")],
            "storage": [raw_tool("save_weather_report")],
        },
        {
            ("weather", "get_weather_forecast"): forecast,
            ("analytics", "analyze_weather"): analysis,
            ("storage", "save_weather_report"): {"saved": True, "file_name": "moscow.md"},
        },
    )
    app.dependency_overrides[get_mcp_registry] = lambda: registry
    app.dependency_overrides[get_llm_client] = lambda: MultiToolLLMClient()
    try:
        created = client.post("/api/sessions", json={"title": "Композиция"}).json()
        response = client.post(
            f"/api/sessions/{created['id']}/messages",
            json={"message": "Сделай погодный отчёт"},
        )

        assert response.status_code == 200
        tool_calls = response.json()["session"]["messages"][-1]["mcp_data"]["tool_calls"]
        assert [call["llm_tool_name"] for call in tool_calls] == [
            "weather__get_weather_forecast",
            "analytics__analyze_weather",
            "storage__save_weather_report",
        ]
        assert [call["sequence"] for call in tool_calls] == [1, 2, 3]
        assert registry.calls[1][2]["weather_data"] == forecast
        assert registry.calls[2][2]["analysis"] == analysis

        history = client.get(f"/api/sessions/{created['id']}").json()
        assert len(history["messages"][-1]["mcp_data"]["tool_calls"]) == 3

        legacy = client.post(
            f"/api/sessions/{created['id']}/tools/list",
            json={"message": "Покажи старый payload"},
        )
        assert legacy.status_code == 200
        assert legacy.json()["session"]["messages"][-1]["mcp_data"]["tools"]
    finally:
        app.dependency_overrides.clear()
