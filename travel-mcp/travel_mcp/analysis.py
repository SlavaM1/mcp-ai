from __future__ import annotations

from decimal import Decimal
from os import getenv
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_COLD_THRESHOLD_C = 10.0
DEFAULT_HOT_THRESHOLD_C = 30.0
DEFAULT_STRONG_WIND_THRESHOLD_KMH = 40.0


def _configured_threshold(name: str, default: float) -> float:
    try:
        return float(getenv(name, str(default)))
    except ValueError:
        return default


COLD_THRESHOLD_C = _configured_threshold("TRAVEL_COLD_THRESHOLD_C", DEFAULT_COLD_THRESHOLD_C)
HOT_THRESHOLD_C = _configured_threshold("TRAVEL_HOT_THRESHOLD_C", DEFAULT_HOT_THRESHOLD_C)
STRONG_WIND_THRESHOLD_KMH = _configured_threshold(
    "TRAVEL_STRONG_WIND_THRESHOLD_KMH", DEFAULT_STRONG_WIND_THRESHOLD_KMH
)

RAIN_RECOMMENDATION = "take_umbrella"
COLD_RECOMMENDATION = "take_warm_layer"
HOT_RECOMMENDATION = "take_water"
STRONG_WIND_RECOMMENDATION = "prepare_for_strong_wind"


class ResolvedLocation(BaseModel):
    city: str
    region: str | None = None
    country: str | None = None
    latitude: str | None = None
    longitude: str | None = None


class ForecastDay(BaseModel):
    date: str
    min_temperature_c: float
    max_temperature_c: float
    avg_temperature_c: float
    humidity_percent: float
    max_wind_kmh: float
    precipitation_mm: float
    conditions: list[str]
    sunrise: str | None = None
    sunset: str | None = None
    moon_phase: str | None = None


class WeatherForecastResult(BaseModel):
    requested_city: str
    resolved_location: ResolvedLocation
    city: str
    forecast: list[ForecastDay] = Field(min_length=1)


class CurrencyConversionResult(BaseModel):
    amount: Decimal
    from_currency: str
    to_currency: str
    rate: Decimal
    converted_amount: Decimal
    as_of: str
    provider: str


class TripPeriod(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    days: int
    from_: str = Field(alias="from")
    to: str


class TripWeather(BaseModel):
    min_temperature_c: float
    max_temperature_c: float
    avg_temperature_c: float
    rain_expected: bool
    max_wind_kmh: float


class TripBudget(BaseModel):
    original_amount: Decimal
    original_currency: str
    converted_amount: Decimal
    destination_currency: str
    rate: Decimal


class TripFlags(BaseModel):
    rain_expected: bool
    cold_weather: bool
    hot_weather: bool
    strong_wind: bool


RecommendationCode = Literal[
    "take_umbrella",
    "take_warm_layer",
    "take_water",
    "prepare_for_strong_wind",
]


class TripAnalysisResult(BaseModel):
    destination: str
    period: TripPeriod
    weather: TripWeather
    budget: TripBudget
    flags: TripFlags
    recommendations: list[RecommendationCode]


def build_trip_analysis(
    destination: str,
    weather_data: WeatherForecastResult,
    budget_data: CurrencyConversionResult,
) -> TripAnalysisResult:
    days = weather_data.forecast
    minimum_temperature = min(day.min_temperature_c for day in days)
    maximum_temperature = max(day.max_temperature_c for day in days)
    average_temperature = round(
        sum(day.avg_temperature_c for day in days) / len(days), 1
    )
    maximum_wind = max(day.max_wind_kmh for day in days)
    condition_text = " ".join(
        condition for day in days for condition in day.conditions
    ).casefold()
    rain_expected = any(day.precipitation_mm > 0 for day in days) or any(
        marker in condition_text for marker in ("rain", "precip", "дожд", "осад")
    )
    cold_weather = minimum_temperature < COLD_THRESHOLD_C
    hot_weather = maximum_temperature > HOT_THRESHOLD_C
    strong_wind = maximum_wind >= STRONG_WIND_THRESHOLD_KMH

    recommendations: list[RecommendationCode] = []
    if rain_expected:
        recommendations.append(RAIN_RECOMMENDATION)
    if cold_weather:
        recommendations.append(COLD_RECOMMENDATION)
    if hot_weather:
        recommendations.append(HOT_RECOMMENDATION)
    if strong_wind:
        recommendations.append(STRONG_WIND_RECOMMENDATION)

    return TripAnalysisResult(
        destination=destination,
        period=TripPeriod(
            days=len(days),
            **{"from": days[0].date},
            to=days[-1].date,
        ),
        weather=TripWeather(
            min_temperature_c=minimum_temperature,
            max_temperature_c=maximum_temperature,
            avg_temperature_c=average_temperature,
            rain_expected=rain_expected,
            max_wind_kmh=maximum_wind,
        ),
        budget=TripBudget(
            original_amount=budget_data.amount,
            original_currency=budget_data.from_currency,
            converted_amount=budget_data.converted_amount,
            destination_currency=budget_data.to_currency,
            rate=budget_data.rate,
        ),
        flags=TripFlags(
            rain_expected=rain_expected,
            cold_weather=cold_weather,
            hot_weather=hot_weather,
            strong_wind=strong_wind,
        ),
        recommendations=recommendations,
    )
