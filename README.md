# mcp-ai

Persisted Angular chat with a Python agent that discovers and invokes tools through Model Context
Protocol. Four independent MCP servers provide weather, currency, deterministic trip analysis, and
Markdown report persistence. The LLM uses OpenAI-compatible DeepSeek chat completions with function
calling and owns orchestration together with the Agent loop.

## Architecture

```text
Browser
  -> nginx / Angular 20
  -> FastAPI agent
        -> OpenAI-compatible DeepSeek API
        -> MCP registry -> weather-mcp  -> wttr.in
                        -> currency-mcp -> open ExchangeRate-API
                        -> travel-mcp   -> deterministic local analysis
                        -> report-mcp   -> persistent Markdown files
        weather-mcp -> APScheduler -> wttr.in
                    -> SQLite weather schedules and samples
        -> SQLAlchemy -> PostgreSQL 17
```

For every chat turn the backend loads session history, opens a request-scoped MCP Streamable HTTP
session to every configured server, and calls `list_tools()` separately on each available server. It
merges the returned names, descriptions, and `inputSchema` values into one DeepSeek function catalog.
Only tools discovered during that request can be invoked. Tool calls are limited by
`MAX_TOOL_CALLS`; the final natural-language response and ordered technical MCP payload are stored
together in PostgreSQL.

The legacy `POST /api/sessions/{id}/tools/list` endpoint remains available for direct tool discovery
and existing saved payloads continue to render in the UI.

## Services

| Service | Purpose | Host port |
| --- | --- | --- |
| `frontend` | Production Angular build served by nginx | `4201` |
| `backend` | FastAPI Agent, DeepSeek client, MCP registry, persistence, Alembic | `8000` |
| `weather-mcp` | MCP 2.2 Streamable HTTP server and wttr.in client | `8001` |
| `currency-mcp` | Current rates and conversion via open ExchangeRate-API | `8002` |
| `travel-mcp` | Pure deterministic trip analysis from weather and currency payloads | `8003` |
| `report-mcp` | Safe persistent Markdown trip reports | `8004` |
| `db` | PostgreSQL 17 | internal only |
| `backend-test` | Optional pytest image | none |
| `weather-mcp-test` | Optional weather MCP unittest image | none |
| `currency-mcp-test` | Optional currency MCP unittest image | none |
| `travel-mcp-test` | Optional travel MCP unittest image | none |
| `report-mcp-test` | Optional report MCP unittest image | none |

| Server | Internal MCP endpoint | Published MCP endpoint | Health |
| --- | --- | --- | --- |
| `weather` | `http://weather-mcp:8001/mcp` | `http://localhost:8001/mcp` | `http://localhost:8001/health` |
| `currency` | `http://currency-mcp:8002/mcp` | `http://localhost:8002/mcp` | `http://localhost:8002/health` |
| `travel` | `http://travel-mcp:8003/mcp` | `http://localhost:8003/mcp` | `http://localhost:8003/health` |
| `report` | `http://report-mcp:8004/mcp` | `http://localhost:8004/mcp` | `http://localhost:8004/health` |

## Multi-MCP Orchestration

The cross-server dependency graph is:

```text
weather__get_weather_forecast ── weather_data ──┐
                                                ├─> travel__analyze_trip
currency__convert_currency ───── budget_data ───┘          │
                                                           └─ trip_analysis
                                                              -> report__save_trip_report
```

DeepSeek chooses whether tools are needed and which discovered functions to call; the generic Agent
loop executes those calls, returns every result to DeepSeek, and repeats until DeepSeek emits a final
answer. No MCP server calls another MCP server. There is no hardcoded intent classifier, keyword
router, travel pipeline, or special case that forces the four calls. A weather-only or currency-only
request therefore uses only the relevant server, and a trip analysis that does not ask for a file
stops after `travel__analyze_trip`.

### Discovery, Routing, and Security Mapping

Discovery and routing work as follows:

