# mcp-ai

Persisted Angular chat with a Python agent that discovers and invokes tools through Model Context
Protocol. The bundled MCP server resolves a city through Open-Meteo Geocoding, reads current weather
from Open-Meteo Forecast, and returns a structured result. The LLM uses OpenAI-compatible chat
completions with function calling.

## Architecture

```text
Browser
  -> nginx / Angular 20
  -> FastAPI agent
       -> OpenAI-compatible LLM
       -> MCP ClientSession -> weather-mcp -> Open-Meteo
                              -> APScheduler -> Open-Meteo
                              -> SQLite weather schedules and samples
       -> SQLAlchemy -> PostgreSQL 17
```

For every chat turn the backend loads session history, opens an MCP Streamable HTTP session, calls
`list_tools()`, converts the returned names, descriptions, and `inputSchema` values to LLM tools, and
lets the model decide whether to call one. Only names returned by that MCP session can be invoked.
Tool calls are limited by `MAX_TOOL_CALLS`; the final natural-language response and technical MCP
payload are stored together in PostgreSQL.

The legacy `POST /api/sessions/{id}/tools/list` endpoint remains available for direct tool discovery
and existing saved payloads continue to render in the UI.

## Services

| Service | Purpose | Host port |
| --- | --- | --- |
| `frontend` | Production Angular build served by nginx | `4201` |
| `backend` | FastAPI agent, MCP client, persistence, Alembic | `8000` |
| `weather-mcp` | MCP 2.2 Streamable HTTP server and Open-Meteo client | `8001` |
| `db` | PostgreSQL 17 | internal only |
| `backend-test` | Optional pytest image | none |

Internal MCP endpoint: `http://weather-mcp:8001/mcp`.

Published MCP endpoint: `http://localhost:8001/mcp`.

The MCP server registers these tools dynamically, so the existing agent discovers all of them through
`ClientSession.list_tools()`:

- `get_current_weather`: current Open-Meteo conditions without storing a sample.
- `create_weather_schedule`: create background collection for a city and interval in seconds.
- `list_weather_schedules`: list active and stopped schedules.
- `stop_weather_schedule`: stop a schedule while retaining its history.
- `run_weather_collection_now`: fetch and persist a single measurement immediately.
- `get_weather_summary`: aggregate stored measurements for a requested period.

All city values are trimmed, non-empty strings of at most 100 characters. Temperatures are Celsius,
relative humidity is percent, and wind speed is km/h.

### Background Weather Collection

`weather-mcp` runs APScheduler inside its own process. Once `create_weather_schedule` creates a
schedule, collection continues independently of the browser, FastAPI agent, and LLM for as long as
the `weather-mcp` container runs. A job runs at most once concurrently (`max_instances=1`), provider
errors are logged without disabling it, and missed intervals are coalesced rather than replayed.

For a demonstration, ask the chat: `Начни собирать погоду в Новосибирске каждую минуту.` This maps to
`create_weather_schedule` with `{"city":"Новосибирск","interval_seconds":60}`. The minimum supported
interval is 30 seconds. Later ask: `Дай сводку погоды по Новосибирску за последний час.` The agent can
select `get_weather_summary`, which returns sample count, actual data range, and min/max/average
metrics, including first and last temperature.

## Configuration

Create `.env` from `.env.example` and provide an OpenAI-compatible provider. For DeepSeek:

```dotenv
LLM_API_KEY=replace-with-your-key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
```

Select the model through `LLM_MODEL`. DeepSeek exposes `deepseek-chat` and
`deepseek-reasoner`; `deepseek-chat` is recommended for this agent's tool-calling flow. Recreate the
backend container after changing the value.

The key is never included in source, images, or committed Compose configuration. If any required LLM
value is absent, `/health` reports `"llm":"not_configured"` and chat requests return a clear `503`
diagnostic instead of reporting an MCP failure.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_API_KEY` | none | OpenAI-compatible API credential |
| `LLM_BASE_URL` | none | API root containing `/chat/completions` |
| `LLM_MODEL` | none | Tool-calling model name |
| `LLM_TIMEOUT_SECONDS` | `60` | Timeout for one LLM request |
| `MAX_TOOL_CALLS` | `4` | Maximum MCP calls in one chat turn |
| `MCP_SERVER_URL` | `http://weather-mcp:8001/mcp` | Streamable HTTP endpoint |
| `MCP_TIMEOUT_SECONDS` | `30` | MCP timeout |
| `DATABASE_URL` | Compose PostgreSQL URL | SQLAlchemy URL |
| `WEATHER_DATABASE_PATH` | `/data/weather.db` in Compose | SQLite file for weather schedules and samples |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | environment/default | Standard proxy settings |
| `FRONTEND_PORT`, `BACKEND_PORT`, `WEATHER_MCP_PORT` | `4201`, `8000`, `8001` | Published ports |

## Run

```bash
docker compose up --build -d
docker compose ps
```

Open:

- UI: <http://localhost:4201>
- Backend OpenAPI: <http://localhost:8000/docs>
- Backend health: <http://localhost:8000/health>
- Weather MCP health: <http://localhost:8001/health>

PostgreSQL chat data is stored in the named volume `mcp-ai-postgres-data`. Weather schedules and
collected measurements are stored separately in SQLite at `/data/weather.db`, mounted as the named
volume `mcp-ai-weather-mcp-data`. Both persist through a normal `docker compose down` followed by
`docker compose up -d`; active weather schedules are loaded and registered again when `weather-mcp`
starts. Stop without removing these volumes:

```bash
docker compose down
```

Do not use `docker compose down -v` unless permanent deletion is intended.

## API

Create a session and send a natural-language message:

```bash
curl -sS -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"title":"Погода"}'

curl -sS -X POST http://localhost:8000/api/sessions/SESSION_UUID/messages \
  -H 'Content-Type: application/json' \
  -d '{"message":"Какая сейчас погода в Новосибирске?"}'
```

Other endpoints:

- `GET /api/sessions` and `GET /api/sessions/{id}`: saved sessions and history.
- `GET /api/mcp/status`: live MCP connectivity and tool count.
- `POST /api/sessions/{id}/tools/list`: direct discovery retained from Day 16.

## Tests

```bash
docker compose --profile test run --build --rm backend-test
docker compose run --build --rm \
  -v "$PWD/weather-mcp/tests:/app/tests:ro" \
  weather-mcp python -m unittest discover -s tests
docker compose build frontend
```

Automated tests cover MCP normalization and schemas, agent-selected and ordinary no-tool turns,
rejection of unadvertised tools, PostgreSQL persistence, Open-Meteo parsing and error categories,
SQLite schedule/sample persistence, duplicate and stopped schedules, scheduler recovery, provider
errors, manual collection, and summary aggregation. Final Open-Meteo and scheduler verification
should use the running containers without mocks.

## Persistence

Alembic owns the existing `chat_sessions` and `chat_messages` schema. No Day 17 migration is needed:
tool name, arguments, result/error, duration, server URL, and discovered tools fit the existing JSONB
`mcp_data` field. The named PostgreSQL volume preserves sessions across rebuilds and restarts.

SQLite is deliberately local to `weather-mcp`; it owns `weather_schedules` and `weather_samples` only.
It does not alter or replace the PostgreSQL chat schema.

## Limitations

- A configured external OpenAI-compatible model with tool-calling support is required for chat.
- Open-Meteo and the configured LLM are external dependencies without a project SLA.
- Authentication and multi-user isolation are not implemented.
- MCP and LLM connections are request-scoped; requests are not queued or streamed to the UI.
