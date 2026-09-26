from __future__ import annotations

import unittest
from urllib.parse import quote

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from weather_mcp.server import MAX_CITY_LENGTH, _normalize_city, app, mcp
from weather_mcp.weather import (
    CITY_NOT_FOUND,
    INVALID_RESPONSE,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    WeatherService,
    WttrWeatherClient,
)


WTTR_RESPONSE = {
    "current_condition": [
        {
            "temp_C": "-5",
            "FeelsLikeC": "-9",
            "weatherDesc": [{"value": "Light snow"}],
            "humidity": "82",
            "pressure": "1017",
            "windspeedKmph": "14",
            "winddir16Point": "NW",
            "winddirDegree": "315",
            "precipMM": "0.4",
            "cloudcover": "90",
            "visibility": "8",
            "uvIndex": "1",
            "localObsDateTime": "2026-09-25 10:30 AM",
        }
    ],
    "nearest_area": [
        {
            "areaName": [{"value": "Novosibirsk"}],
            "region": [{"value": "Novosibirsk Oblast"}],
            "country": [{"value": "Russia"}],
            "latitude": "55.041",
            "longitude": "82.934",
        }
    ],
    "weather": [
        {
            "date": "2026-09-25",
            "mintempC": "3",
            "maxtempC": "11",
            "avgtempC": "7",
            "astronomy": [
                {"sunrise": "07:14 AM", "sunset": "07:10 PM", "moon_phase": "Waxing Gibbous"}
            ],
            "hourly": [
                {
                    "time": "1200",
                    "tempC": "8",
                    "FeelsLikeC": "5",
                    "weatherDesc": [{"value": "Cloudy"}],
                    "humidity": "75",
                    "precipMM": "0.1",
                    "chanceofrain": "20",
                    "windspeedKmph": "12",
                    "winddir16Point": "NW",
                }
            ],
        },
        {
            "date": "2026-09-26",
            "mintempC": "2",
            "maxtempC": "10",
            "avgtempC": "6",
            "astronomy": [{}],
            "hourly": [
                {
                    "time": "1200",
                    "tempC": "7",
                    "FeelsLikeC": "5",
                    "weatherDesc": [{"value": "Light rain"}],
                    "humidity": "81",
                    "precipMM": "1.3",
                    "chanceofrain": "85",
                    "windspeedKmph": "19",
                    "winddir16Point": "W",
                }
            ],
        },
    ],
}


async def call_service(handler: httpx2.AsyncBaseTransport, city: str = "Novosibirsk"):
    async with httpx2.AsyncClient(transport=handler, trust_env=False) as client:
        return await WeatherService(WttrWeatherClient(client, "https://wttr.in")).get_current_weather(city)


class WeatherServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_weather_normalizes_wttr_response_and_location(self) -> None:
        result = await call_service(httpx2.MockTransport(lambda request: httpx2.Response(200, json=WTTR_RESPONSE)))

        self.assertEqual(result.requested_city, "Novosibirsk")
        self.assertEqual(result.city, "Novosibirsk")
        self.assertEqual(result.resolved_location.region, "Novosibirsk Oblast")
        self.assertEqual(result.country, "Russia")
        self.assertEqual(result.temperature_c, -5)
        self.assertEqual(result.feels_like_c, -9)
        self.assertEqual(result.condition, "Light snow")
        self.assertEqual(result.pressure_hpa, 1017)
        self.assertEqual(result.wind_direction_degrees, 315)

    async def test_city_is_url_encoded_for_spaces_and_cyrillic(self) -> None:
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return httpx2.Response(200, json=WTTR_RESPONSE)

        client = WttrWeatherClient(
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler), trust_env=False), "https://wttr.in"
        )
        try:
            await client.get_weather("New York")
            await client.get_weather("Санкт-Петербург")
        finally:
            await client._client.aclose()

        self.assertEqual(str(requests[0].url), "https://wttr.in/New%20York?format=j1")
        self.assertEqual(
            str(requests[1].url),
            f"https://wttr.in/{quote('Санкт-Петербург', safe='')}?format=j1",
        )

    async def test_optional_current_values_and_nearest_area_may_be_missing(self) -> None:
        response = {
            "current_condition": [
                {"temp_C": "1", "humidity": "70", "windspeedKmph": "5"}
            ]
        }
        result = await call_service(httpx2.MockTransport(lambda request: httpx2.Response(200, json=response)), "Омск")

        self.assertEqual(result.city, "Омск")
        self.assertIsNone(result.country)
        self.assertIsNone(result.feels_like_c)
        self.assertIsNone(result.condition)

    async def test_city_not_found(self) -> None:
        transport = httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"data": {"error": [{"msg": "Unknown location"}]}})
        )
        with self.assertRaisesRegex(ToolError, CITY_NOT_FOUND):
            await call_service(transport)

    async def test_provider_timeout(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("provider details", request=request)

        with self.assertRaisesRegex(ToolError, PROVIDER_TIMEOUT):
            await call_service(httpx2.MockTransport(handler))

    async def test_provider_unavailable_for_http_error(self) -> None:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(503, json={"reason": "internal detail"}))
        with self.assertRaisesRegex(ToolError, PROVIDER_UNAVAILABLE):
            await call_service(transport)

    async def test_invalid_provider_response(self) -> None:
        with self.assertRaisesRegex(ToolError, INVALID_RESPONSE):
            await call_service(httpx2.MockTransport(lambda request: httpx2.Response(200, json={"current_condition": []})))

    async def test_malformed_json_response(self) -> None:
        with self.assertRaisesRegex(ToolError, INVALID_RESPONSE):
            await call_service(
                httpx2.MockTransport(lambda request: httpx2.Response(200, content=b"not json"))
            )

    async def test_forecast_and_hourly_weather_are_normalized(self) -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json=WTTR_RESPONSE)), trust_env=False
        ) as client:
            service = WeatherService(WttrWeatherClient(client, "https://wttr.in"))
            forecast = await service.get_weather_forecast("Новосибирск", 2)
            hourly = await service.get_hourly_weather("Новосибирск")

        self.assertEqual([item.date for item in forecast.forecast], ["2026-09-25", "2026-09-26"])
        self.assertEqual(forecast.forecast[0].moon_phase, "Waxing Gibbous")
        self.assertEqual(forecast.forecast[0].humidity_percent, 75)
        self.assertEqual(forecast.forecast[1].precipitation_mm, 1.3)
        self.assertEqual(forecast.forecast[1].conditions, ["Light rain"])
        self.assertEqual(hourly.date, "2026-09-25")
        self.assertEqual(hourly.hours[0].time, "12:00")
        self.assertEqual(hourly.hours[0].chance_of_rain_percent, 20)


class MCPDefinitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_schemas_include_current_forecast_and_hourly_weather(self) -> None:
        tools = await mcp.list_tools()
        self.assertEqual(
            [tool.name for tool in tools],
            [
                "get_current_weather",
                "get_weather_forecast",
                "analyze_weather",
                "save_weather_report",
                "get_hourly_weather",
                "create_weather_schedule",
                "list_weather_schedules",
                "stop_weather_schedule",
                "run_weather_collection_now",
                "get_weather_summary",
            ],
        )
        current_schema = tools[0].input_schema
        city = current_schema["properties"]["city"]
        self.assertEqual(current_schema["required"], ["city"])
        self.assertEqual(city["type"], "string")
        self.assertEqual(city["minLength"], 1)
        self.assertEqual(city["maxLength"], MAX_CITY_LENGTH)
        self.assertIn("пробелы", city["description"])

        forecast_schema = tools[1].input_schema
        self.assertEqual(forecast_schema["required"], ["city", "days"])
        self.assertEqual(forecast_schema["properties"]["days"]["minimum"], 1)
        self.assertEqual(forecast_schema["properties"]["days"]["maximum"], 3)

        analyze_schema = tools[2].input_schema
        self.assertEqual(analyze_schema["required"], ["weather_data"])
        self.assertIn("get_weather_forecast", tools[2].description)

        report_schema = tools[3].input_schema
        self.assertEqual(report_schema["required"], ["analysis"])
        self.assertEqual(report_schema["properties"]["format"]["const"], "markdown")
        self.assertIn("analyze_weather", tools[3].description)

        summary_schema = tools[-1].input_schema
        self.assertEqual(summary_schema["required"], ["city", "minutes"])
        self.assertEqual(summary_schema["properties"]["minutes"]["minimum"], 1)
        self.assertEqual(
            set(tools[0].output_schema["properties"]),
            {
                "requested_city",
                "resolved_location",
                "city",
                "country",
                "temperature_c",
                "feels_like_c",
                "condition",
                "humidity_percent",
                "pressure_hpa",
                "wind_speed_kmh",
                "wind_direction",
                "wind_direction_degrees",
                "precipitation_mm",
                "cloud_cover_percent",
                "visibility_km",
                "uv_index",
                "observation_time",
                "latitude",
                "longitude",
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