1. The registry connects to each entry in `MCP_SERVERS` and performs dynamic `list_tools()` discovery
   per server for the current API request.
2. The Agent exposes each validated name to DeepSeek as `<server>__<tool>`, preventing collisions
   between identically named tools on different servers.
3. An internal, non-LLM registry maps that namespaced name to the connected session and original tool
   name. For example, `currency__convert_currency` is routed to `currency-mcp` as
   `convert_currency`; an undiscovered or fabricated name is rejected rather than routed.
4. After each call, the complete MCP result, including `structured_content`, is sent back to DeepSeek.
   For the trip chain, the exact weather `structured_content` becomes `weather_data`, the exact
   currency conversion `structured_content` becomes `budget_data`, and the exact travel
   `structured_content` becomes `trip_analysis`. The Agent does not synthesize, summarize, or
   transform those objects between servers.

If one server cannot connect or list tools, healthy catalogs remain usable and aggregate status is
`partial`. The Agent tells DeepSeek which servers are unavailable so it can provide a clear limitation
instead of inventing data. A runtime tool failure is recorded as a controlled result and the loop can
continue where useful. Only when no server provides a catalog does the registry return aggregate
`unavailable`.

Security boundaries are enforced at the registry mapping: server names and discovered tool names
must use a restricted ASCII pattern, server names cannot contain the `__` separator, LLM-visible names
are limited to 64 characters, duplicate/unsafe catalogs are rejected, and only the request's
discovered allowlist can be called. Credentials and query strings are removed from server URLs before
logging or persistence. Persisted call metadata makes the safe mapping auditable through
`llm_tool_name`, `server_name`, sanitized `server_url`, and original `tool_name`. Each MCP transport
also limits accepted `Host` values to its Compose name and local published address and allows no
browser origins. These controls do not replace authentication, which is not implemented.

### Discovered Tool Catalog

The current live catalog contains exactly 14 namespaced tools:

```text
weather__get_current_weather
weather__get_weather_forecast
weather__analyze_weather
weather__save_weather_report
weather__get_hourly_weather
weather__create_weather_schedule
weather__list_weather_schedules
weather__stop_weather_schedule
weather__run_weather_collection_now
weather__get_weather_summary
currency__get_exchange_rate
currency__convert_currency
travel__analyze_trip
report__save_trip_report
```

The ten weather tools retain their existing behavior: current weather, one-to-three-day forecast,
deterministic analysis, weather report saving, hourly forecast, schedule creation/listing/stopping,
immediate collection, and historical summary. All city values are trimmed, non-empty strings of at
most 100 characters. The Agent extracts the city from ordinary user text through DeepSeek function
calling; city names are not hardcoded. Temperatures are Celsius, humidity is percent, and wind speed
is km/h.

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

### Currency Provider

`currency-mcp` uses the open ExchangeRate-API endpoint
`https://open.er-api.com/v6/latest/{BASE}` without an API key. `get_exchange_rate` returns the rate for
one unit of the base currency plus provider and update time; `convert_currency` fetches the same live
rate and converts a finite non-negative amount. Currency codes are normalized to three uppercase
ASCII letters.

Provider JSON floating-point values are parsed as `Decimal`, multiplication is performed with decimal
arithmetic, and `converted_amount` is rounded to two places with `ROUND_HALF_UP`; monetary and rate
values are serialized as decimal strings. The rates are current informational reference data, not a
guaranteed bank, card, cash, or executable trading quote. Provider timeout, proxy, rate-limit, HTTP,
unsupported-code, missing-rate, and malformed-response failures are returned as controlled tool
errors without exposing upstream response details.

### Deterministic Travel Analysis

`travel-mcp` is a pure computation service: it makes no provider HTTP request and does not call an
LLM. `analyze_trip` validates the supplied weather forecast and currency conversion, then calculates
the period, weather aggregates, budget, flags, and stable recommendation codes. In the default
configuration:

- rain is expected when precipitation is greater than zero or conditions contain a recognized
  English/Russian rain or precipitation marker; recommendation `take_umbrella`;
