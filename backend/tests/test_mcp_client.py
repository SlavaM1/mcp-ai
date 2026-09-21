from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.mcp_client import MCPConnectionError, MCPToolsClient


class FakeTool:
    def model_dump(self, **_kwargs):
        return {
            "name": "read_wiki_structure",
            "description": "Read repository topics",
            "inputSchema": {"type": "object", "required": ["repoName"]},
        }


@pytest.mark.asyncio
async def test_connects_and_normalizes_list_tools_result():
    state = {"connected": False, "listed": False}

    class FakeSession:
        async def list_tools(self):
            state["listed"] = True
            return SimpleNamespace(tools=[FakeTool()])

    @asynccontextmanager
    async def fake_session():
        state["connected"] = True
        yield FakeSession()

    tools = await MCPToolsClient("https://example.test/mcp", session_factory=fake_session).list_tools()

    assert state == {"connected": True, "listed": True}
    assert tools == [
        {
            "name": "read_wiki_structure",
            "description": "Read repository topics",
            "input_schema": {"type": "object", "required": ["repoName"]},
        }
    ]


@pytest.mark.asyncio
async def test_connection_error_is_translated():
    @asynccontextmanager
    async def broken_session():
        raise OSError("connection refused")
        yield

    client = MCPToolsClient("https://unavailable.test/mcp", session_factory=broken_session)

    with pytest.raises(MCPConnectionError, match="Не удалось подключиться"):
        await client.list_tools()
