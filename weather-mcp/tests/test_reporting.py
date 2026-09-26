from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from weather_mcp.reporting import WeatherAnalysis, build_weather_analysis, write_weather_report
from weather_mcp.weather import WeatherForecastResult


FORECAST = {
    "requested_city": "Новосибирск",
    "resolved_location": {
        "city": "Novosibirsk",
        "region": "Novosibirsk Oblast",
        "country": "Russia",
    },
    "city": "Novosibirsk",
    "forecast": [
        {
            "date": "2026-09-26",
            "min_temperature_c": 1,
            "max_temperature_c": 9,
            "avg_temperature_c": 7,
            "humidity_percent": 70,
            "max_wind_kmh": 12,
            "precipitation_mm": 1.2,
            "conditions": ["Cloudy"],
        },
        {
            "date": "2026-09-27",
            "min_temperature_c": -1,
            "max_temperature_c": 6,
            "avg_temperature_c": 3,
            "humidity_percent": 80,
            "max_wind_kmh": 23,
            "precipitation_mm": 3.1,
            "conditions": ["Light rain"],
        },
        {
            "date": "2026-09-28",
            "min_temperature_c": 2,
            "max_temperature_c": 8,
            "avg_temperature_c": 5.6,
            "humidity_percent": 72,
            "max_wind_kmh": 18,
            "precipitation_mm": 1,
            "conditions": ["Cloudy"],
        },
    ],
}


class WeatherAnalysisTests(unittest.TestCase):
    def test_analysis_calculates_exact_aggregates(self) -> None:
        analysis = build_weather_analysis(WeatherForecastResult.model_validate(FORECAST))

        self.assertEqual(analysis.temperature.min_c, -1)
        self.assertEqual(analysis.temperature.max_c, 9)
        self.assertEqual(analysis.temperature.avg_c, 5.2)
        self.assertEqual(analysis.temperature.change_c, -1.4)
        self.assertEqual(analysis.coldest_day, "2026-09-27")
        self.assertEqual(analysis.warmest_day, "2026-09-26")
        self.assertEqual(analysis.humidity.avg_percent, 74)
        self.assertEqual(analysis.wind.max_kmh, 23)
        self.assertEqual(analysis.precipitation.total_mm, 5.3)
        self.assertEqual(analysis.precipitation.max_day, "2026-09-27")
        self.assertEqual(analysis.conditions, ["Cloudy", "Light rain"])
        self.assertTrue(analysis.has_rain)
        self.assertEqual(analysis.temperature_trend, "cooling")

    def test_empty_forecast_is_controlled_error(self) -> None:
        weather_data = WeatherForecastResult.model_validate({**FORECAST, "forecast": []})

        with self.assertRaisesRegex(ToolError, "EMPTY_FORECAST"):
            build_weather_analysis(weather_data)

    def test_malformed_forecast_is_rejected_by_schema(self) -> None:
        malformed = {**FORECAST, "forecast": [{"date": "2026-09-26"}]}

        with self.assertRaises(ValidationError):
            WeatherForecastResult.model_validate(malformed)


class WeatherReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analysis = build_weather_analysis(WeatherForecastResult.model_validate(FORECAST))

    def test_markdown_report_is_created_with_expected_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = write_weather_report(self.analysis, Path(temporary_directory) / "reports")
            path = Path(result.path)

            self.assertTrue(result.saved)
            self.assertEqual(path.parent, Path(temporary_directory) / "reports")
            self.assertEqual(path.name, result.file_name)
            content = path.read_text(encoding="utf-8")
            self.assertIn("# Weather report: Novosibirsk", content)
            self.assertIn("- Average: 5.2 °C", content)
            self.assertIn("- Total: 5.3 mm", content)
            self.assertIn("- Maximum speed: 23 km/h", content)

    def test_empty_analysis_is_rejected_by_schema(self) -> None:
        with self.assertRaises(ValidationError):
            WeatherAnalysis.model_validate({})

    def test_unsafe_city_cannot_escape_report_directory(self) -> None:
        unsafe = WeatherAnalysis.model_validate(
            {**self.analysis.model_dump(by_alias=True), "city": "../../Moscow\\report"}
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_directory = Path(temporary_directory) / "reports"
            result = write_weather_report(unsafe, report_directory)
            path = Path(result.path)

            self.assertEqual(path.parent, report_directory)
            self.assertNotIn("..", result.file_name)
            self.assertNotIn("/", result.file_name)
            self.assertNotIn("\\", result.file_name)

    def test_existing_report_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_directory = Path(temporary_directory)
            first = write_weather_report(self.analysis, report_directory)
            first_content = Path(first.path).read_text(encoding="utf-8")
            second = write_weather_report(self.analysis, report_directory)

            self.assertNotEqual(first.file_name, second.file_name)
            self.assertEqual(Path(first.path).read_text(encoding="utf-8"), first_content)
            self.assertTrue(Path(second.path).is_file())

    def test_filesystem_failure_is_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            not_a_directory = Path(temporary_directory) / "blocked"
            not_a_directory.write_text("file", encoding="utf-8")

            with self.assertRaisesRegex(ToolError, "REPORT_WRITE_ERROR"):
                write_weather_report(self.analysis, not_a_directory)