- cold means minimum temperature below `10 C`; recommendation `take_warm_layer`;
- hot means maximum temperature above `30 C`; recommendation `take_water`;
- strong wind means maximum wind at least `40 km/h`; recommendation
  `prepare_for_strong_wind`.

The three numeric thresholds can be overridden inside the service environment with
`TRAVEL_COLD_THRESHOLD_C`, `TRAVEL_HOT_THRESHOLD_C`, and `TRAVEL_STRONG_WIND_THRESHOLD_KMH`.
Stock Compose uses the defaults and does not forward these three optional variables; inject them with
a Compose override when customization is required. At exactly `10 C` or `30 C`, cold/hot are false;
at exactly `40 km/h`, strong wind is true.

### Trip Report Persistence

`report-mcp` accepts only the complete prepared `travel__analyze_trip` object and the `markdown`
format. It fetches nothing and performs no trip analysis. It validates JSON-compatible structured
data, creates a safe transliterated destination slug, and uses a timestamp plus UUID so an existing
file is never overwritten. Reports are written under `/data/reports` in the separate named volume
`mcp-ai-report-mcp-reports`, so they survive `report-mcp` and Compose restarts.

### MCP Tool Composition

The existing Agent loop supports multiple sequential MCP calls during one chat turn. For example:

```text
weather__get_weather_forecast
    -> weather__analyze_weather
    -> weather__save_weather_report
```

Send one user message:

```text
Собери прогноз погоды по Новосибирску на 3 дня,
проанализируй данные и сохрани отчет в файл.
```

DeepSeek selects each tool from the schemas and descriptions returned by `ClientSession.list_tools()`.
The Agent executes the selected MCP call, returns its `structured_content` to DeepSeek, and continues
the same generic tool-calling loop. The output of `weather__get_weather_forecast` is passed in the
`weather_data` argument of `weather__analyze_weather`; that analysis is then passed in the `analysis`
argument of `weather__save_weather_report`. There is no hardcoded report pipeline or keyword
orchestration in the Agent.

`weather__analyze_weather` does not call wttr.in or an LLM. It calculates temperature, humidity,
wind, and precipitation aggregates with Python. `weather__save_weather_report` neither fetches nor
analyzes weather. It writes a uniquely named Markdown file under `/data/reports`, which is inside the
existing persistent `mcp-ai-weather-mcp-data` Docker volume. This legacy weather-report storage is
separate from the trip-report volume. The technical payload saved in PostgreSQL contains the ordered
`tool_calls` array with arguments, results, errors, duration, namespaced/original tool names, and
server mapping for every step.

### Background Weather Collection

`weather-mcp` runs APScheduler inside its own process. Once `weather__create_weather_schedule` creates a
schedule, collection continues independently of the browser, FastAPI agent, and LLM for as long as
the `weather-mcp` container runs. A job runs at most once concurrently (`max_instances=1`), provider
errors are logged without disabling it, and missed intervals are coalesced rather than replayed.

