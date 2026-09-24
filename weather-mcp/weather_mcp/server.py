from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from os import getenv
from typing import Annotated, Any

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from pydantic_core import PydanticCustomError
from starlette.requests import Request
from starlette.responses import JSONResponse

from weather_mcp import __version__
from weather_mcp.logging_config import configure_json_logging
from weather_mcp.persistence import WeatherRepository, WeatherSample, WeatherSchedule, to_timestamp, utc_now
from weather_mcp.scheduler import WeatherScheduler
from weather_mcp.weather import WeatherResult, WeatherService

MAX_CITY_LENGTH = 100
MIN_INTERVAL_SECONDS = 30
HTTP_TIMEOUT = httpx2.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)


def _normalize_city(value: Any) -> str:
    if not isinstance(value, str):
        raise PydanticCustomError("city_invalid", "Город должен быть строкой.")
    city = value.strip()
    if not city:
        raise PydanticCustomError("city_invalid", "Город не должен быть пустым.")
    if len(city) > MAX_CITY_LENGTH:
        raise PydanticCustomError(
            "city_invalid",
            f"Название города должно содержать не более {MAX_CITY_LENGTH} символов.",
        )
    return city


City = Annotated[
    str,
    BeforeValidator(_normalize_city),
    Field(
        min_length=1,
        max_length=MAX_CITY_LENGTH,
        description="Название города; пробелы по краям удаляются. Максимум 100 символов.",
    ),
]


@dataclass(frozen=True)
class AppContext:
    weather: WeatherService
    repository: WeatherRepository
    scheduler: WeatherScheduler


class ScheduleResult(BaseModel):
    schedule_id: str
    city: str
    interval_seconds: int
    active: bool
    created_at: str
    last_run_at: str | None
    next_run_at: str | None
    created: bool = Field(description="True for a new schedule; false when the active duplicate is returned.")


class ScheduleListResult(BaseModel):
    schedules: list[ScheduleResult]


class StopScheduleResult(BaseModel):
    schedule_id: str
    stopped: bool
    message: str


class WeatherSampleResult(BaseModel):
    city: str
    collected_at: str
    temperature_c: float
    relative_humidity_percent: int
    wind_speed_kmh: float


class WeatherMetrics(BaseModel):
    min: float
    max: float
    avg: float


class TemperatureMetrics(WeatherMetrics):
    first: float
    last: float


class WeatherSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    city: str
    period_minutes: int
    samples: int
    from_: str | None = Field(alias="from")
    to: str | None
    temperature: TemperatureMetrics | None
    relative_humidity_percent: WeatherMetrics | None
    wind_speed_kmh: WeatherMetrics | None
    message: str | None = None


@asynccontextmanager
async def lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    limits = httpx2.Limits(max_connections=100, max_keepalive_connections=20)
    async with httpx2.AsyncClient(
        timeout=HTTP_TIMEOUT,
        limits=limits,
        trust_env=True,
        headers={"User-Agent": f"weather-mcp/{__version__}"},
    ) as client:
        repository = WeatherRepository(getenv("WEATHER_DATABASE_PATH", "/data/weather.db"))
        scheduler = WeatherScheduler(repository, WeatherService(client))
        scheduler.start()
        try:
            yield AppContext(weather=WeatherService(client), repository=repository, scheduler=scheduler)
        finally:
            scheduler.shutdown()
            repository.close()


mcp = MCPServer[AppContext](
    "weather-mcp",
    description="Current weather from Open-Meteo",
    version=__version__,
    lifespan=lifespan,
)


@mcp.tool(
    description=(
        "Получить текущую погоду в городе через Open-Meteo. "
        "Температура возвращается в °C, влажность в %, скорость ветра в км/ч."
    )
)
async def get_current_weather(city: City, ctx: Context[AppContext, Any]) -> WeatherResult:
    return await ctx.request_context.lifespan_context.weather.get_current_weather(city)


@mcp.tool(
    description=(
        "Создать фоновое периодическое наблюдение за погодой города. Сбор выполняется самим "
        "weather-mcp по расписанию, даже когда чат закрыт. Минимальный интервал 30 секунд; "
        "для демонстрации передайте 60. Повторный вызов для того же города и интервала вернёт "
        "уже активное расписание, а не создаст дубликат."
    )
)
async def create_weather_schedule(
    city: City,
    interval_seconds: Annotated[
        int,
        Field(
            ge=MIN_INTERVAL_SECONDS,
            le=86_400,
            description="Интервал сбора в секундах, от 30 до 86400. Используйте 60 для одной минуты.",
        ),
    ],
    ctx: Context[AppContext, Any],
) -> ScheduleResult:
    schedule, created = ctx.request_context.lifespan_context.scheduler.create_schedule(city, interval_seconds)
    return _schedule_result(schedule, created=created)


