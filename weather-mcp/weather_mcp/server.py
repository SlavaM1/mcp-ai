from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from os import getenv
from pathlib import Path
from typing import Annotated, Any, Literal

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
from weather_mcp.reporting import SavedReport, WeatherAnalysis, build_weather_analysis, write_weather_report
from weather_mcp.scheduler import WeatherScheduler
from weather_mcp.weather import (
    HourlyWeatherResult,
    WeatherForecastResult,
    WeatherResult,
    WeatherService,
    WttrWeatherClient,
)

MAX_CITY_LENGTH = 100
MIN_INTERVAL_SECONDS = 30


def _weather_timeout() -> httpx2.Timeout:
    try:
        seconds = float(getenv("WEATHER_HTTP_TIMEOUT_SECONDS", "15"))
    except ValueError:
        seconds = 15.0
    if seconds <= 0:
        seconds = 15.0
    return httpx2.Timeout(seconds)


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
    reports_directory: Path


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
    feels_like_c: float | None
    humidity_percent: int
    pressure_hpa: int | None
    wind_speed_kmh: float
    precipitation_mm: float | None
    cloud_cover_percent: int | None
    visibility_km: float | None
    condition: str | None


class WeatherMetrics(BaseModel):
    min: float
    max: float
    avg: float


class TemperatureMetrics(WeatherMetrics):
    first: float
    last: float


class PrecipitationMetrics(BaseModel):
    total: float
    max: float


class WeatherSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    city: str
    period_minutes: int
    samples: int
    from_: str | None = Field(alias="from")
    to: str | None
    temperature: TemperatureMetrics | None
    feels_like: WeatherMetrics | None
    humidity: WeatherMetrics | None
    pressure_hpa: WeatherMetrics | None
    wind_speed_kmh: WeatherMetrics | None
    precipitation_mm: PrecipitationMetrics | None
    conditions: list[str] | None = None
    message: str | None = None


@asynccontextmanager
async def lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    limits = httpx2.Limits(max_connections=100, max_keepalive_connections=20)
    async with httpx2.AsyncClient(
        timeout=_weather_timeout(),
        limits=limits,
        trust_env=True,
        headers={"User-Agent": f"weather-mcp/{__version__}"},
    ) as client:
        repository = WeatherRepository(getenv("WEATHER_DATABASE_PATH", "/data/weather.db"))
        weather = WeatherService(
            WttrWeatherClient(client, getenv("WEATHER_BASE_URL", "https://wttr.in"))
        )
        scheduler = WeatherScheduler(repository, weather)
        scheduler.start()
        try:
            yield AppContext(
                weather=weather,
                repository=repository,
                scheduler=scheduler,
                reports_directory=Path(getenv("WEATHER_REPORTS_DIRECTORY", "/data/reports")),
            )
        finally:
            scheduler.shutdown()
            repository.close()


mcp = MCPServer[AppContext](
    "weather-mcp",
    description="Weather data from wttr.in",
    version=__version__,
    lifespan=lifespan,
)


@mcp.tool(
    description=(
        "Получить текущую погоду в указанном пользователем городе через wttr.in. "
        "Возвращает температуру в °C, ощущаемую температуру, влажность, давление, ветер, "
        "осадки, облачность и видимость, а также фактически найденную локацию."
    )
)
async def get_current_weather(city: City, ctx: Context[AppContext, Any]) -> WeatherResult:
    return await ctx.request_context.lifespan_context.weather.get_current_weather(city)


@mcp.tool(
    description=(
        "Gets structured forecast data for a requested city and number of days. "
        "Use this when weather data is needed for further analysis or a report. "
        "days accepts 1 to 3, matching the forecast available from wttr.in."
    )
)
async def get_weather_forecast(
    city: City,
    days: Annotated[int, Field(ge=1, le=3, description="Число дней прогноза, от 1 до 3.")],
    ctx: Context[AppContext, Any],
) -> WeatherForecastResult:
    return await ctx.request_context.lifespan_context.weather.get_weather_forecast(city, days)


@mcp.tool(
    description=(
        "Analyzes structured weather forecast data supplied in weather_data. "
        "Use the structured output of get_weather_forecast as weather_data. "
        "Does not fetch weather itself. Returns deterministic aggregated weather statistics "
        "suitable for a report."
    )
)
async def analyze_weather(weather_data: WeatherForecastResult) -> WeatherAnalysis:
    return build_weather_analysis(weather_data)


@mcp.tool(
    description=(
        "Saves an already prepared weather analysis as a Markdown report. "
        "Use the structured output of analyze_weather as the analysis argument. "
        "Does not fetch or analyze weather itself."
    )
)
async def save_weather_report(
    analysis: WeatherAnalysis,
    ctx: Context[AppContext, Any],
    format: Annotated[
        Literal["markdown"],
        Field(description="Report format. Only markdown is supported."),
    ] = "markdown",
) -> SavedReport:
    del format
    return write_weather_report(
        analysis,
        ctx.request_context.lifespan_context.reports_directory,
    )


@mcp.tool(
    description=(
        "Получить почасовой прогноз погоды для текущего или ближайшего дня в указанном "
        "пользователем городе."
    )
)
async def get_hourly_weather(city: City, ctx: Context[AppContext, Any]) -> HourlyWeatherResult:
    return await ctx.request_context.lifespan_context.weather.get_hourly_weather(city)


@mcp.tool(
    description=(
        "Начать фоновый периодический сбор погоды для указанного пользователем города. Сбор выполняется самим "
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
        "Немедленно запросить wttr.in и сохранить одно измерение в историю выбранного города. "
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
        "Получить агрегированную статистику по ранее автоматически собранным данным погоды города за "
        "последние minutes минут. Для текущей погоды без истории используйте get_current_weather."
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
            feels_like=None,
            humidity=None,
            pressure_hpa=None,
            wind_speed_kmh=None,
            precipitation_mm=None,
            message="За запрошенный период сохранённых измерений нет.",
        )
    temperatures = [sample.temperature_c for sample in samples]
    feels_like = [sample.feels_like_c for sample in samples if sample.feels_like_c is not None]
    humidities = [sample.humidity_percent for sample in samples]
    pressures = [sample.pressure_hpa for sample in samples if sample.pressure_hpa is not None]
    wind_speeds = [sample.wind_speed_kmh for sample in samples]
    precipitation = [sample.precipitation_mm for sample in samples if sample.precipitation_mm is not None]
    conditions = list(dict.fromkeys(sample.condition for sample in samples if sample.condition))
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
        feels_like=_optional_metrics(feels_like),
        humidity=_metrics(humidities),
        pressure_hpa=_optional_metrics(pressures),
        wind_speed_kmh=_metrics(wind_speeds),
        precipitation_mm=(
            PrecipitationMetrics(total=round(sum(precipitation), 1), max=max(precipitation))
            if precipitation
            else None
        ),
        conditions=conditions or None,
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
        feels_like_c=sample.feels_like_c,
        humidity_percent=sample.humidity_percent,
        pressure_hpa=sample.pressure_hpa,
        wind_speed_kmh=sample.wind_speed_kmh,
        precipitation_mm=sample.precipitation_mm,
        cloud_cover_percent=sample.cloud_cover_percent,
        visibility_km=sample.visibility_km,
        condition=sample.condition,
    )


def _metrics(values: list[float | int]) -> WeatherMetrics:
    return WeatherMetrics(
        min=min(values), max=max(values), avg=round(sum(values) / len(values), 1)
    )


def _optional_metrics(values: list[float | int]) -> WeatherMetrics | None:
    return _metrics(values) if values else None


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
