from __future__ import annotations

import json
import unittest
from decimal import Decimal

from pydantic import ValidationError

from travel_mcp.analysis import (
    CurrencyConversionResult,
    ForecastDay,
    ResolvedLocation,
    WeatherForecastResult,
    build_trip_analysis,
)
from travel_mcp.server import (
    MAX_DESTINATION_LENGTH,
    _normalize_destination,
    app,
    health,
    mcp,
)


def forecast(*days: ForecastDay) -> WeatherForecastResult:
    return WeatherForecastResult(
        requested_city="Moscow",
        resolved_location=ResolvedLocation(city="Moscow", country="Russia"),
        city="Moscow",
        forecast=list(days),
    )


def day(
    date: str,
    minimum: float,
    maximum: float,
    average: float,
    wind: float,
    precipitation: float = 0,
    conditions: list[str] | None = None,
) -> ForecastDay:
    return ForecastDay(
        date=date,
        min_temperature_c=minimum,
        max_temperature_c=maximum,
        avg_temperature_c=average,
        humidity_percent=60,
        max_wind_kmh=wind,
        precipitation_mm=precipitation,
        conditions=conditions or ["Clear"],
    )


BUDGET = CurrencyConversionResult(
    amount="1000",
    from_currency="USD",
    to_currency="EUR",
    converted_amount=920,
    rate=0.92,
    as_of="Thu, 26 Sep 2024 00:02:31 +0000",
    provider="ExchangeRate-API",
)


class TripAnalysisTests(unittest.TestCase):
    def test_analysis_uses_supplied_weather_and_budget_deterministically(self) -> None:
        weather = forecast(
            day("2026-10-01", 8, 20, 14, 15),
            day("2026-10-02", 12, 33, 22, 45, conditions=["Rain showers"]),
            day("2026-10-03", 11, 25, 18, 20),
        )

        first = build_trip_analysis("Paris", weather, BUDGET)
        second = build_trip_analysis("Paris", weather, BUDGET)

        self.assertEqual(first, second)
        self.assertEqual(
            first.model_dump(by_alias=True),
            {
                "destination": "Paris",
                "period": {
                    "days": 3,
                    "from": "2026-10-01",
                    "to": "2026-10-03",
                },
                "weather": {
                    "min_temperature_c": 8.0,
                    "max_temperature_c": 33.0,
                    "avg_temperature_c": 18.0,
                    "rain_expected": True,
                    "max_wind_kmh": 45.0,
                },
                "budget": {
                    "original_amount": Decimal("1000"),
                    "original_currency": "USD",
                    "converted_amount": Decimal("920"),
                    "destination_currency": "EUR",
                    "rate": Decimal("0.92"),
                },
                "flags": {
                    "rain_expected": True,
                    "cold_weather": True,
                    "hot_weather": True,
                    "strong_wind": True,
                },
                "recommendations": [
                    "take_umbrella",
                    "take_warm_layer",
                    "take_water",
                    "prepare_for_strong_wind",
                ],
            },
        )

    def test_exact_threshold_behavior(self) -> None:
        result = build_trip_analysis(
            "Rome",
            forecast(day("2026-10-01", 10, 30, 20, 40)),
            BUDGET,
        )

        self.assertFalse(result.flags.cold_weather)
        self.assertFalse(result.flags.hot_weather)
        self.assertTrue(result.flags.strong_wind)
        self.assertEqual(result.recommendations, ["prepare_for_strong_wind"])

    def test_accepts_actual_currency_mcp_conversion_payload(self) -> None:
        result = build_trip_analysis(
            "Istanbul",
            forecast(day("2026-10-01", 15, 24, 20, 17)),
            CurrencyConversionResult.model_validate(
                {
                    "amount": "100000",
                    "from_currency": "RUB",
                    "to_currency": "TRY",
                    "rate": "0.42",
                    "converted_amount": "42000.00",
                    "as_of": "2026-09-27",
                    "provider": "ExchangeRate-API",
                }
            ),
        )

        self.assertEqual(result.budget.original_amount, Decimal("100000"))
        self.assertEqual(result.budget.original_currency, "RUB")
        self.assertEqual(result.budget.converted_amount, Decimal("42000.00"))
        self.assertEqual(result.budget.destination_currency, "TRY")

    def test_thresholds_are_strict_for_cold_and_hot(self) -> None:
        result = build_trip_analysis(
            "Rome",
            forecast(day("2026-10-01", 9.9, 30.1, 20, 39.9)),
            BUDGET,
        )

        self.assertTrue(result.flags.cold_weather)
        self.assertTrue(result.flags.hot_weather)
        self.assertFalse(result.flags.strong_wind)

    def test_rain_detected_from_precipitation_and_english_or_russian_condition(self) -> None:
        cases = (
            day("2026-10-01", 15, 20, 18, 5, precipitation=0.1),
            day("2026-10-01", 15, 20, 18, 5, conditions=["Light precipitation"]),
            day("2026-10-01", 15, 20, 18, 5, conditions=["Небольшой дождь"]),
            day("2026-10-01", 15, 20, 18, 5, conditions=["Осадки"]),
        )
        for rainy_day in cases:
            with self.subTest(conditions=rainy_day.conditions):
                result = build_trip_analysis("London", forecast(rainy_day), BUDGET)
                self.assertTrue(result.weather.rain_expected)
                self.assertTrue(result.flags.rain_expected)

    def test_empty_forecast_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            forecast()


class MCPDefinitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_tool_name_description_and_schemas(self) -> None:
        tools = await mcp.list_tools()

        self.assertEqual([tool.name for tool in tools], ["analyze_trip"])
        tool = tools[0]
        self.assertEqual(
            tool.input_schema["required"],
            ["destination", "weather_data", "budget_data"],
        )
        self.assertEqual(
            tool.input_schema["properties"]["destination"]["maxLength"],
            MAX_DESTINATION_LENGTH,
        )
        forecast_schema = tool.input_schema["$defs"]["WeatherForecastResult"]
        self.assertEqual(forecast_schema["properties"]["forecast"]["minItems"], 1)
        self.assertIn("get_weather_forecast", tool.description)
        self.assertIn("convert_currency", tool.description)
        self.assertIn("fetches nothing", tool.description)
        self.assertEqual(
            set(tool.output_schema["properties"]),
            {"destination", "period", "weather", "budget", "flags", "recommendations"},
        )

    async def test_health_route_returns_ok(self) -> None:
        self.assertIn("/health", {route.path for route in app.routes})
        response = await health(None)  # type: ignore[arg-type]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"status": "ok"})

    def test_destination_normalization_and_validation(self) -> None:
        self.assertEqual(_normalize_destination("  New York  "), "New York")
        for invalid in ("", "   ", "x" * (MAX_DESTINATION_LENGTH + 1), 42):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "Destination"):
                    _normalize_destination(invalid)
