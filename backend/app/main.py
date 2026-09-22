import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from .agent import AgentExecutionError, ChatAgent
from .config import get_settings
from .database import get_db
from .llm_client import OpenAICompatibleClient
from .mcp_client import (
    MCPClientError,
    MCPInvalidResponseError,
    MCPTimeoutError,
    MCPToolsClient,
)
from .models import ChatMessage, ChatSession, MessageRole
from .schemas import (
    AgentMessageRequest,
    AgentMessageResponse,
    MCPStatus,
    MessageRead,
    SessionCreate,
    SessionRead,
    SessionSummary,
    ToolListRequest,
    ToolListResponse,
)

logger = logging.getLogger(__name__)
settings = get_settings()
app = FastAPI(title="mcp-ai API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def get_mcp_client() -> MCPToolsClient:
    return MCPToolsClient(settings.mcp_server_url, settings.mcp_timeout_seconds)


def get_llm_client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        settings.llm_api_key,
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_timeout_seconds,
    )


def _session_query(session_id: uuid.UUID):
    return (
        select(ChatSession)
        .where(ChatSession.id == session_id)
        .options(selectinload(ChatSession.messages))
    )


def _session_read(chat_session: ChatSession) -> SessionRead:
    return SessionRead(
        id=chat_session.id,
        title=chat_session.title,
        created_at=chat_session.created_at,
        updated_at=chat_session.updated_at,
        message_count=len(chat_session.messages),
        messages=[MessageRead.model_validate(message) for message in chat_session.messages],
    )


def _load_session(db: Session, session_id: uuid.UUID) -> ChatSession:
    chat_session = db.scalar(_session_query(session_id))
    if chat_session is None:
        raise HTTPException(status_code=404, detail="Чат-сессия не найдена.")
    return chat_session


def _save_exchange(
    db: Session,
    chat_session: ChatSession,
    user_content: str,
    assistant_content: str,
    mcp_data: dict,
) -> ChatSession:
    try:
        if not chat_session.messages:
            chat_session.title = user_content[:157] + ("..." if len(user_content) > 157 else "")
        chat_session.messages.extend(
            [
                ChatMessage(role=MessageRole.USER, content=user_content),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=assistant_content,
                    mcp_data=mcp_data,
                ),
            ]
        )
        chat_session.updated_at = func.now()
        db.commit()
        return _load_session(db, chat_session.id)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Failed to save MCP exchange")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Не удалось сохранить сообщения в базе данных.",
        ) from exc


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.exception("Database healthcheck failed")
        raise HTTPException(status_code=503, detail="База данных недоступна.") from exc
    llm_configured = all((settings.llm_api_key, settings.llm_base_url, settings.llm_model))
    return {
        "status": "ok",
        "database": "connected",
        "llm": "configured" if llm_configured else "not_configured",
    }


@app.post("/api/sessions", response_model=SessionRead, status_code=201)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)) -> SessionRead:
    chat_session = ChatSession(title=payload.title.strip())
    try:
        db.add(chat_session)
        db.commit()
        db.refresh(chat_session)
        return _session_read(chat_session)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Не удалось создать чат-сессию.") from exc


@app.get("/api/sessions", response_model=list[SessionSummary])
def list_sessions(db: Session = Depends(get_db)) -> list[SessionSummary]:
    statement = (
        select(ChatSession, func.count(ChatMessage.id))
        .outerjoin(ChatMessage)
        .group_by(ChatSession.id)
        .order_by(ChatSession.updated_at.desc())
    )
    try:
        return [
            SessionSummary(
                id=chat_session.id,
                title=chat_session.title,
                created_at=chat_session.created_at,
                updated_at=chat_session.updated_at,
                message_count=message_count,
            )
            for chat_session, message_count in db.execute(statement).all()
        ]
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Не удалось загрузить чат-сессии.") from exc


@app.get("/api/sessions/{session_id}", response_model=SessionRead)
def get_session(session_id: uuid.UUID, db: Session = Depends(get_db)) -> SessionRead:
    return _session_read(_load_session(db, session_id))


