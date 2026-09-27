from __future__ import annotations

import json
import unittest
from decimal import Decimal

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from currency_mcp.currency import (
    INVALID_AMOUNT,
    INVALID_CURRENCY_CODE,
    INVALID_RESPONSE,
    PROVIDER_FAILURE,
    PROVIDER_HTTP_ERROR,
    PROVIDER_PROXY_ERROR,
    PROVIDER_RATE_LIMITED,
    PROVIDER_TIMEOUT,
    QUOTE_CURRENCY_NOT_FOUND,
    UNSUPPORTED_CURRENCY,
    CurrencyService,
    ExchangeRateClient,
    normalize_amount,
    normalize_currency_code,
)
from currency_mcp.server import DEFAULT_BASE_URL, app, health, mcp


PROVIDER_RESPONSE = {
    "result": "success",
    "provider": "https://www.exchangerate-api.com",
    "time_last_update_unix": 1_727_308_951,
    "time_last_update_utc": "Thu, 26 Sep 2024 00:02:31 +0000",
    "time_next_update_unix": 1_727_395_188,
    "time_next_update_utc": "Fri, 27 Sep 2024 00:02:68 +0000",
    "base_code": "USD",
    "rates": {"USD": 1, "EUR": 0.9, "JPY": 144.123456, "XTS": 0.2},
}


def error_code(error: ToolError) -> str:
    message = str(error)
    return json.loads(message[message.index("{") :])["error"]


async def service_for(handler: httpx2.AsyncBaseTransport) -> tuple[CurrencyService, httpx2.AsyncClient]:
    client = httpx2.AsyncClient(transport=handler, trust_env=False)
    return CurrencyService(ExchangeRateClient(client, DEFAULT_BASE_URL)), client


class CurrencyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_real_exchange_rate_api_shape(self) -> None:
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return httpx2.Response(200, json=PROVIDER_RESPONSE)

        service, client = await service_for(httpx2.MockTransport(handler))
        try:
            result = await service.get_exchange_rate(" usd ", " eur ")
        finally:
            await client.aclose()

        self.assertEqual(str(requests[0].url), f"{DEFAULT_BASE_URL}/USD")
        self.assertEqual(result.base_currency, "USD")
        self.assertEqual(result.quote_currency, "EUR")
        self.assertEqual(result.rate, "0.9")
        self.assertEqual(result.as_of, "Thu, 26 Sep 2024 00:02:31 +0000")
        self.assertEqual(result.provider, "ExchangeRate-API")

    async def test_uses_timestamp_when_utc_update_time_is_absent(self) -> None:
        response = {"result": "success", "timestamp": 1_727_308_951, "rates": {"EUR": "0.90"}}
        service, client = await service_for(
            httpx2.MockTransport(lambda request: httpx2.Response(200, json=response))
        )
        try:
            result = await service.get_exchange_rate("USD", "EUR")
        finally:
            await client.aclose()
        self.assertEqual(result.as_of, "1727308951")
        self.assertEqual(result.rate, "0.9")

    async def test_conversion_uses_exact_decimal_arithmetic_and_half_up_rounding(self) -> None:
        service, client = await service_for(
            httpx2.MockTransport(lambda request: httpx2.Response(200, json=PROVIDER_RESPONSE))
        )
        try:
            exact = await service.convert_currency("0.1", "USD", "XTS")
            rounded = await service.convert_currency(Decimal("1.005"), "USD", "USD")
            zero = await service.convert_currency(0, "USD", "EUR")
        finally:
            await client.aclose()

        self.assertEqual(exact.amount, "0.1")
        self.assertEqual(exact.rate, "0.2")
        self.assertEqual(exact.converted_amount, "0.02")
        self.assertEqual(rounded.converted_amount, "1.01")
        self.assertEqual(zero.converted_amount, "0.00")

    async def test_missing_quote_currency_is_controlled(self) -> None:
        service, client = await service_for(
            httpx2.MockTransport(lambda request: httpx2.Response(200, json=PROVIDER_RESPONSE))
        )
        try:
            with self.assertRaises(ToolError) as raised:
                await service.get_exchange_rate("USD", "GBP")
        finally:
            await client.aclose()
        self.assertEqual(error_code(raised.exception), QUOTE_CURRENCY_NOT_FOUND)

    async def test_provider_timeout_and_proxy_errors_are_controlled(self) -> None:
        cases: list[tuple[type[httpx2.RequestError], str]] = [
            (httpx2.ReadTimeout, PROVIDER_TIMEOUT),
            (httpx2.ProxyError, PROVIDER_PROXY_ERROR),
        ]
        for exception_type, expected in cases:
            with self.subTest(expected=expected):
                def handler(request: httpx2.Request) -> httpx2.Response:
                    raise exception_type("upstream detail", request=request)

                service, client = await service_for(httpx2.MockTransport(handler))
                try:
                    with self.assertRaises(ToolError) as raised:
                        await service.get_exchange_rate("USD", "EUR")
                finally:
                    await client.aclose()
                self.assertEqual(error_code(raised.exception), expected)
                self.assertNotIn("upstream detail", str(raised.exception))

    async def test_http_and_rate_limit_errors_are_controlled(self) -> None:
        for status, expected in ((429, PROVIDER_RATE_LIMITED), (500, PROVIDER_HTTP_ERROR)):
            with self.subTest(status=status):
                service, client = await service_for(
                    httpx2.MockTransport(
                        lambda request, status=status: httpx2.Response(
                            status, json={"private": "provider details"}
                        )
                    )
                )
                try:
                    with self.assertRaises(ToolError) as raised:
                        await service.get_exchange_rate("USD", "EUR")
                finally:
                    await client.aclose()
                self.assertEqual(error_code(raised.exception), expected)
                self.assertNotIn("provider details", str(raised.exception))

    async def test_malformed_and_failed_provider_responses_are_controlled(self) -> None:
        cases = [
            (b"not-json", INVALID_RESPONSE),
            ({"timestamp": 1, "rates": {"EUR": 0.9}}, INVALID_RESPONSE),
            ({"result": "error", "error-type": "unsupported-code"}, UNSUPPORTED_CURRENCY),
            ({"result": "success", "rates": {"EUR": 0.9}}, INVALID_RESPONSE),
            ({"result": "success", "timestamp": 1, "rates": []}, INVALID_RESPONSE),
            ({"result": "success", "timestamp": 1, "rates": {"EUR": "NaN"}}, INVALID_RESPONSE),
            (
                {
                    "result": "success",
                    "base_code": "GBP",
                    "timestamp": 1,
                    "rates": {"EUR": 0.9},
                },
                INVALID_RESPONSE,
            ),
        ]
        for body, expected in cases:
            with self.subTest(expected=expected, body=body):
                def handler(request: httpx2.Request) -> httpx2.Response:
                    if isinstance(body, bytes):
                        return httpx2.Response(200, content=body)
                    return httpx2.Response(200, json=body)

                service, client = await service_for(httpx2.MockTransport(handler))
                try:
                    with self.assertRaises(ToolError) as raised:
                        await service.get_exchange_rate("USD", "EUR")
                finally:
                    await client.aclose()
                self.assertEqual(error_code(raised.exception), expected)


