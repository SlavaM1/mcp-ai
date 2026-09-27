from __future__ import annotations

import json
import logging
from decimal import MAX_EMAX, Decimal, DecimalException, ROUND_HALF_UP, localcontext
from typing import Any

import httpx2
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

PROVIDER = "ExchangeRate-API"

INVALID_CURRENCY_CODE = "INVALID_CURRENCY_CODE"
INVALID_AMOUNT = "INVALID_AMOUNT"
PROVIDER_TIMEOUT = "CURRENCY_PROVIDER_TIMEOUT"
PROVIDER_PROXY_ERROR = "CURRENCY_PROVIDER_PROXY_ERROR"
PROVIDER_RATE_LIMITED = "CURRENCY_PROVIDER_RATE_LIMITED"
PROVIDER_HTTP_ERROR = "CURRENCY_PROVIDER_HTTP_ERROR"
PROVIDER_FAILURE = "CURRENCY_PROVIDER_FAILURE"
INVALID_RESPONSE = "CURRENCY_PROVIDER_INVALID_RESPONSE"
QUOTE_CURRENCY_NOT_FOUND = "QUOTE_CURRENCY_NOT_FOUND"
UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"

_ERROR_MESSAGES = {
    INVALID_CURRENCY_CODE: "Currency codes must contain exactly three ASCII letters.",
    INVALID_AMOUNT: "Amount must be a finite, non-negative decimal number.",
    PROVIDER_TIMEOUT: "The currency provider did not respond within the configured timeout.",
    PROVIDER_PROXY_ERROR: "The configured proxy could not connect to the currency provider.",
    PROVIDER_RATE_LIMITED: "The currency provider rate limit has been reached. Try again later.",
    PROVIDER_HTTP_ERROR: "The currency provider returned an HTTP error.",
    PROVIDER_FAILURE: "The currency provider could not complete the request.",
    INVALID_RESPONSE: "The currency provider returned a malformed response.",
    QUOTE_CURRENCY_NOT_FOUND: "The requested quote currency is not available from the provider.",
    UNSUPPORTED_CURRENCY: "The requested base currency is not supported by the provider.",
}

_CENT = Decimal("0.01")
logger = logging.getLogger(__name__)


class ExchangeRateResult(BaseModel):
    base_currency: str
    quote_currency: str
    rate: str
    as_of: str
    provider: str


class CurrencyConversionResult(BaseModel):
    amount: str
    from_currency: str
    to_currency: str
    rate: str
    converted_amount: str
    as_of: str
    provider: str


def normalize_currency_code(value: Any) -> str:
    if not isinstance(value, str):
        raise tool_error(INVALID_CURRENCY_CODE)
    code = value.strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise tool_error(INVALID_CURRENCY_CODE)
    return code


