# Краткое описание mcp-ai

`mcp-ai` - учебный полнофункциональный чат: Angular UI обращается к FastAPI Agent, Agent вместе с
OpenAI-compatible DeepSeek динамически обнаруживает и оркестрирует инструменты четырёх независимых
MCP-серверов. История диалогов и технические payload сохраняются в PostgreSQL.

## История по дням

- **Day 16:** создан persisted MCP chat: Angular, nginx, FastAPI, официальный MCP SDK,
  `ClientSession.list_tools()`, PostgreSQL/SQLAlchemy/Alembic, JSONB с техническими данными и Docker
  Compose. На этом этапе приложение только показывало динамический каталог внешнего MCP.
- **Day 17:** добавлены DeepSeek function calling и универсальный Agent loop, локальный
  `weather-mcp`, вызов выбранных моделью tools, текущая/дневная/почасовая погода и сохранение полной
  трассы вызова. Текущий погодный провайдер - wttr.in.
- **Day 18:** реализованы APScheduler, SQLite-хранилище расписаний и измерений, создание/остановка
  фонового сбора, немедленный запуск, восстановление активных jobs и агрегированные сводки.
- **Day 19:** добавлена погодная композиция
  `get_weather_forecast -> analyze_weather -> save_weather_report`: данные передаются через
  `structured_content`, анализ детерминирован, Markdown-файлы безопасно сохраняются в weather volume.
- **Day 20:** один MCP-клиент заменён реестром четырёх серверов; появились namespacing,
  per-server discovery/status, partial failure, `currency-mcp`, `travel-mcp`, `report-mcp` и
  межсерверная цепочка погода + валюта -> анализ поездки -> отчёт.

## Текущая архитектура

```text
Angular 20 / nginx
    -> FastAPI Agent -> DeepSeek
                     -> MCP registry
                          -> weather-mcp  -> wttr.in, APScheduler, SQLite
                          -> currency-mcp -> open ExchangeRate-API
                          -> travel-mcp   -> pure deterministic analysis
                          -> report-mcp   -> persistent Markdown
    -> PostgreSQL 17: chats + ordered MCP metadata

weather__get_weather_forecast ─┐
                               ├─> travel__analyze_trip -> report__save_trip_report
currency__convert_currency ────┘
```

Agent открывает request-scoped сессию к каждому серверу и отдельно выполняет `list_tools()`. Реестр
показывает DeepSeek только проверенные имена `<server>__<tool>` и хранит внутреннее соответствие с
исходным сервером/tool. DeepSeek выбирает последовательность по schemas/descriptions, а Agent
исполняет её и возвращает модели полный результат каждого шага. Hardcoded intent routing и
фиксированного travel pipeline нет.

При межсерверной композиции исходный `structured_content` прогноза передаётся как `weather_data`,
конвертации как `budget_data`, а результат travel как `trip_analysis`; Agent их не пересчитывает и не
сокращает. Недоступность одного MCP даёт общий статус `partial`: остальные инструменты продолжают
работать, а модель получает явное уведомление о недоступном источнике.

## Сервисы и данные

| Сервис | Назначение | Порт/хранилище |
| --- | --- | --- |
| `frontend` | Angular UI, история и технические детали вызовов | `4201` |
| `backend` | FastAPI, DeepSeek Agent, MCP registry, SQLAlchemy/Alembic | `8000` |
| `weather-mcp` | 10 tools: погода, анализ/отчёт, scheduler/history | `8001`, `mcp-ai-weather-mcp-data` |
| `currency-mcp` | `get_exchange_rate`, `convert_currency` | `8002`, stateless |
| `travel-mcp` | `analyze_trip` по готовым weather/currency payload | `8003`, stateless |
| `report-mcp` | `save_trip_report` в Markdown | `8004`, `mcp-ai-report-mcp-reports` |
| `db` | Чаты и MCP metadata | `mcp-ai-postgres-data` |

Всего динамически обнаруживаются 14 namespaced tools. `currency-mcp` использует открытый
ExchangeRate-API без ключа; курсы информационные, а конвертация выполняется через `Decimal` с
`ROUND_HALF_UP` до двух знаков. `travel-mcp` не обращается к сети или LLM: применяет стабильные
пороги дождя, холода `<10 C`, жары `>30 C`, ветра `>=40 km/h` и возвращает recommendation codes.
`report-mcp` только форматирует готовый анализ, создаёт уникальное безопасное имя и сохраняет файл в
отдельном persistent volume. Старые погодные composition и scheduler остаются рабочими.

## Проверка Day 20

27 сентября 2026 года live-проверка подтвердила: все 4 MCP имеют статус `ok`, каталог содержит 14
tools; реальные запросы ExchangeRate-API `USD/EUR` и `RUB/TRY` успешны; DeepSeek корректно выбрал
weather-only, currency-only, комбинированный сценарий, полную цепочку
`weather__get_weather_forecast` -> `currency__convert_currency` -> `travel__analyze_trip` ->
`report__save_trip_report` и вариант без последнего шага. Проверено точное равенство payload на
границах weather/currency -> travel и travel -> report, работа погоды при outage currency, понятный
unavailable-ответ для валютного запроса, неизменный SHA-256 отчёта после restart `report-mcp`,
создание расписания и legacy weather composition. Это targeted live verification, а не утверждение о
повторном полном прогоне всех tests.