class ValidationTests(unittest.TestCase):
    def test_currency_codes_are_trimmed_uppercase_ascii(self) -> None:
        self.assertEqual(normalize_currency_code(" usd "), "USD")
        for value in ("", "US", "USDD", "U1D", "EU\N{LATIN SMALL LETTER R WITH ACUTE}", None):
            with self.subTest(value=value):
                with self.assertRaises(ToolError) as raised:
                    normalize_currency_code(value)
                self.assertEqual(error_code(raised.exception), INVALID_CURRENCY_CODE)

    def test_amount_must_be_finite_and_non_negative_but_zero_is_allowed(self) -> None:
        self.assertEqual(normalize_amount(" 0 "), Decimal("0"))
        self.assertEqual(normalize_amount("0.1"), Decimal("0.1"))
        for value in (-1, "-0.01", float("inf"), Decimal("NaN"), True, "nope", ""):
            with self.subTest(value=value):
                with self.assertRaises(ToolError) as raised:
                    normalize_amount(value)
                self.assertEqual(error_code(raised.exception), INVALID_AMOUNT)


class MCPDefinitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_tool_inputs_remain_controlled_tool_errors(self) -> None:
        cases = [
            (
                "get_exchange_rate",
                {"base_currency": "U1", "quote_currency": "EUR"},
                INVALID_CURRENCY_CODE,
            ),
            (
                "convert_currency",
                {"amount": -1, "from_currency": "USD", "to_currency": "EUR"},
                INVALID_AMOUNT,
            ),
        ]
        for tool_name, arguments, expected in cases:
            with self.subTest(tool_name=tool_name):
                with self.assertRaises(ToolError) as raised:
                    await mcp.call_tool(tool_name, arguments)
                self.assertEqual(error_code(raised.exception), expected)

    async def test_tool_names_descriptions_and_schemas(self) -> None:
        tools = await mcp.list_tools()
        self.assertEqual([tool.name for tool in tools], ["get_exchange_rate", "convert_currency"])
        self.assertIn("one unit", tools[0].description)
        self.assertIn("two decimal places", tools[1].description)

        rate_schema = tools[0].input_schema
        self.assertEqual(rate_schema["required"], ["base_currency", "quote_currency"])
        for name in ("base_currency", "quote_currency"):
            self.assertEqual(rate_schema["properties"][name]["minLength"], 3)
            self.assertEqual(rate_schema["properties"][name]["maxLength"], 3)
            self.assertEqual(rate_schema["properties"][name]["pattern"], "^[A-Z]{3}$")

        conversion_schema = tools[1].input_schema
        self.assertEqual(
            conversion_schema["required"], ["amount", "from_currency", "to_currency"]
        )
        self.assertEqual(conversion_schema["properties"]["amount"]["minimum"], 0)
        self.assertEqual(
            set(tools[0].output_schema["properties"]),
            {"base_currency", "quote_currency", "rate", "as_of", "provider"},
        )
        self.assertEqual(
            set(tools[1].output_schema["properties"]),
            {
                "amount",
                "from_currency",
                "to_currency",
                "rate",
                "converted_amount",
                "as_of",
                "provider",
            },
        )

    async def test_health_route_returns_ok(self) -> None:
        self.assertIn("/health", {route.path for route in app.routes})
        response = await health(None)  # type: ignore[arg-type]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"status": "ok"})