For a demonstration, ask the chat: `Начни собирать погоду в Новосибирске каждую минуту.` This maps to
`weather__create_weather_schedule` with `{"city":"Новосибирск","interval_seconds":60}`. The minimum supported
interval is 30 seconds. Later ask: `Дай сводку погоды по Новосибирску за последний час.` The agent can
select `weather__get_weather_summary`, which returns sample count, actual data range, and min/max/average
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
| `MAX_TOOL_CALLS` | `12` | Maximum MCP calls in one chat turn |
| `MCP_SERVERS` | four-server JSON map from `.env.example` | Server namespace to Streamable HTTP endpoint map |
| `MCP_SERVER_URL` | `http://weather-mcp:8001/mcp` | Legacy single-weather-server fallback when `MCP_SERVERS` is unset |
| `MCP_TIMEOUT_SECONDS` | `30` | Timeout for each MCP connection/call |
| `DATABASE_URL` | Compose PostgreSQL URL | SQLAlchemy URL |
| `CORS_ORIGINS` | `http://localhost:4201` | Comma-separated frontend origins accepted by FastAPI |
| `WEATHER_DATABASE_PATH` | `/data/weather.db` in Compose | SQLite file for weather schedules and samples |
| `WEATHER_REPORTS_DIRECTORY` | `/data/reports` in Compose | Directory for generated Markdown reports |
| `WEATHER_BASE_URL` | `https://wttr.in` | Weather provider base URL |
| `WEATHER_HTTP_TIMEOUT_SECONDS` | `15` | Timeout for one wttr.in request |
| `CURRENCY_BASE_URL` | `https://open.er-api.com/v6/latest` | Open ExchangeRate-API base URL |
| `CURRENCY_HTTP_TIMEOUT_SECONDS` | `10` | Timeout for one currency-provider request |
| `REPORTS_DIRECTORY` | `/data/reports` | Trip-report directory; stock Compose fixes it to the persistent volume mount |
| `TRAVEL_COLD_THRESHOLD_C` | `10` | Optional cold threshold when injected into `travel-mcp` |
| `TRAVEL_HOT_THRESHOLD_C` | `30` | Optional hot threshold when injected into `travel-mcp` |
| `TRAVEL_STRONG_WIND_THRESHOLD_KMH` | `40` | Optional strong-wind threshold when injected into `travel-mcp` |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | environment/default | Standard proxy settings |
| `FRONTEND_PORT`, `BACKEND_PORT` | `4201`, `8000` | Published UI and API ports |
| `WEATHER_MCP_PORT`, `CURRENCY_MCP_PORT` | `8001`, `8002` | Published provider MCP ports |
| `TRAVEL_MCP_PORT`, `REPORT_MCP_PORT` | `8003`, `8004` | Published analysis and report MCP ports |

The normal four-server setting is a JSON object and should remain quoted where Compose embeds it:

```dotenv
MCP_SERVERS={"weather":"http://weather-mcp:8001/mcp","currency":"http://currency-mcp:8002/mcp","travel":"http://travel-mcp:8003/mcp","report":"http://report-mcp:8004/mcp"}
```

### Proxy Configuration

The backend and all four runtime MCP services receive `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`.
Weather and currency HTTP clients honor the standard proxy environment for external provider calls.
Keep `db`, `backend`, `frontend`, `weather-mcp`, `currency-mcp`, `travel-mcp`, `report-mcp`,
`localhost`, and `127.0.0.1` in `NO_PROXY` so internal MCP, database, and health traffic does not leave
the Compose network. `travel-mcp` and `report-mcp` have no provider egress in their tools, but receive
the same environment for consistent container behavior.

## Run

```bash
docker compose up --build -d
docker compose ps
```

Open:

- UI: <http://localhost:4201>
- Backend OpenAPI: <http://localhost:8000/docs>
- Backend health: <http://localhost:8000/health>
- Aggregate MCP status and per-server tool counts: <http://localhost:8000/api/mcp/status>
- Weather MCP health: <http://localhost:8001/health>
- Currency MCP health: <http://localhost:8002/health>
- Travel MCP health: <http://localhost:8003/health>
- Report MCP health: <http://localhost:8004/health>

PostgreSQL chat data is stored in the named volume `mcp-ai-postgres-data`. Weather schedules and
collected measurements are stored separately in SQLite at `/data/weather.db`; legacy weather reports
are stored under `/data/reports`. Both weather paths use `mcp-ai-weather-mcp-data`. Trip reports use
`/data/reports` in the independent `mcp-ai-report-mcp-reports` volume. All three named volumes persist
through a normal `docker compose down` followed by `docker compose up -d`; active weather schedules
are loaded and registered again when `weather-mcp` starts. Currency and travel are stateless. Stop
without removing the volumes:

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
- `POST /api/sessions/{id}/messages`: run one Agent/DeepSeek turn against the merged live catalog.
- `GET /api/mcp/status`: aggregate `ok`/`partial`/`unavailable`, total tool count, and each server's
  sanitized URL, status, tool count, and controlled error.
