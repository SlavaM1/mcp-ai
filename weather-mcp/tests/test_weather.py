from __future__ import annotations

import unittest

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from weather_mcp.server import MAX_CITY_LENGTH, _normalize_city, app, mcp
from weather_mcp.weather import (
    CITY_NOT_FOUND,
    INVALID_RESPONSE,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    WeatherService,
)


GEOCODING_RESPONSE = {
    "generationtime_ms": 0.1,
    "results": [
        {
            "name": "Москва",
            "country": "Россия",
            "latitude": 55.7522,
            "longitude": 37.6156,
        }
    ]
}

FORECAST_RESPONSE = {
    "timezone": "Europe/Moscow",
    "utc_offset_seconds": 10800,
    "current": {
        "time": "2026-09-22T14:15",
        "temperature_2m": 12.4,
        "apparent_temperature": 10.1,
        "relative_humidity_2m": 71,
        "wind_speed_10m": 8.2,
        "weather_code": 61,
    },
}


async def call_service(handler: httpx2.AsyncBaseTransport, city: str = "Moscow"):
    async with httpx2.AsyncClient(transport=handler, trust_env=False) as client:
        return await WeatherService(client).get_current_weather(city)


class WeatherServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_weather_response(self) -> None:
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            if request.url.host == "geocoding-api.open-meteo.com":
                return httpx2.Response(200, json=GEOCODING_RESPONSE)
            return httpx2.Response(200, json=FORECAST_RESPONSE)

        result = await call_service(httpx2.MockTransport(handler))
        payload = result.model_dump(mode="json", by_alias=True)

        self.assertEqual(payload["city"], "Москва")
        self.assertEqual(payload["country"], "Россия")
        self.assertEqual(payload["weatherDescription"], "Небольшой дождь")
        self.assertEqual(payload["observedAt"], "2026-09-22T14:15:00+03:00")
        self.assertEqual(payload["timezone"], "Europe/Moscow")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.params["count"], "1")
        self.assertIn("temperature_2m", requests[1].url.params["current"])

    async def test_city_not_found(self) -> None:
        transport = httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"generationtime_ms": 0.1})
        )
        with self.assertRaisesRegex(ToolError, f"^{CITY_NOT_FOUND}$"):
            await call_service(transport)

    async def test_provider_timeout(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("provider details", request=request)

        with self.assertRaisesRegex(ToolError, f"^{PROVIDER_TIMEOUT}$"):
            await call_service(httpx2.MockTransport(handler))

    async def test_provider_unavailable(self) -> None:
        transport = httpx2.MockTransport(
            lambda request: httpx2.Response(503, json={"reason": "internal detail"})
        )
        with self.assertRaisesRegex(ToolError, f"^{PROVIDER_UNAVAILABLE}$"):
            await call_service(transport)

    async def test_invalid_provider_response(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.host == "geocoding-api.open-meteo.com":
                return httpx2.Response(200, json=GEOCODING_RESPONSE)
            invalid = {**FORECAST_RESPONSE, "current": {**FORECAST_RESPONSE["current"], "relative_humidity_2m": 101}}
            return httpx2.Response(200, json=invalid)

        with self.assertRaisesRegex(ToolError, f"^{INVALID_RESPONSE}$"):
            await call_service(httpx2.MockTransport(handler))

    async def test_missing_geocoding_metadata_is_invalid(self) -> None:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))
        with self.assertRaisesRegex(ToolError, f"^{INVALID_RESPONSE}$"):
            await call_service(transport)


class MCPDefinitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_schema_requires_described_bounded_city(self) -> None:
        tools = await mcp.list_tools()
        self.assertEqual(
            [tool.name for tool in tools],
            [
                "get_current_weather",
                "create_weather_schedule",
                "list_weather_schedules",
                "stop_weather_schedule",
                "run_weather_collection_now",
                "get_weather_summary",
            ],
        )

        schema = tools[0].input_schema
        city = schema["properties"]["city"]
        self.assertEqual(schema["required"], ["city"])
        self.assertEqual(city["type"], "string")
        self.assertEqual(city["minLength"], 1)
        self.assertEqual(city["maxLength"], MAX_CITY_LENGTH)
        self.assertIn("пробелы", city["description"])

        create_schema = tools[1].input_schema
        self.assertEqual(create_schema["required"], ["city", "interval_seconds"])
        self.assertEqual(create_schema["properties"]["interval_seconds"]["minimum"], 30)

        summary_schema = tools[-1].input_schema
        self.assertEqual(summary_schema["required"], ["city", "minutes"])
        self.assertEqual(summary_schema["properties"]["minutes"]["minimum"], 1)

        self.assertEqual(
            set(tools[0].output_schema["properties"]),
            {
                "city",
                "country",
                "latitude",
                "longitude",
                "temperature",
                "apparentTemperature",
                "relativeHumidity",
                "windSpeed",
                "weatherCode",
                "weatherDescription",
                "observedAt",
                "timezone",
            },
        )

    def test_city_normalization_and_validation(self) -> None:
        self.assertEqual(_normalize_city("  New York  "), "New York")
        for invalid in ("", "   ", "x" * (MAX_CITY_LENGTH + 1), 42):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "Город|города"):
                    _normalize_city(invalid)

    def test_health_route_is_registered(self) -> None:
        self.assertIn("/health", {route.path for route in app.routes})
