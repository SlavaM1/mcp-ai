# Day 16 summary

- Implemented an Angular chat UI with persisted sessions, history, MCP status, tool descriptions,
  and input JSON Schemas.
- FastAPI uses the official Python MCP SDK and calls only `ClientSession.list_tools()` over
  Streamable HTTP.
- PostgreSQL stores `chat_sessions` and `chat_messages`; structured MCP data uses JSONB. Alembic owns
  the schema.
- Docker Compose runs `frontend` (nginx), `backend` (FastAPI), and `db` (PostgreSQL) with the named
  volume `mcp-ai-postgres-data`.
- Public server: `https://mcp.deepwiki.com/mcp` (DeepWiki, no authentication).
- Start: `docker compose up --build -d`.
- UI: `http://localhost:4201`; backend: `http://localhost:8000`; health: `/health`.
- Automated tests mock MCP and cover normalization, connection failure, session creation, persistence,
  and history retrieval. Final result: `4 passed`.
- Real DeepWiki verification returned 3 tools: `ask_question`, `read_wiki_contents`, and
  `read_wiki_structure`.
- Persistence was verified after `docker compose restart`: the same session retained both messages
  and the JSONB payload containing all 3 tools.
- Branch: `dev/day16`.
- Commit hash and push result are recorded in the external `~/ai/mcp-ai.md` after Git creates the
  commit containing this file.
- Current stage lists tools only. It neither invokes tools nor uses an LLM.
