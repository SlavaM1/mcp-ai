from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BeforeValidator, Field
from pydantic_core import PydanticCustomError
from starlette.requests import Request
from starlette.responses import JSONResponse

from weather_mcp import __version__
from weather_mcp.logging_config import configure_json_logging
from weather_mcp.weather import WeatherResult, WeatherService

MAX_CITY_LENGTH = 100
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


@asynccontextmanager
async def lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    limits = httpx2.Limits(max_connections=100, max_keepalive_connections=20)
    async with httpx2.AsyncClient(
        timeout=HTTP_TIMEOUT,
        limits=limits,
        trust_env=True,
        headers={"User-Agent": f"weather-mcp/{__version__}"},
    ) as client:
        yield AppContext(weather=WeatherService(client))


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
