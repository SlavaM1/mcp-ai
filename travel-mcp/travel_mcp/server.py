from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BeforeValidator, Field
from pydantic_core import PydanticCustomError
from starlette.requests import Request
from starlette.responses import JSONResponse

from travel_mcp import __version__
from travel_mcp.analysis import (
    CurrencyConversionResult,
    TripAnalysisResult,
    WeatherForecastResult,
    build_trip_analysis,
)

MAX_DESTINATION_LENGTH = 100


def _normalize_destination(value: Any) -> str:
    if not isinstance(value, str):
        raise PydanticCustomError(
            "destination_invalid", "Destination must be a string."
        )
    destination = value.strip()
    if not destination:
        raise PydanticCustomError(
            "destination_invalid", "Destination must not be empty."
        )
    if len(destination) > MAX_DESTINATION_LENGTH:
        raise PydanticCustomError(
            "destination_invalid",
            f"Destination must contain at most {MAX_DESTINATION_LENGTH} characters.",
        )
    return destination


Destination = Annotated[
    str,
    BeforeValidator(_normalize_destination),
    Field(
        min_length=1,
        max_length=MAX_DESTINATION_LENGTH,
        description="Trip destination; surrounding whitespace is removed. Maximum 100 characters.",
    ),
]


mcp = MCPServer(
    "travel-mcp",
    description="Deterministic trip analysis from previously obtained weather and currency data",
    version=__version__,
)


@mcp.tool(
    description=(
        "Analyze a trip using prior structured outputs supplied by the caller. "
        "Requires weather_data to be the output of weather-mcp get_weather_forecast and "
        "budget_data to be the output of currency-mcp convert_currency. "
        "This tool fetches nothing: it makes no HTTP/provider requests and does not call an LLM. "
        "It deterministically calculates weather flags, budget details, and recommendation codes "
        "from the supplied real inputs."
    )
)
async def analyze_trip(
    destination: Destination,
    weather_data: Annotated[
        WeatherForecastResult,
        Field(description="Prior structured output from get_weather_forecast."),
    ],
    budget_data: Annotated[
        CurrencyConversionResult,
        Field(description="Prior structured output from convert_currency."),
    ],
) -> TripAnalysisResult:
    return build_trip_analysis(destination, weather_data, budget_data)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        allowed_hosts=["travel-mcp:8003", "localhost:8003", "127.0.0.1:8003"],
        allowed_origins=[],
    ),
)
