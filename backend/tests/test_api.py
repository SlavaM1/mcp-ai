from app.main import app, get_mcp_client


class FakeMCPClient:
    server_url = "https://mcp.example.test/mcp"

    async def list_tools(self):
        return [
            {
                "name": "ask_question",
                "description": "Ask about a repository",
                "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}},
            }
        ]


def test_api_creates_session(client):
    response = client.post("/api/sessions", json={"title": "Тестовый чат"})

    assert response.status_code == 201
    assert response.json()["title"] == "Тестовый чат"
    assert response.json()["messages"] == []


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
