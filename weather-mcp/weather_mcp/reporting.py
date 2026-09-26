from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from weather_mcp.weather import WeatherForecastResult


class AnalysisPeriod(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    from_: str = Field(alias="from")
    to: str
    days: int


class TemperatureAnalysis(BaseModel):
    min_c: float
    max_c: float
    avg_c: float
    first_day_avg_c: float
    last_day_avg_c: float
    change_c: float


class HumidityAnalysis(BaseModel):
    avg_percent: float


class WindAnalysis(BaseModel):
    max_kmh: float


class PrecipitationAnalysis(BaseModel):
    total_mm: float
    max_day_mm: float
    max_day: str


class WeatherAnalysis(BaseModel):
    city: str
    period: AnalysisPeriod
    temperature: TemperatureAnalysis
    coldest_day: str
    warmest_day: str
    humidity: HumidityAnalysis
    wind: WindAnalysis
    precipitation: PrecipitationAnalysis
    conditions: list[str]
    has_rain: bool
    has_snow: bool
    has_strong_wind: bool
    temperature_trend: Literal["warming", "cooling", "stable"]


class SavedReport(BaseModel):
    saved: bool
    file_name: str
    path: str
    format: Literal["markdown"]


def build_weather_analysis(weather_data: WeatherForecastResult) -> WeatherAnalysis:
    days = weather_data.forecast
    if not days:
        raise _report_error("EMPTY_FORECAST", "Прогноз не содержит ни одного дня для анализа.")

    average_temperatures = [day.avg_temperature_c for day in days]
    humidities = [day.humidity_percent for day in days]
    precipitation = [day.precipitation_mm for day in days]
    coldest = min(days, key=lambda day: day.avg_temperature_c)
    warmest = max(days, key=lambda day: day.avg_temperature_c)
    wettest = max(days, key=lambda day: day.precipitation_mm)
    conditions = list(dict.fromkeys(condition for day in days for condition in day.conditions))
    change = round(average_temperatures[-1] - average_temperatures[0], 1)
    condition_text = " ".join(conditions).lower()

    return WeatherAnalysis(
        city=weather_data.resolved_location.city or weather_data.city,
        period=AnalysisPeriod(
            **{"from": days[0].date},
            to=days[-1].date,
            days=len(days),
        ),
        temperature=TemperatureAnalysis(
            min_c=min(day.min_temperature_c for day in days),
            max_c=max(day.max_temperature_c for day in days),
            avg_c=round(sum(average_temperatures) / len(average_temperatures), 1),
            first_day_avg_c=average_temperatures[0],
            last_day_avg_c=average_temperatures[-1],
            change_c=change,
        ),
        coldest_day=coldest.date,
        warmest_day=warmest.date,
        humidity=HumidityAnalysis(avg_percent=round(sum(humidities) / len(humidities), 1)),
        wind=WindAnalysis(max_kmh=max(day.max_wind_kmh for day in days)),
        precipitation=PrecipitationAnalysis(
            total_mm=round(sum(precipitation), 1),
            max_day_mm=wettest.precipitation_mm,
            max_day=wettest.date,
        ),
        conditions=conditions,
        has_rain="rain" in condition_text or "дожд" in condition_text,
        has_snow="snow" in condition_text or "снег" in condition_text,
        has_strong_wind=max(day.max_wind_kmh for day in days) >= 40,
        temperature_trend="warming" if change > 0 else "cooling" if change < 0 else "stable",
    )


def write_weather_report(
    analysis: WeatherAnalysis,
    reports_directory: Path,
) -> SavedReport:
    if not analysis.city.strip():
        raise _report_error("EMPTY_ANALYSIS", "Анализ не содержит города для отчёта.")
    content = _markdown_report(analysis)
    try:
        reports_directory.mkdir(parents=True, exist_ok=True)
        directory = reports_directory.resolve(strict=True)
        slug = _safe_slug(analysis.city)
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S-%f")
        file_name = f"weather-report-{slug}-{timestamp}-{uuid4().hex[:8]}.md"
        path = directory / file_name
        if path.parent != directory:
            raise OSError("unsafe report path")
        with path.open("x", encoding="utf-8") as report_file:
            report_file.write(content)
    except OSError as exc:
        raise _report_error(
            "REPORT_WRITE_ERROR",
            "Не удалось безопасно сохранить погодный отчёт.",
        ) from exc
    return SavedReport(saved=True, file_name=file_name, path=str(path), format="markdown")


def _safe_slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return slug[:80] or "weather"


def _markdown_report(analysis: WeatherAnalysis) -> str:
    conditions = "\n".join(f"- {condition}" for condition in analysis.conditions) or "- Not available"
    return f"""# Weather report: {analysis.city}

Period: {analysis.period.from_} - {analysis.period.to} ({analysis.period.days} days)

## Temperature

- Minimum: {analysis.temperature.min_c:g} °C
- Maximum: {analysis.temperature.max_c:g} °C
- Average: {analysis.temperature.avg_c:g} °C
- Change over period: {analysis.temperature.change_c:+g} °C

## Humidity

- Average: {analysis.humidity.avg_percent:g}%

## Wind

- Maximum speed: {analysis.wind.max_kmh:g} km/h

## Precipitation

- Total: {analysis.precipitation.total_mm:g} mm
- Highest precipitation: {analysis.precipitation.max_day_mm:g} mm
- Wettest day: {analysis.precipitation.max_day}

## Conditions

{conditions}

## Highlights

- Coldest day: {analysis.coldest_day}
- Warmest day: {analysis.warmest_day}
- Temperature trend: {analysis.temperature_trend}
"""


def _report_error(code: str, message: str) -> ToolError:
    return ToolError(json.dumps({"error": code, "message": message}, ensure_ascii=False))