@app.get("/api/mcp/status", response_model=MCPStatus)
async def mcp_status(client: MCPToolsClient = Depends(get_mcp_client)) -> MCPStatus:
    try:
        tools = await client.list_tools()
        return MCPStatus(
            connected=True,
            server_url=client.server_url,
            tool_count=len(tools),
        )
    except MCPClientError as exc:
        return MCPStatus(connected=False, server_url=client.server_url, error=str(exc))


@app.post("/api/sessions/{session_id}/messages", response_model=AgentMessageResponse)
async def send_agent_message(
    session_id: uuid.UUID,
    payload: AgentMessageRequest,
    db: Session = Depends(get_db),
    mcp_client: MCPToolsClient = Depends(get_mcp_client),
    llm_client: OpenAICompatibleClient = Depends(get_llm_client),
) -> AgentMessageResponse:
    chat_session = _load_session(db, session_id)
    user_content = payload.message.strip()
    agent = ChatAgent(mcp_client, llm_client, settings.max_tool_calls)
    db.commit()
    try:
        result = await agent.run(chat_session.messages, user_content)
    except AgentExecutionError as exc:
        mcp_data = {
            "status": "error",
            "server_url": mcp_client.server_url,
            "error": str(exc),
            "error_category": exc.category,
            "available_tools": exc.tools,
            "tool_calls": exc.tool_calls,
        }
        _save_exchange(db, chat_session, user_content, str(exc), mcp_data)
        response_status = {
            "llm_configuration": status.HTTP_503_SERVICE_UNAVAILABLE,
            "mcp_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
            "mcp_unavailable": status.HTTP_502_BAD_GATEWAY,
            "llm_unavailable": status.HTTP_502_BAD_GATEWAY,
            "llm_invalid_response": status.HTTP_502_BAD_GATEWAY,
            "tool_limit": status.HTTP_502_BAD_GATEWAY,
        }.get(exc.category, status.HTTP_502_BAD_GATEWAY)
        raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    has_tool_error = any(call.get("error") for call in result.tool_calls)
    mcp_data = {
        "status": "tool_error" if has_tool_error else "ok",
        "server_url": mcp_client.server_url,
        "available_tools": result.tools,
        "tool_calls": result.tool_calls,
    }
    saved_session = _save_exchange(
        db,
        chat_session,
        user_content,
        result.content,
        mcp_data,
    )
    return AgentMessageResponse(
        session=_session_read(saved_session),
        server_url=mcp_client.server_url,
        tool_calls=result.tool_calls,
    )


@app.post("/api/sessions/{session_id}/tools/list", response_model=ToolListResponse)
async def list_mcp_tools(
    session_id: uuid.UUID,
    payload: ToolListRequest,
    db: Session = Depends(get_db),
    client: MCPToolsClient = Depends(get_mcp_client),
) -> ToolListResponse:
    chat_session = _load_session(db, session_id)
    user_content = payload.message.strip()
    try:
        tools = await client.list_tools()
    except MCPClientError as exc:
        assistant_content = f"Не удалось получить список MCP-инструментов: {exc}"
        _save_exchange(
            db,
            chat_session,
            user_content,
            assistant_content,
            {"status": "error", "server_url": client.server_url, "error": str(exc)},
        )
        response_status = 504 if isinstance(exc, MCPTimeoutError) else 502
        if isinstance(exc, MCPInvalidResponseError):
            response_status = 502
        raise HTTPException(status_code=response_status, detail=assistant_content) from exc

    assistant_content = (
        f"MCP-сервер вернул {len(tools)} "
        f"{_tools_word(len(tools))}. Ни один инструмент не был вызван."
    )
    saved_session = _save_exchange(
        db,
        chat_session,
        user_content,
        assistant_content,
        {"status": "ok", "server_url": client.server_url, "tools": tools},
    )
    return ToolListResponse(
        session=_session_read(saved_session),
        tools=tools,
        server_url=client.server_url,
        connected=True,
    )


def _tools_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "инструмент"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "инструмента"
    return "инструментов"
