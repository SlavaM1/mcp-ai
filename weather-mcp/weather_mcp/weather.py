from __future__ import annotations

import json
import logging
import math
from typing import Any
from urllib.parse import quote

import httpx2
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

CITY_NOT_FOUND = "CITY_NOT_FOUND"
PROVIDER_TIMEOUT = "WEATHER_PROVIDER_TIMEOUT"
PROVIDER_UNAVAILABLE = "WEATHER_PROVIDER_UNAVAILABLE"
INVALID_RESPONSE = "WEATHER_PROVIDER_INVALID_RESPONSE"

_ERROR_MESSAGES = {
    CITY_NOT_FOUND: "Город не найден. Проверьте название и попробуйте снова.",
    PROVIDER_TIMEOUT: "wttr.in не ответил за отведённое время.",
    PROVIDER_UNAVAILABLE: "wttr.in временно недоступен.",
    INVALID_RESPONSE: "wttr.in вернул некорректный ответ.",
}


class ResolvedLocation(BaseModel):
    city: str
    region: str | None = None
    country: str | None = None
    latitude: str | None = None
    longitude: str | None = None


class WeatherResult(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    requested_city: str
    resolved_location: ResolvedLocation
    city: str
    country: str | None = None
    temperature_c: float
    feels_like_c: float | None = None
    condition: str | None = None
    humidity_percent: int
    pressure_hpa: int | None = None
    wind_speed_kmh: float
    wind_direction: str | None = None
    wind_direction_degrees: int | None = None
    precipitation_mm: float | None = None
    cloud_cover_percent: int | None = None
    visibility_km: float | None = None
    uv_index: int | None = None
    observation_time: str | None = None
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
    forecast: list[ForecastDay]


class HourlyWeather(BaseModel):
    time: str
    temperature_c: float
    feels_like_c: float | None = None
    condition: str | None = None
    humidity_percent: int
    precipitation_mm: float | None = None
    chance_of_rain_percent: int | None = None
    wind_speed_kmh: float
    wind_direction: str | None = None


class HourlyWeatherResult(BaseModel):
    requested_city: str
    resolved_location: ResolvedLocation
    city: str
    date: str
    hours: list[HourlyWeather]


class WttrWeatherClient:
    """Fetches wttr.in JSON without exposing provider-specific failures to MCP tools."""

    def __init__(self, client: httpx2.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def get_weather(self, city: str) -> dict[str, Any]:
        if not city.strip():
            raise _tool_error(CITY_NOT_FOUND)
        url = f"{self._base_url}/{quote(city, safe='')}"
        try:
            response = await self._client.get(url, params={"format": "j1"})
            response.raise_for_status()
        except httpx2.TimeoutException as exc:
            logger.warning(
                "Weather provider request timed out",
                extra={"event": "weather_provider_error", "provider": "wttr.in", "error_category": "timeout"},
            )
            raise _tool_error(PROVIDER_TIMEOUT) from exc
        except httpx2.HTTPStatusError as exc:
            logger.warning(
                "Weather provider returned an error status",
                extra={
                    "event": "weather_provider_error",
                    "provider": "wttr.in",
                    "error_category": "unavailable",
                    "status_code": exc.response.status_code,
                },
            )
            raise _tool_error(PROVIDER_UNAVAILABLE) from exc
        except httpx2.RequestError as exc:
            logger.warning(
                "Weather provider request failed",
                extra={"event": "weather_provider_error", "provider": "wttr.in", "error_category": "unavailable"},
            )
            raise _tool_error(PROVIDER_UNAVAILABLE) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise _tool_error(INVALID_RESPONSE) from exc
        if not isinstance(payload, dict):
            raise _tool_error(INVALID_RESPONSE)
        if _provider_reports_unknown_city(payload):
            raise _tool_error(CITY_NOT_FOUND)
        return payload


class WeatherService:
    """Normalizes wttr.in responses for MCP tools and background collection."""

    def __init__(self, client: WttrWeatherClient) -> None:
        self._client = client

    async def get_current_weather(self, city: str) -> WeatherResult:
        payload = await self._client.get_weather(city)
        current = _first_dict(payload.get("current_condition"))
        if current is None:
            raise _tool_error(INVALID_RESPONSE)
        location = _location(payload, city)
        try:
            result = WeatherResult(
                requested_city=city,
                resolved_location=location,
                city=location.city,
                country=location.country,
                temperature_c=_required_number(current, "temp_C"),
                feels_like_c=_optional_number(current, "FeelsLikeC"),
                condition=_text_value(current.get("weatherDesc")),
                humidity_percent=_required_int(current, "humidity"),
                pressure_hpa=_optional_int(current, "pressure"),
                wind_speed_kmh=_required_number(current, "windspeedKmph"),
                wind_direction=_optional_text(current.get("winddir16Point")),
                wind_direction_degrees=_optional_int(current, "winddirDegree"),
                precipitation_mm=_optional_number(current, "precipMM"),
                cloud_cover_percent=_optional_int(current, "cloudcover"),
                visibility_km=_optional_number(current, "visibility"),
                uv_index=_optional_int(current, "uvIndex"),
                observation_time=_optional_text(current.get("localObsDateTime")),
                latitude=location.latitude,
                longitude=location.longitude,
            )
        except (TypeError, ValueError) as exc:
            raise _tool_error(INVALID_RESPONSE) from exc
        logger.info("Weather request completed", extra={"event": "weather_request_succeeded", "provider": "wttr.in"})
        return result

    async def get_weather_forecast(self, city: str, days: int) -> WeatherForecastResult:
        payload = await self._client.get_weather(city)
        weather = payload.get("weather")
        if not isinstance(weather, list) or not weather:
            raise _tool_error(INVALID_RESPONSE)
        location = _location(payload, city)
        forecast: list[ForecastDay] = []
        try:
            for item in weather[:days]:
                if not isinstance(item, dict):
                    raise ValueError("weather item is not an object")
                astronomy = _first_dict(item.get("astronomy")) or {}
                humidity, max_wind, precipitation, conditions = _daily_weather_details(item)
                forecast.append(
                    ForecastDay(
                        date=_required_text(item, "date"),
                        min_temperature_c=_required_number(item, "mintempC"),
                        max_temperature_c=_required_number(item, "maxtempC"),
                        avg_temperature_c=_required_number(item, "avgtempC"),
                        humidity_percent=humidity,
                        max_wind_kmh=max_wind,
                        precipitation_mm=precipitation,
                        conditions=conditions,
                        sunrise=_optional_text(astronomy.get("sunrise")),
                        sunset=_optional_text(astronomy.get("sunset")),
                        moon_phase=_optional_text(astronomy.get("moon_phase")),
                    )
                )
        except (TypeError, ValueError) as exc:
            raise _tool_error(INVALID_RESPONSE) from exc
        if not forecast:
            raise _tool_error(INVALID_RESPONSE)
        return WeatherForecastResult(
            requested_city=city,
            resolved_location=location,
            city=location.city,
            forecast=forecast,
        )

    async def get_hourly_weather(self, city: str) -> HourlyWeatherResult:
        payload = await self._client.get_weather(city)
        weather = payload.get("weather")
        if not isinstance(weather, list) or not weather or not isinstance(weather[0], dict):
            raise _tool_error(INVALID_RESPONSE)
        day = weather[0]
        hourly = day.get("hourly")
        if not isinstance(hourly, list) or not hourly:
            raise _tool_error(INVALID_RESPONSE)
        location = _location(payload, city)
        try:
            hours: list[HourlyWeather] = []
            for item in hourly:
                if not isinstance(item, dict):
                    raise ValueError("hourly item is not an object")
                hours.append(
                    HourlyWeather(
                        time=_format_hour(_required_text(item, "time")),
                        temperature_c=_required_number(item, "tempC"),
                        feels_like_c=_optional_number(item, "FeelsLikeC"),
                        condition=_text_value(item.get("weatherDesc")),
                        humidity_percent=_required_int(item, "humidity"),
                        precipitation_mm=_optional_number(item, "precipMM"),
                        chance_of_rain_percent=_optional_int(item, "chanceofrain"),
                        wind_speed_kmh=_required_number(item, "windspeedKmph"),
                        wind_direction=_optional_text(item.get("winddir16Point")),
                    )
                )
            date = _required_text(day, "date")
        except (TypeError, ValueError) as exc:
            raise _tool_error(INVALID_RESPONSE) from exc
        if not hours:
            raise _tool_error(INVALID_RESPONSE)
        return HourlyWeatherResult(
            requested_city=city,
            resolved_location=location,
            city=location.city,
            date=date,
            hours=hours,
        )


def _location(payload: dict[str, Any], requested_city: str) -> ResolvedLocation:
    area = _first_dict(payload.get("nearest_area")) or {}
    return ResolvedLocation(
        city=_text_value(area.get("areaName")) or requested_city,
        region=_text_value(area.get("region")),
        country=_text_value(area.get("country")),
        latitude=_optional_text(area.get("latitude")),
        longitude=_optional_text(area.get("longitude")),
    )


def _provider_reports_unknown_city(payload: dict[str, Any]) -> bool:
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    errors = data.get("error")
    if not isinstance(errors, list):
        return False
    return any(_text_value(item.get("msg")) for item in errors if isinstance(item, dict))


def _daily_weather_details(item: dict[str, Any]) -> tuple[float, float, float, list[str]]:
    hourly = item.get("hourly")
    if not isinstance(hourly, list) or not hourly:
        raise ValueError("missing hourly forecast")
    humidities: list[int] = []
    wind_speeds: list[float] = []
    precipitation: list[float] = []
    conditions: list[str] = []
    for hour in hourly:
        if not isinstance(hour, dict):
            raise ValueError("hourly item is not an object")
        humidities.append(_required_int(hour, "humidity"))
        wind_speeds.append(_required_number(hour, "windspeedKmph"))
        precipitation.append(_required_number(hour, "precipMM"))
        condition = _text_value(hour.get("weatherDesc"))
        if condition:
            conditions.append(condition)
    if not conditions:
        raise ValueError("missing weather condition")
    return (
        round(sum(humidities) / len(humidities), 1),
        max(wind_speeds),
        round(sum(precipitation), 1),
        list(dict.fromkeys(conditions)),
    )


def _first_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        first = _first_dict(value)
        if first is not None:
            return _optional_text(first.get("value"))
    return None


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = _optional_text(payload.get(field))
    if value is None:
        raise ValueError(f"missing {field}")
    return value


def _optional_number(payload: dict[str, Any], field: str) -> float | None:
    value = payload.get(field)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid {field}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not math.isfinite(number):
        raise ValueError(f"invalid {field}")
    return number


def _required_number(payload: dict[str, Any], field: str) -> float:
    value = _optional_number(payload, field)
    if value is None:
        raise ValueError(f"missing {field}")
    return value


def _optional_int(payload: dict[str, Any], field: str) -> int | None:
    value = _optional_number(payload, field)
    if value is None:
        return None
    if not value.is_integer():
        raise ValueError(f"invalid {field}")
    return int(value)


def _required_int(payload: dict[str, Any], field: str) -> int:
    value = _optional_int(payload, field)
    if value is None:
        raise ValueError(f"missing {field}")
    return value


def _format_hour(value: str) -> str:
    if value.isdigit() and 0 <= int(value) <= 2359:
        padded = value.zfill(4)
        if int(padded[:2]) <= 23 and int(padded[2:]) <= 59:
            return f"{padded[:2]}:{padded[2:]}"
    return value


def _tool_error(code: str) -> ToolError:
    return ToolError(json.dumps({"error": code, "message": _ERROR_MESSAGES[code]}, ensure_ascii=False))
