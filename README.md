# mcp-ai

Persisted Angular chat with a Python agent that discovers and invokes tools through Model Context
Protocol. The bundled MCP server gets weather from wttr.in, returns normalized current, forecast,
and hourly data for any city supplied by the user, analyzes forecasts, and stores Markdown reports.
The LLM uses OpenAI-compatible chat completions with function calling.

## Architecture

```text
Browser
  -> nginx / Angular 20
  -> FastAPI agent
        -> OpenAI-compatible DeepSeek API
        -> MCP ClientSession -> weather-mcp -> wttr.in
                               -> APScheduler -> wttr.in
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
| `weather-mcp` | MCP 2.2 Streamable HTTP server and wttr.in client | `8001` |
| `db` | PostgreSQL 17 | internal only |
| `backend-test` | Optional pytest image | none |
| `weather-mcp-test` | Optional weather MCP unittest image | none |

Internal MCP endpoint: `http://weather-mcp:8001/mcp`.

Published MCP endpoint: `http://localhost:8001/mcp`.

The MCP server registers these tools dynamically, so the existing agent discovers all of them through
`ClientSession.list_tools()`:

- `get_current_weather`: current weather in the requested city without storing a sample.
- `get_weather_forecast`: normalized weather forecast for one to three days, including daily
  temperature, humidity, wind, precipitation, and conditions.
- `analyze_weather`: deterministic aggregates calculated from supplied structured forecast data.
- `save_weather_report`: save supplied weather analysis as a safe, uniquely named Markdown report.
- `get_hourly_weather`: normalized hourly forecast for the current or nearest day.
- `create_weather_schedule`: create background collection for a city and interval in seconds.
- `list_weather_schedules`: list active and stopped schedules.
- `stop_weather_schedule`: stop a schedule while retaining its history.
- `run_weather_collection_now`: fetch and persist a single measurement immediately.
- `get_weather_summary`: aggregate stored measurements for a requested period.

All city values are trimmed, non-empty strings of at most 100 characters. The Agent extracts the city
from ordinary user text through its existing DeepSeek function-calling flow; city names are not
hardcoded. Temperatures are Celsius, humidity is percent, and wind speed is km/h.

### Weather Provider

Weather provider: **wttr.in**

Endpoint: `https://wttr.in/{city}?format=j1`, for example
<https://wttr.in/Novosibirsk?format=j1>. An API key is not required. The provider request URL-encodes
city names and the HTTP client honors standard `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` settings.

Current weather includes resolved location, temperature, feels-like temperature, condition, humidity,
pressure, wind, precipitation, cloud cover, visibility, UV index, and provider observation time when
available. Forecast results include daily temperatures, average humidity, maximum wind, total
precipitation, conditions, sunrise/sunset, and moon phase. Hourly results include temperature,
feels-like temperature, condition, humidity, precipitation chance, and wind.

### MCP Tool Composition

The existing Agent loop supports multiple sequential MCP calls during one chat turn. For example:

```text
get_weather_forecast
    -> analyze_weather
    -> save_weather_report
```

Send one user message:

```text
Собери прогноз погоды по Новосибирску на 3 дня,
проанализируй данные и сохрани отчет в файл.
```

DeepSeek selects each tool from the schemas and descriptions returned by `ClientSession.list_tools()`.
The Agent executes the selected MCP call, returns its `structured_content` to DeepSeek, and continues
the same generic tool-calling loop. The output of `get_weather_forecast` is passed in the
`weather_data` argument of `analyze_weather`; that analysis is then passed in the `analysis` argument
of `save_weather_report`. There is no hardcoded report pipeline or keyword orchestration in the
Agent.

`analyze_weather` does not call wttr.in or an LLM. It calculates temperature, humidity, wind, and
precipitation aggregates with Python. `save_weather_report` neither fetches nor analyzes weather. It
writes a uniquely named Markdown file under `/data/reports`, which is inside the existing persistent
`mcp-ai-weather-mcp-data` Docker volume. The technical payload saved in PostgreSQL contains the
ordered `tool_calls` array with arguments, results, errors, and duration for every step.

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
| `WEATHER_REPORTS_DIRECTORY` | `/data/reports` in Compose | Directory for generated Markdown reports |
| `WEATHER_BASE_URL` | `https://wttr.in` | Weather provider base URL |
| `WEATHER_HTTP_TIMEOUT_SECONDS` | `15` | Timeout for one wttr.in request |
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
collected measurements are stored separately in SQLite at `/data/weather.db`; generated reports are
stored under `/data/reports`. Both paths are mounted through the named volume
`mcp-ai-weather-mcp-data`. All data persists through a normal `docker compose down` followed by
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
docker compose --profile test run --build --rm weather-mcp-test
docker compose build frontend
```

Automated tests cover MCP normalization and schemas, agent-selected and ordinary no-tool turns,
rejection of unadvertised tools, three-step composition and payload transfer, multi-call PostgreSQL
persistence, wttr.in parsing, URL encoding, error categories, forecast and hourly normalization,
analysis calculations, safe report generation, SQLite schedule/sample persistence, duplicate and
stopped schedules, scheduler recovery, provider errors, manual collection, and summary aggregation.
Final DeepSeek, wttr.in, report persistence, and scheduler verification should use the running
containers without mocks.

## Persistence

Alembic owns the existing `chat_sessions` and `chat_messages` schema. No Day 17 migration is needed:
tool name, arguments, result/error, duration, server URL, and discovered tools fit the existing JSONB
`mcp_data` field. The named PostgreSQL volume preserves sessions across rebuilds and restarts.

SQLite is deliberately local to `weather-mcp`; it owns `weather_schedules` and `weather_samples` only.
Markdown reports share the weather MCP data volume but are not stored in SQLite. Neither changes or
replaces the PostgreSQL chat schema.

## Limitations

- A configured external OpenAI-compatible model with tool-calling support is required for chat.
- wttr.in and the configured LLM are external dependencies without a project SLA.
- Authentication and multi-user isolation are not implemented.
- MCP and LLM connections are request-scoped; requests are not queued or streamed to the UI.
