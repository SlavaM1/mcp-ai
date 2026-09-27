from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class TripPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    days: int = Field(ge=1)
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)


class TripWeather(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_temperature_c: float
    max_temperature_c: float
    avg_temperature_c: float
    rain_expected: bool
    max_wind_kmh: float


class TripBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_amount: Decimal
    original_currency: str = Field(min_length=3, max_length=3)
    converted_amount: Decimal
    destination_currency: str = Field(min_length=3, max_length=3)
    rate: Decimal


class TripFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rain_expected: bool
    cold_weather: bool
    hot_weather: bool
    strong_wind: bool


class TripAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(min_length=1, max_length=200)
    period: TripPeriod
    weather: TripWeather
    budget: TripBudget
    flags: TripFlags
    recommendations: list[
        Literal[
            "take_umbrella",
            "take_warm_layer",
            "take_water",
            "prepare_for_strong_wind",
        ]
    ]


class SavedReport(BaseModel):
    saved: bool
    file_name: str
    path: str
    format: Literal["markdown"]


def write_trip_report(
    trip_analysis: TripAnalysis | dict[str, Any],
    reports_directory: Path,
    format: str = "markdown",
) -> SavedReport:
    if format != "markdown":
        raise _report_error(
            "UNSUPPORTED_REPORT_FORMAT",
            "Only markdown reports are supported.",
        )

    analysis, destination = _validate_trip_analysis(trip_analysis)
    content = build_markdown(analysis)

    try:
        reports_directory.mkdir(parents=True, exist_ok=True)
        directory = reports_directory.resolve(strict=True)
        if not directory.is_dir():
            raise OSError("reports path is not a directory")

        slug = _safe_slug(destination)
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S-%f")
        file_name = f"trip-report-{slug}-{timestamp}-{uuid4().hex}.md"
        path = directory / file_name
        if path.parent != directory or path.resolve(strict=False).parent != directory:
            raise OSError("unsafe report path")

        with path.open("x", encoding="utf-8") as report_file:
            report_file.write(content)
    except OSError as exc:
        raise _report_error(
            "REPORT_WRITE_ERROR",
            "The trip report could not be saved safely.",
        ) from exc

    return SavedReport(
        saved=True,
        file_name=file_name,
        path=str(path),
        format="markdown",
    )


def build_markdown(trip_analysis: TripAnalysis | dict[str, Any]) -> str:
    analysis, destination = _validate_trip_analysis(trip_analysis)
    lines = [f"# Trip Report: {_display_value(destination)}", ""]

    for key, value in analysis.items():
        if key == "destination":
            continue
        lines.extend((f"## {_heading(key)}", ""))
        lines.extend(_render_value(value, heading_level=3))
        lines.append("")

    if len(analysis) == 1:
        lines.extend(("No additional analysis details were supplied.", ""))
    return "\n".join(lines).rstrip() + "\n"


def _validate_trip_analysis(value: Any) -> tuple[dict[str, Any], str]:
    try:
        analysis = value if isinstance(value, TripAnalysis) else TripAnalysis.model_validate(value)
        normalized = analysis.model_dump(mode="json", by_alias=True)
        _validate_json_value(normalized)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _report_error(
            "INVALID_TRIP_ANALYSIS",
            "trip_analysis must be a complete structured analyze_trip result.",
        ) from exc
    return normalized, analysis.destination.strip()


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("object keys must be strings")
            _validate_json_value(item)
        return
    raise TypeError("unsupported structured value")


def _render_value(value: Any, heading_level: int) -> list[str]:
    if isinstance(value, dict):
        if not value:
            return ["Not available"]
        lines: list[str] = []
        for key, item in value.items():
            if _is_scalar(item):
                lines.append(f"- **{_heading(key)}:** {_display_value(item)}")
            else:
                lines.extend((f"{'#' * heading_level} {_heading(key)}", ""))
                lines.extend(_render_value(item, min(heading_level + 1, 6)))
                lines.append("")
        while lines and not lines[-1]:
            lines.pop()
        return lines

    if isinstance(value, list):
        if not value:
            return ["Not available"]
        lines = []
        for index, item in enumerate(value, start=1):
            if _is_scalar(item):
                lines.append(f"- {_display_value(item)}")
                continue
            title = _item_title(item, index)
            lines.extend((f"{'#' * heading_level} {title}", ""))
            lines.extend(_render_value(item, min(heading_level + 1, 6)))
            lines.append("")
        while lines and not lines[-1]:
            lines.pop()
        return lines

    return [_display_value(value)]


def _item_title(value: Any, index: int) -> str:
    if isinstance(value, dict):
        for key in ("name", "title", "date", "day"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool):
                return f"{index}. {_display_value(candidate)}"
    return f"Item {index}"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def _display_value(value: Any) -> str:
    if value is None:
        return "Not available"
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    text = str(value).replace("\x00", "").strip()
    return text or "Not available"


def _heading(value: str) -> str:
    words = re.sub(r"[_-]+", " ", value).strip()
    return words[:1].upper() + words[1:] if words else "Details"


_CYRILLIC_TRANSLITERATION = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya", "і": "i", "ї": "yi", "є": "ye",
        "ґ": "g",
    }
)


def _safe_slug(value: str) -> str:
    transliterated = value.lower().translate(_CYRILLIC_TRANSLITERATION)
    ascii_value = unicodedata.normalize("NFKD", transliterated).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    slug = re.sub(r"^(?:trip-)?report(?:-|$)", "", slug).strip("-")
    return slug[:80].rstrip("-") or "destination"


def _report_error(code: str, message: str) -> ToolError:
    return ToolError(json.dumps({"error": code, "message": message}, separators=(",", ":")))