- `POST /api/sessions/{id}/tools/list`: direct four-server discovery retained from Day 16; it lists
  tools but does not invoke them.

## Tests

```bash
docker compose --profile test run --build --rm backend-test
docker compose --profile test run --build --rm weather-mcp-test
docker compose --profile test run --build --rm currency-mcp-test
docker compose --profile test run --build --rm travel-mcp-test
docker compose --profile test run --build --rm report-mcp-test
docker compose build frontend
```

Automated tests cover per-server discovery, namespacing and internal routing, unsafe/unadvertised tool
rejection, sanitized URL metadata, partial/all-server failure, Agent-selected and ordinary no-tool
turns, exact cross-server payload transfer, optional reporting, ordered PostgreSQL persistence, and
the tool-call limit. Service tests cover wttr.in normalization and errors, weather composition,
SQLite scheduler recovery and summaries, ExchangeRate-API validation and exact decimal conversion,
travel thresholds/recommendation codes, and safe unique trip-report rendering and persistence.

### Live Verification (2026-09-27)

Targeted checks against the running containers and real external services produced these factual
results today:

- `GET /api/mcp/status` reported all four servers `ok` and exactly 14 discovered tools.
- Real open ExchangeRate-API calls for `USD/EUR` and `RUB/TRY` succeeded. Volatile rate values are
  intentionally not recorded here.
- Real DeepSeek routing succeeded for weather-only, currency-only, combined weather+currency, the
  exact four-call order `weather__get_weather_forecast` -> `currency__convert_currency` ->
  `travel__analyze_trip` -> `report__save_trip_report`, and the no-report three-call order
  `weather__get_weather_forecast` -> `currency__convert_currency` -> `travel__analyze_trip`.
- Payload equality was checked across both dependency boundaries: weather and currency
  `structured_content` exactly matched `travel`'s `weather_data` and `budget_data`, and travel
  `structured_content` exactly matched report's `trip_analysis`.
- With `currency-mcp` unavailable, weather remained operational and a currency request returned a
  clear unavailable response rather than fabricated data.
- A saved trip report's SHA-256 remained unchanged across a `report-mcp` restart, confirming the
  mounted report volume retained the same file contents.
- Weather schedule creation succeeded, and the legacy three-step weather
  forecast/analyze/save composition also succeeded.

These are targeted live results, not a claim that every command in the full automated test matrix was
rerun in that verification session.

## Persistence

Alembic owns the existing `chat_sessions` and `chat_messages` schema. No new migration is needed for
multi-MCP: namespaced/original tool names, server mapping, arguments, result/error, duration,
discovered tools, and per-server statuses fit the existing JSONB `mcp_data` field. The named
PostgreSQL volume preserves sessions across rebuilds and restarts.

SQLite is deliberately local to `weather-mcp`; it owns `weather_schedules` and `weather_samples` only.
Weather Markdown reports share the weather MCP data volume but are not stored in SQLite. Trip
Markdown reports are owned by `report-mcp` in its own volume. None of these stores changes or replaces
the PostgreSQL chat schema.

## Limitations

- A configured external OpenAI-compatible model with tool-calling support is required for chat.
- wttr.in, open ExchangeRate-API, and the configured LLM are external dependencies without a project
  SLA; rates are informational and can differ from executable provider quotes.
- wttr.in exposes at most a three-day forecast to this project; trip analysis requires both a valid
  weather forecast and currency conversion payload.
- `travel-mcp` applies fixed deterministic rules rather than comprehensive travel planning, and
  `report-mcp` supports Markdown only.
- Authentication and multi-user isolation are not implemented.
- Partial availability preserves independent tools but cannot complete a chain whose required
  upstream server is unavailable.
- MCP and LLM connections are request-scoped; orchestration calls are executed sequentially, and
  requests are not queued or streamed to the UI.
