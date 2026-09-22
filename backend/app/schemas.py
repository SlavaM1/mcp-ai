import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class SessionCreate(BaseModel):
    title: str = Field(default="Новый MCP-чат", min_length=1, max_length=160)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: Literal["user", "assistant", "system"]
    content: str
    mcp_data: dict[str, Any] | None
    created_at: datetime


class SessionSummary(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class SessionRead(SessionSummary):
    messages: list[MessageRead]


class ToolListRequest(BaseModel):
    message: str = Field(default="Получить список MCP-инструментов", min_length=1, max_length=2000)


class AgentMessageRequest(BaseModel):
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
    ]


class ToolDefinition(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] | None = None


class ToolListResponse(BaseModel):
    session: SessionRead
    tools: list[ToolDefinition]
    server_url: str
    connected: bool


class MCPStatus(BaseModel):
    connected: bool
    server_url: str
    tool_count: int | None = None
    error: str | None = None


class AgentMessageResponse(BaseModel):
    session: SessionRead
    server_url: str
    tool_calls: list[dict[str, Any]]
