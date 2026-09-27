from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from os import getenv
from typing import Annotated, Any

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, SkipValidation
from starlette.requests import Request
from starlette.responses import JSONResponse

from currency_mcp import __version__
from currency_mcp.currency import (
    CurrencyConversionResult,
    CurrencyService,
    ExchangeRateClient,
    ExchangeRateResult,
    normalize_amount,
    normalize_currency_code,
)
from currency_mcp.logging_config import configure_json_logging

DEFAULT_BASE_URL = "https://open.er-api.com/v6/latest"

CurrencyCode = Annotated[
    SkipValidation[str],
    Field(
        description="Three-letter currency code; surrounding whitespace is removed and letters are uppercased.",
        json_schema_extra={
            "minLength": 3,
            "maxLength": 3,
            "pattern": r"^[A-Z]{3}$",
        },
    ),
]
Amount = Annotated[
    SkipValidation[Decimal],
    Field(
        description="Finite, non-negative decimal amount. Zero is allowed.",
        json_schema_extra={"minimum": 0},
    ),
]


@dataclass(frozen=True)
class AppContext:
    currency: CurrencyService


def _currency_timeout() -> httpx2.Timeout:
    try:
        seconds = float(getenv("CURRENCY_HTTP_TIMEOUT_SECONDS", "10"))
    except ValueError:
        seconds = 10.0
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = 10.0
    return httpx2.Timeout(seconds)


@asynccontextmanager
async def lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    limits = httpx2.Limits(max_connections=100, max_keepalive_connections=20)
    async with httpx2.AsyncClient(
        timeout=_currency_timeout(),
        limits=limits,
        trust_env=True,
        headers={"User-Agent": f"currency-mcp/{__version__}"},
    ) as client:
        yield AppContext(
            currency=CurrencyService(
                ExchangeRateClient(client, getenv("CURRENCY_BASE_URL", DEFAULT_BASE_URL))
            )
        )


mcp = MCPServer[AppContext](
    "currency-mcp",
    description="Current exchange rates and deterministic currency conversion via ExchangeRate-API",
    version=__version__,
    lifespan=lifespan,
)


@mcp.tool(
    description=(
        "Get the current exchange rate for one unit of base_currency expressed in quote_currency. "
        "Use three-letter currency codes such as USD and EUR. Returns the provider update time and source."
    )
)
async def get_exchange_rate(
    base_currency: CurrencyCode,
    quote_currency: CurrencyCode,
    ctx: Context[AppContext, Any],
) -> ExchangeRateResult:
    base = normalize_currency_code(base_currency)
    quote = normalize_currency_code(quote_currency)
    return await ctx.request_context.lifespan_context.currency.get_exchange_rate(
        base, quote
    )


@mcp.tool(
    description=(
        "Convert a finite, non-negative amount from one currency to another using the current "
        "ExchangeRate-API rate. Uses decimal arithmetic and returns converted_amount rounded "
        "to two decimal places with half-up rounding."
    )
)
async def convert_currency(
    amount: Amount,
    from_currency: CurrencyCode,
    to_currency: CurrencyCode,
    ctx: Context[AppContext, Any],
) -> CurrencyConversionResult:
    normalized_amount = normalize_amount(amount)
    source = normalize_currency_code(from_currency)
    target = normalize_currency_code(to_currency)
    return await ctx.request_context.lifespan_context.currency.convert_currency(
        normalized_amount,
        source,
        target,
    )


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        allowed_hosts=["currency-mcp:8002", "localhost:8002", "127.0.0.1:8002"],
        allowed_origins=[],
    ),
)

configure_json_logging()