def normalize_amount(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise tool_error(INVALID_AMOUNT)
    try:
        if isinstance(value, Decimal):
            amount = value
        elif isinstance(value, (int, float)):
            amount = Decimal(str(value))
        elif isinstance(value, str) and value.strip():
            amount = Decimal(value.strip())
        else:
            raise ValueError
    except (DecimalException, ValueError):
        raise tool_error(INVALID_AMOUNT) from None
    if not amount.is_finite() or amount < 0:
        raise tool_error(INVALID_AMOUNT)
    return amount


class ExchangeRateClient:
    """Fetch and validate ExchangeRate-API responses behind a stable error contract."""

    def __init__(self, client: httpx2.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def get_rate(self, base_currency: str, quote_currency: str) -> ExchangeRateResult:
        base = normalize_currency_code(base_currency)
        quote = normalize_currency_code(quote_currency)
        payload = await self._fetch(base)

        result = payload.get("result")
        if not isinstance(result, str):
            raise tool_error(INVALID_RESPONSE)
        if result != "success":
            if payload.get("error-type") == "unsupported-code":
                raise tool_error(UNSUPPORTED_CURRENCY)
            raise tool_error(PROVIDER_FAILURE)
        provider_base = payload.get("base_code")
        if provider_base is not None and provider_base != base:
            raise tool_error(INVALID_RESPONSE)
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            raise tool_error(INVALID_RESPONSE)
        if quote not in rates:
            raise tool_error(QUOTE_CURRENCY_NOT_FOUND)

        rate = _provider_decimal(rates[quote])
        if rate <= 0:
            raise tool_error(INVALID_RESPONSE)
        as_of = _as_of(payload)
        return ExchangeRateResult(
            base_currency=base,
            quote_currency=quote,
            rate=decimal_text(rate),
            as_of=as_of,
            provider=PROVIDER,
        )

    async def _fetch(self, base_currency: str) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self._base_url}/{base_currency}")
            response.raise_for_status()
        except httpx2.TimeoutException:
            _log_provider_error("timeout")
            raise tool_error(PROVIDER_TIMEOUT) from None
        except httpx2.ProxyError:
            _log_provider_error("proxy")
            raise tool_error(PROVIDER_PROXY_ERROR) from None
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                _log_provider_error("rate_limit", status_code=429)
                raise tool_error(PROVIDER_RATE_LIMITED) from None
            _log_provider_error("http", status_code=exc.response.status_code)
            raise tool_error(PROVIDER_HTTP_ERROR) from None
        except httpx2.RequestError:
            _log_provider_error("network")
            raise tool_error(PROVIDER_HTTP_ERROR) from None

        try:
            payload = json.loads(response.content, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError, DecimalException):
            raise tool_error(INVALID_RESPONSE) from None
        if not isinstance(payload, dict):
            raise tool_error(INVALID_RESPONSE)
        return payload


class CurrencyService:
    def __init__(self, client: ExchangeRateClient) -> None:
        self._client = client

    async def get_exchange_rate(
        self, base_currency: str, quote_currency: str
    ) -> ExchangeRateResult:
        return await self._client.get_rate(base_currency, quote_currency)

    async def convert_currency(
        self, amount: Any, from_currency: str, to_currency: str
    ) -> CurrencyConversionResult:
        normalized_amount = normalize_amount(amount)
        rate_result = await self._client.get_rate(from_currency, to_currency)
        rate = Decimal(rate_result.rate)
        converted = _rounded_product(normalized_amount, rate)
        return CurrencyConversionResult(
            amount=decimal_text(normalized_amount),
            from_currency=rate_result.base_currency,
            to_currency=rate_result.quote_currency,
            rate=rate_result.rate,
            converted_amount=f"{converted:.2f}",
            as_of=rate_result.as_of,
            provider=rate_result.provider,
        )


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _provider_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise tool_error(INVALID_RESPONSE)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (DecimalException, ValueError):
        raise tool_error(INVALID_RESPONSE) from None
    if not number.is_finite():
        raise tool_error(INVALID_RESPONSE)
    return number


def _as_of(payload: dict[str, Any]) -> str:
    updated = payload.get("time_last_update_utc")
    if isinstance(updated, str) and updated.strip():
        return updated.strip()

    timestamp = payload.get("timestamp")
    if isinstance(timestamp, bool) or timestamp is None:
        raise tool_error(INVALID_RESPONSE)
    if isinstance(timestamp, str):
        timestamp = timestamp.strip()
        if not timestamp:
            raise tool_error(INVALID_RESPONSE)
        return timestamp
    if isinstance(timestamp, (int, Decimal)):
        number = _provider_decimal(timestamp)
        if number < 0:
            raise tool_error(INVALID_RESPONSE)
        return decimal_text(number)
    raise tool_error(INVALID_RESPONSE)


def _rounded_product(amount: Decimal, rate: Decimal) -> Decimal:
    precision = max(28, len(amount.as_tuple().digits) + len(rate.as_tuple().digits) + 4)
    adjusted_result = amount.adjusted() + rate.adjusted() + 2 if amount and rate else 2
    try:
        with localcontext() as context:
            context.prec = precision
            context.Emax = min(MAX_EMAX, max(context.Emax, adjusted_result))
            return (amount * rate).quantize(_CENT, rounding=ROUND_HALF_UP)
    except DecimalException:
        raise tool_error(INVALID_AMOUNT) from None


def _log_provider_error(category: str, *, status_code: int | None = None) -> None:
    extra: dict[str, Any] = {
        "event": "currency_provider_error",
        "provider": PROVIDER,
        "error_category": category,
    }
    if status_code is not None:
        extra["status_code"] = status_code
    logger.warning("Currency provider request failed", extra=extra)


def tool_error(code: str) -> ToolError:
    return ToolError(json.dumps({"error": code, "message": _ERROR_MESSAGES[code]}, separators=(",", ":")))
