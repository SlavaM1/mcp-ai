# mcp-ai

`mcp-ai` is a small persisted chat client that discovers tools exposed by a remote Model Context
Protocol server. The current stage connects to a public MCP endpoint with the official Python SDK,
executes only `tools/list`, renders each tool and its input JSON Schema, and stores the complete
user/assistant exchange in PostgreSQL.

The UI follows the visual structure of the local `web_llm` project: a compact connection header,
saved chat history, a central message stream, and a fixed composer. It is implemented independently
and does not include files, secrets, build output, or Git metadata from the reference project.

## Current scope

- Gets the MCP tool list through the official `mcp` Python package.
- Does **not** execute any MCP tool.
- Does **not** use an LLM or generate model responses.
- Stores chat sessions, user requests, assistant responses, server metadata, and tool schemas.

## Architecture

```text
Browser
  -> nginx / Angular 20
  -> /api reverse proxy
  -> FastAPI
      -> official MCP ClientSession -> DeepWiki Streamable HTTP
      -> SQLAlchemy -> PostgreSQL 17
```

The backend opens a request-scoped Streamable HTTP connection, initializes an MCP `ClientSession`,
calls `list_tools()`, and normalizes the SDK models into JSON. A successful user request and its
assistant response are committed in one database transaction. On an MCP error, a readable assistant
error is also persisted before the API returns `502` or `504`.

## Public MCP server

The default is the official public DeepWiki endpoint:

```text
https://mcp.deepwiki.com/mcp
```

DeepWiki documents this endpoint as free, no-auth, and Streamable HTTP. It exposes tools for reading
and querying documentation for public GitHub repositories. The endpoint can be replaced using
`MCP_SERVER_URL` without changing code. External availability and rate limits are controlled by the
provider and have no project SLA.

## Containers

| Service | Purpose | Published port |
| --- | --- | --- |
| `frontend` | production Angular build served by nginx; proxies `/api` | `4201` |
| `backend` | FastAPI, MCP client, SQLAlchemy; runs Alembic on startup | `8000` |
| `db` | PostgreSQL 17 | internal only |
| `backend-test` | optional test image with pytest | none |

PostgreSQL data is kept in the named volume `mcp-ai-postgres-data`. Never use
`docker compose down -v` unless permanent data deletion is intended.

## Database

Alembic owns the schema; the application never calls `create_all()`.

- `chat_sessions`: UUID, title, creation time, update time.
- `chat_messages`: UUID, session UUID, constrained role (`user`, `assistant`, `system`), content,
  optional JSONB MCP payload, creation time.
- `chat_messages.session_id` uses `ON DELETE CASCADE` and is indexed.

## Configuration

All versions are pinned in `backend/requirements*.txt`, `frontend/package.json`, and the Dockerfiles.
Copying `.env.example` to `.env` is optional because Compose has the same safe local defaults.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_SERVER_URL` | `https://mcp.deepwiki.com/mcp` | remote Streamable HTTP endpoint |
| `MCP_TIMEOUT_SECONDS` | `30` | MCP HTTP and response timeout |
| `DATABASE_URL` | Compose PostgreSQL URL | SQLAlchemy connection URL |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | local development values | database bootstrap |
| `CORS_ORIGINS` | `http://localhost:4201` | direct backend CORS origin list |
| `FRONTEND_PORT`, `BACKEND_PORT` | `4201`, `8000` | host ports |

Do not commit `.env`; it is ignored by Git and Docker build context.

## Run

```bash
docker compose up --build -d
docker compose ps
```

Open:

- UI: <http://localhost:4201>
- Backend OpenAPI: <http://localhost:8000/docs>
- Backend readiness: <http://localhost:8000/health>

Stop without deleting PostgreSQL data:

```bash
docker compose down
```

## Tests

Run backend tests in Docker against PostgreSQL:

```bash
docker compose --profile test run --build --rm backend-test
```

The small test suite verifies MCP connection/session use and result normalization, connection error
translation, session creation, atomic storage of a user request plus tool response, and retrieval of
the stored history. The real external endpoint is intentionally mocked in automated tests.

Build the production frontend separately if needed:

```bash
docker compose build frontend
```

## API examples

Create a chat session:

```bash
curl -sS -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"title":"DeepWiki tools"}'
```

List sessions and load one history:

```bash
curl -sS http://localhost:8000/api/sessions
curl -sS http://localhost:8000/api/sessions/SESSION_UUID
```

Perform and persist a real `tools/list` exchange:

```bash
curl -sS -X POST http://localhost:8000/api/sessions/SESSION_UUID/tools/list \
  -H 'Content-Type: application/json' \
  -d '{"message":"Получить список MCP-инструментов"}'
```

Check MCP separately from backend readiness:

```bash
curl -sS http://localhost:8000/api/mcp/status
```

Temporary MCP failure does not fail `/health`; it is reported by `/api/mcp/status` and the tool-list
action. Database failures make `/health` return `503`.

## Persistence check

1. Create a session and request tools through the UI or API.
2. Record the session UUID and retrieve its history.
3. Run `docker compose restart`.
4. Wait for healthy services and request the same session again.
5. The same messages and JSONB tool payload must remain available.

The volume also survives `docker compose down` followed by `docker compose up -d`. Do not add `-v`.

## Known limitations

- Only `tools/list` is supported; pagination is not needed by the current DeepWiki response.
- MCP tools are displayed but cannot be invoked.
- There is no LLM, authentication, multi-user isolation, chat deletion, or server selector in the UI.
- The MCP connection is request-scoped rather than pooled.
- DeepWiki only works with public repositories and is an external dependency.
