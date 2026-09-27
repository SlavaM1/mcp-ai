from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from report_mcp.reporting import build_markdown, write_trip_report


TRIP_ANALYSIS = {
    "destination": "Санкт-Петербург",
    "period": {"days": 3, "from": "2026-09-27", "to": "2026-09-29"},
    "weather": {
        "min_temperature_c": 7,
        "max_temperature_c": 12,
        "avg_temperature_c": 9.5,
        "rain_expected": True,
        "max_wind_kmh": 18,
    },
    "budget": {
        "original_amount": "100000",
        "original_currency": "RUB",
        "converted_amount": "1000.00",
        "destination_currency": "EUR",
        "rate": "0.01",
    },
    "flags": {
        "rain_expected": True,
        "cold_weather": True,
        "hot_weather": False,
        "strong_wind": False,
    },
    "recommendations": ["take_umbrella", "take_warm_layer"],
}


def error_payload(context: unittest.TestCase, exception: ToolError) -> dict[str, str]:
    try:
        return json.loads(str(exception))
    except json.JSONDecodeError as exc:
        context.fail(f"ToolError did not contain JSON: {exc}")


class MarkdownTests(unittest.TestCase):
    def test_markdown_contains_all_analysis_domains(self) -> None:
        content = build_markdown(TRIP_ANALYSIS)

        self.assertIn("# Trip Report: Санкт-Петербург", content)
        self.assertIn("## Weather", content)
        self.assertIn("**Avg temperature c:** 9.5", content)
        self.assertIn("## Period", content)
        self.assertIn("**Days:** 3", content)
        self.assertIn("## Budget", content)
        self.assertIn("**Converted amount:** 1000.00", content)
        self.assertIn("take_umbrella", content)

    def test_invalid_data_is_controlled_json_error(self) -> None:
        for invalid in (
            {},
            {"destination": ""},
            {"destination": "Paris"},
            {**TRIP_ANALYSIS, "recommendations": ["invented"]},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ToolError) as raised:
                    build_markdown(invalid)
                self.assertEqual(
                    error_payload(self, raised.exception)["error"],
                    "INVALID_TRIP_ANALYSIS",
                )


class PersistenceTests(unittest.TestCase):
    def test_report_is_written_under_configured_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            reports = Path(temporary_directory) / "persistent" / "reports"
            result = write_trip_report(TRIP_ANALYSIS, reports)
            path = Path(result.path)

            self.assertTrue(result.saved)
            self.assertEqual(result.format, "markdown")
            self.assertEqual(path.parent, reports.resolve())
            self.assertEqual(path.name, result.file_name)
            self.assertEqual(path.read_text(encoding="utf-8"), build_markdown(TRIP_ANALYSIS))

    def test_path_traversal_destination_produces_safe_transliterated_filename(self) -> None:
        analysis = {**TRIP_ANALYSIS, "destination": "../../Москва\\report"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            reports = Path(temporary_directory) / "reports"
            result = write_trip_report(analysis, reports)
            path = Path(result.path)

            self.assertEqual(path.parent, reports.resolve())
            self.assertRegex(
                result.file_name,
                r"^trip-report-moskva-report-\d{4}-\d{2}-\d{2}-\d{6}-\d{6}-[0-9a-f]{32}\.md$",
            )
            self.assertNotIn("..", result.file_name)
            self.assertNotIn("/", result.file_name)
            self.assertNotIn("\\", result.file_name)

    def test_destination_report_does_not_duplicate_report_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = write_trip_report(
                {**TRIP_ANALYSIS, "destination": "report"},
                Path(temporary_directory),
            )
            self.assertTrue(result.file_name.startswith("trip-report-destination-"))
            self.assertFalse(result.file_name.startswith("trip-report-report-"))

    def test_repeated_writes_are_unique_and_do_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            reports = Path(temporary_directory)
            first = write_trip_report(TRIP_ANALYSIS, reports)
            first_content = Path(first.path).read_text(encoding="utf-8")
            second = write_trip_report(TRIP_ANALYSIS, reports)

            self.assertNotEqual(first.file_name, second.file_name)
            self.assertEqual(Path(first.path).read_text(encoding="utf-8"), first_content)
            self.assertTrue(Path(second.path).is_file())

    def test_unsupported_format_is_controlled_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ToolError) as raised:
                write_trip_report(TRIP_ANALYSIS, Path(temporary_directory), "pdf")
        self.assertEqual(
            error_payload(self, raised.exception)["error"],
            "UNSUPPORTED_REPORT_FORMAT",
        )

    def test_filesystem_failure_is_controlled_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            blocked = Path(temporary_directory) / "blocked"
            blocked.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(ToolError) as raised:
                write_trip_report(TRIP_ANALYSIS, blocked)
        self.assertEqual(
            error_payload(self, raised.exception)["error"],
            "REPORT_WRITE_ERROR",
        )
