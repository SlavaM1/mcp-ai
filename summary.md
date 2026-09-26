# Краткое описание mcp-ai

`mcp-ai` - учебный полнофункциональный проект чата, в котором Angular UI обращается к FastAPI Agent,
Agent использует OpenAI-compatible DeepSeek API для выбора инструментов, а сами инструменты
динамически обнаруживаются и вызываются через MCP `ClientSession`.

## Архитектура

```text
Angular UI
    -> FastAPI Agent
    -> OpenAI-compatible DeepSeek API
    -> MCP ClientSession
    -> weather-mcp
```

История чатов и технические MCP payload сохраняются в PostgreSQL. `weather-mcp` получает данные из
wttr.in, а расписания и собранная погодная история хранятся в SQLite. Docker Compose объединяет UI,
backend, MCP server и базы данных; named volumes сохраняют данные после обычного перезапуска.

## Что реализовано за время проекта

- Angular chat UI с сессиями, историей сообщений и просмотром технических MCP-вызовов.
- FastAPI API, SQLAlchemy persistence и Alembic-схема для PostgreSQL.
- Универсальный Agent loop с DeepSeek function calling, динамическим `list_tools()`, проверкой
  разрешённых tools и ограничением числа вызовов.
- Отдельный `weather-mcp` сервер с текущей погодой, дневным и почасовым прогнозом wttr.in.
- APScheduler для фонового сбора погоды, SQLite-история, управление расписаниями и агрегированные
  сводки.
- Композиция нескольких MCP tools в одном сообщении без hardcoded pipeline:
  `get_weather_forecast -> analyze_weather -> save_weather_report`.
- Детерминированный анализ прогноза без повторного запроса провайдера и без LLM внутри tool.
- Безопасные уникальные Markdown-отчёты в `/data/reports` с сохранением в Docker volume
  `mcp-ai-weather-mcp-data`.
- Ordered technical payload для нескольких вызовов с arguments, result, error, duration и sequence.
- Unit и integration tests для Agent, API, MCP schemas, weather normalization, scheduler, анализа,
  генерации отчётов и передачи данных между инструментами.

## Основной сценарий

Пользователь отправляет один запрос на получение, анализ и сохранение прогноза. DeepSeek сам выбирает
последовательность tools. Agent после каждого MCP-вызова возвращает модели структурированный
результат, и модель передаёт его в следующий tool. Итоговый отчёт остаётся в persistent storage, а
вся последовательность доступна в истории чата и Angular UI.
