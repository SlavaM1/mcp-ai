from contextlib import asynccontextmanager

import pytest

from app.config import Settings
from app.mcp_client import (
    ConnectedMCPRegistry,
    MCPConnectionError,
    MCPInvalidResponseError,
    MCPRegistry,
    MCPRegistryError,
    MCPUnknownToolError,
)


def tool(name):
    return {
        "name": name,
        "description": f"Tool {name}",
        "input_schema": {"type": "object", "properties": {}},
    }


class FakeConnectedTools:
    def __init__(self, name, tools, calls):
        self.name = name
        self.tools = tools
        self.calls = calls

    async def list_tools(self):
        if isinstance(self.tools, Exception):
            raise self.tools
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((self.name, name, arguments))
        return {
            "is_error": False,
            "content": [{"type": "text", "text": "ok"}],
            "structured_content": {"server": self.name, "arguments": arguments},
        }


class FakeClient:
    def __init__(self, name, tools, calls, closed, connection_error=None):
        self.name = name
        self.tools = tools
        self.calls = calls
        self.closed = closed
        self.connection_error = connection_error

    @asynccontextmanager
    async def connect(self):
        if self.connection_error:
            raise self.connection_error
        try:
            yield FakeConnectedTools(self.name, self.tools, self.calls)
        finally:
            self.closed.append(self.name)


def make_registry(definitions):
    calls = []
    closed = []

    def factory(name, _url):
        value = definitions[name]
        if isinstance(value, Exception):
            return FakeClient(name, [], calls, closed, connection_error=value)
        return FakeClient(name, value, calls, closed)

    servers = {name: f"https://{name}.example.test/mcp" for name in definitions}
    return MCPRegistry(servers, client_factory=factory), calls, closed


@pytest.mark.asyncio
async def test_registry_merges_namespaces_and_routes_only_discovered_tools():
    registry, calls, closed = make_registry(
        {"docs": [tool("search")], "issues": [tool("search")]}
    )

    async with registry.connect() as connected:
        tools = await connected.list_tools()
        assert [item["name"] for item in tools] == ["docs__search", "issues__search"]
        assert tools[0] == {
            "name": "docs__search",
            "llm_tool_name": "docs__search",
            "original_name": "search",
            "tool_name": "search",
            "server_name": "docs",
            "server_url": "https://docs.example.test/mcp",
            "description": "Tool search",
            "input_schema": {"type": "object", "properties": {}},
        }
        assert connected.route_metadata("issues__search")["server_name"] == "issues"

        await connected.call_tool("issues__search", {"query": "42"})
        with pytest.raises(MCPUnknownToolError):
            await connected.call_tool("unknown__search", {})
        with pytest.raises(MCPUnknownToolError):
            await connected.call_tool("docs__missing", {})

    assert calls == [("issues", "search", {"query": "42"})]
    assert set(closed) == {"docs", "issues"}


@pytest.mark.asyncio
async def test_registry_does_not_expose_url_credentials_in_metadata():
    calls = []
    closed = []

    def factory(name, _url):
        return FakeClient(name, [tool("search")], calls, closed)

    registry = MCPRegistry(
        {"docs": "https://user:secret@docs.example.test/mcp?access_token=hidden"},
        client_factory=factory,
    )

    tools = await registry.list_tools()

    assert registry.server_url == "https://docs.example.test/mcp"
    assert registry.servers[0]["server_url"] == "https://docs.example.test/mcp"
    assert tools[0]["server_url"] == "https://docs.example.test/mcp"


@pytest.mark.asyncio
async def test_registry_keeps_healthy_server_when_one_is_down():
    registry, _, closed = make_registry(
        {
            "weather": [tool("forecast")],
            "broken": MCPConnectionError("offline"),
        }
    )

    tools = await registry.list_tools()

    assert [item["name"] for item in tools] == ["weather__forecast"]
    assert registry.status == "partial"
    assert registry.connected is True
    assert registry.servers == [
        {
            "server_name": "weather",
            "server_url": "https://weather.example.test/mcp",
            "status": "ok",
            "tool_count": 1,
            "error": None,
        },
        {
            "server_name": "broken",
            "server_url": "https://broken.example.test/mcp",
            "status": "unavailable",
            "tool_count": 0,
            "error": "offline",
        },
    ]
    assert closed == ["weather"]


@pytest.mark.asyncio
async def test_registry_all_down_raises_controlled_aggregate_error():
    registry, _, _ = make_registry(
        {
            "one": MCPConnectionError("one down"),
            "two": MCPConnectionError("two down"),
        }
    )

    with pytest.raises(MCPRegistryError) as raised:
        await registry.list_tools()

    assert [item["error"] for item in raised.value.servers] == ["one down", "two down"]
    assert registry.status == "unavailable"


@pytest.mark.asyncio
async def test_duplicate_tool_in_one_server_rejects_its_catalog():
    registry, _, closed = make_registry({"docs": [tool("search"), tool("search")]})

    with pytest.raises(MCPRegistryError) as raised:
        await registry.list_tools()

    assert "повторяющееся" in raised.value.servers[0]["error"]
    assert closed == ["docs"]


def test_registry_rejects_unsafe_server_name_and_settings_support_legacy_fallback(monkeypatch):
    monkeypatch.delenv("MCP_SERVERS", raising=False)
    with pytest.raises(ValueError, match="Имя MCP-сервера"):
        MCPRegistry({"bad.name": "https://example.test/mcp"})

    legacy = Settings(
        mcp_server_url="https://legacy.example.test/mcp",
        mcp_servers=None,
    )
    configured = Settings(
        mcp_server_url="https://legacy.example.test/mcp",
        mcp_servers={"docs": "https://docs.example.test/mcp"},
    )

    assert legacy.configured_mcp_servers == {"weather": "https://legacy.example.test/mcp"}
    assert configured.configured_mcp_servers == {"docs": "https://docs.example.test/mcp"}
    assert Settings.model_fields["max_tool_calls"].default == 12


def test_settings_parse_mcp_servers_json_environment(monkeypatch):
    monkeypatch.setenv(
        "MCP_SERVERS",
        '{"docs":"https://docs.example.test/mcp","issues":"https://issues.example.test/mcp"}',
    )

    settings = Settings(_env_file=None)

    assert settings.configured_mcp_servers == {
        "docs": "https://docs.example.test/mcp",
        "issues": "https://issues.example.test/mcp",
    }


def test_registry_rejects_unsafe_discovered_name():
    session = ConnectedMCPRegistry(
        {"docs": "https://docs.example.test/mcp"},
        {},
        [],
    )
    with pytest.raises(MCPInvalidResponseError, match="небезопасное"):
        session._namespace_tools("docs", [tool("unsafe.name")])