@mcp.tool(
    description="Показать все сохранённые расписания фонового сбора погоды, включая остановленные."
)
async def list_weather_schedules(ctx: Context[AppContext, Any]) -> ScheduleListResult:
    schedules = ctx.request_context.lifespan_context.repository.list_schedules()
    return ScheduleListResult(schedules=[_schedule_result(schedule, created=False) for schedule in schedules])


@mcp.tool(
    description=(
        "Остановить периодическое наблюдение по его schedule_id. История измерений остаётся "
        "доступной для сводок."
    )
)
async def stop_weather_schedule(
    schedule_id: Annotated[str, Field(min_length=1, description="Идентификатор расписания из list_weather_schedules.")],
    ctx: Context[AppContext, Any],
) -> StopScheduleResult:
    schedule = ctx.request_context.lifespan_context.scheduler.stop_schedule(schedule_id)
    if schedule is None:
        return StopScheduleResult(
            schedule_id=schedule_id,
            stopped=False,
            message="Расписание с таким schedule_id не найдено.",
        )
    return StopScheduleResult(
        schedule_id=schedule_id,
        stopped=schedule.active is False,
        message="Расписание остановлено; накопленные измерения сохранены.",
    )


@mcp.tool(
    description=(
        "Немедленно запросить Open-Meteo и сохранить одно измерение в историю выбранного города. "
        "Используйте для проверки или дополнения данных, не создаёт расписание."
    )
)
async def run_weather_collection_now(
    city: City, ctx: Context[AppContext, Any]
) -> WeatherSampleResult:
    sample = await ctx.request_context.lifespan_context.scheduler.collect_now(city)
    return _sample_result(sample)


@mcp.tool(
    description=(
        "Получить агрегированную сводку по ранее сохранённым измерениям погоды города за последние "
        "minutes минут. Для текущей погоды без истории используйте get_current_weather."
    )
)
async def get_weather_summary(
    city: City,
    minutes: Annotated[
        int,
        Field(ge=1, le=525_600, description="Размер запрошенного периода в минутах, минимум 1."),
    ],
    ctx: Context[AppContext, Any],
) -> WeatherSummary:
    now = utc_now()
    samples = ctx.request_context.lifespan_context.repository.samples_since(
        city, now - timedelta(minutes=minutes), now
    )
    return _build_summary(city, minutes, samples)


def _build_summary(city: str, minutes: int, samples: list[WeatherSample]) -> WeatherSummary:
    if not samples:
        return WeatherSummary(
            city=city,
            period_minutes=minutes,
            samples=0,
            **{"from": None},
            to=None,
            temperature=None,
            relative_humidity_percent=None,
            wind_speed_kmh=None,
            message="За запрошенный период сохранённых измерений нет.",
        )
    temperatures = [sample.temperature_c for sample in samples]
    humidities = [sample.relative_humidity_percent for sample in samples]
    wind_speeds = [sample.wind_speed_kmh for sample in samples]
    return WeatherSummary(
        city=city,
        period_minutes=minutes,
        samples=len(samples),
        **{"from": to_timestamp(samples[0].collected_at)},
        to=to_timestamp(samples[-1].collected_at),
        temperature=TemperatureMetrics(
            min=min(temperatures),
            max=max(temperatures),
            avg=round(sum(temperatures) / len(temperatures), 1),
            first=temperatures[0],
            last=temperatures[-1],
        ),
        relative_humidity_percent=_metrics(humidities),
        wind_speed_kmh=_metrics(wind_speeds),
    )


def _schedule_result(schedule: WeatherSchedule, *, created: bool) -> ScheduleResult:
    return ScheduleResult(
        schedule_id=schedule.schedule_id,
        city=schedule.city,
        interval_seconds=schedule.interval_seconds,
        active=schedule.active,
        created_at=to_timestamp(schedule.created_at),
        last_run_at=to_timestamp(schedule.last_run_at) if schedule.last_run_at else None,
        next_run_at=to_timestamp(schedule.next_run_at) if schedule.next_run_at else None,
        created=created,
    )


def _sample_result(sample: WeatherSample) -> WeatherSampleResult:
    return WeatherSampleResult(
        city=sample.city,
        collected_at=to_timestamp(sample.collected_at),
        temperature_c=sample.temperature_c,
        relative_humidity_percent=sample.relative_humidity_percent,
        wind_speed_kmh=sample.wind_speed_kmh,
    )


def _metrics(values: list[float | int]) -> WeatherMetrics:
    return WeatherMetrics(
        min=min(values), max=max(values), avg=round(sum(values) / len(values), 1)
    )


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        allowed_hosts=["weather-mcp:8001", "localhost:8001", "127.0.0.1:8001"],
        allowed_origins=[],
    ),
)

configure_json_logging()
