from contextlib import asynccontextmanager

from app.main import app, get_llm_client, get_mcp_client


class FakeMCPClient:
    server_url = "https://mcp.example.test/mcp"

    @asynccontextmanager
    async def connect(self):
        yield self

    async def list_tools(self):
        return [
            {
                "name": "ask_question",
                "description": "Ask about a repository",
                "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}},
            }
        ]

    async def call_tool(self, name, arguments):
        assert name == "ask_question"
        return {
            "is_error": False,
            "content": [{"type": "text", "text": "ok"}],
            "structured_content": {"answer": "42"},
        }


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
                            "name": "ask_question",
                            "arguments": '{"question":"Ответь"}',
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "Итоговый ответ: 42.", "tool_calls": []}


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


def test_tools_exchange_is_saved_and_returned_in_history(client):
    app.dependency_overrides[get_mcp_client] = lambda: FakeMCPClient()
    try:
        created = client.post("/api/sessions", json={"title": "Новый MCP-чат"}).json()
        response = client.post(
            f"/api/sessions/{created['id']}/tools/list",
            json={"message": "Покажи инструменты"},
        )

        assert response.status_code == 200
        assert response.json()["tools"][0]["name"] == "ask_question"
        assert [message["role"] for message in response.json()["session"]["messages"]] == [
            "user",
            "assistant",
        ]

        history = client.get(f"/api/sessions/{created['id']}")
        assert history.status_code == 200
        messages = history.json()["messages"]
        assert messages[0]["content"] == "Покажи инструменты"
        assert messages[1]["mcp_data"]["tools"][0]["name"] == "ask_question"
    finally:
        app.dependency_overrides.clear()


def test_agent_exchange_and_tool_payload_are_saved(client):
    app.dependency_overrides[get_mcp_client] = lambda: FakeMCPClient()
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient()
    try:
        created = client.post("/api/sessions", json={"title": "Агент"}).json()
        response = client.post(
            f"/api/sessions/{created['id']}/messages",
            json={"message": "Ответь через инструмент"},
        )

        assert response.status_code == 200
        messages = response.json()["session"]["messages"]
        assert messages[-1]["content"] == "Итоговый ответ: 42."
        call = messages[-1]["mcp_data"]["tool_calls"][0]
        assert call["name"] == "ask_question"
        assert call["arguments"] == {"question": "Ответь"}
        assert call["result"]["structured_content"] == {"answer": "42"}

        history = client.get(f"/api/sessions/{created['id']}").json()
        assert history["messages"][-1]["mcp_data"]["tool_calls"][0]["id"] == "call-api-1"
    finally:
        app.dependency_overrides.clear()
