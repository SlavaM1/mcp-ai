from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Annotated

import httpx2
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

CITY_NOT_FOUND = "Город не найден. Проверьте название и попробуйте снова."
PROVIDER_TIMEOUT = "Open-Meteo не ответил за отведённое время."
PROVIDER_UNAVAILABLE = "Open-Meteo временно недоступен."
INVALID_RESPONSE = "Open-Meteo вернул некорректный ответ."

logger = logging.getLogger(__name__)

WEATHER_DESCRIPTIONS_RU = {
    0: "Ясно",
    1: "Преимущественно ясно",
    2: "Переменная облачность",
    3: "Пасмурно",
    45: "Туман",
    48: "Туман с отложением изморози",
    51: "Слабая морось",
    53: "Умеренная морось",
    55: "Сильная морось",
    56: "Слабая ледяная морось",
    57: "Сильная ледяная морось",
    61: "Небольшой дождь",
    63: "Умеренный дождь",
    65: "Сильный дождь",
    66: "Слабый ледяной дождь",
    67: "Сильный ледяной дождь",
    71: "Небольшой снег",
    73: "Умеренный снег",
    75: "Сильный снег",
    77: "Снежные зерна",
    80: "Небольшой ливень",
    81: "Умеренный ливень",
    82: "Сильный ливень",
    85: "Небольшой снегопад",
    86: "Сильный снегопад",
    95: "Гроза",
    96: "Гроза со слабым градом",
    99: "Гроза с сильным градом",
}


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)


class _Location(_ProviderModel):
    name: Annotated[str, Field(min_length=1)]
    country: Annotated[str, Field(min_length=1)]
    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]


class _GeocodingResponse(_ProviderModel):
    generationtime_ms: float
    results: list[_Location] = Field(default_factory=list)


class _CurrentWeather(_ProviderModel):
    time: Annotated[datetime, Field(strict=False)]
    temperature_2m: float
    apparent_temperature: float
    relative_humidity_2m: Annotated[int, Field(ge=0, le=100)]
    wind_speed_10m: Annotated[float, Field(ge=0)]
    weather_code: Annotated[int, Field(ge=0)]


class _ForecastResponse(_ProviderModel):
    timezone: Annotated[str, Field(min_length=1)]
    utc_offset_seconds: Annotated[int, Field(gt=-86400, lt=86400)]
    current: _CurrentWeather


class WeatherResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    city: str
    country: str
    latitude: float
    longitude: float
    temperature: float = Field(description="Текущая температура, °C")
    apparent_temperature: float = Field(alias="apparentTemperature", description="Ощущаемая температура, °C")
    relative_humidity: int = Field(alias="relativeHumidity", description="Относительная влажность, %")
    wind_speed: float = Field(alias="windSpeed", description="Скорость ветра, км/ч")
    weather_code: int = Field(alias="weatherCode")
    weather_description: str = Field(alias="weatherDescription")
    observed_at: str = Field(alias="observedAt")
    timezone: str


class WeatherService:
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self._client = client

    async def get_current_weather(self, city: str) -> WeatherResult:
        geocoding_data = await self._request_json(
            GEOCODING_URL,
            params={"name": city, "count": 1, "language": "ru", "format": "json"},
            provider_endpoint="geocoding",
        )
        try:
            locations = _GeocodingResponse.model_validate(geocoding_data).results
        except ValidationError as exc:
            self._invalid_response("geocoding", exc)

        if not locations:
            logger.info(
                "City was not found by weather provider",
                extra={"event": "weather_request_failed", "provider": "open-meteo", "error_category": "not_found"},
            )
            raise ToolError(CITY_NOT_FOUND)

        location = locations[0]
        forecast_data = await self._request_json(
            FORECAST_URL,
            params={
                "latitude": location.latitude,
                "longitude": location.longitude,
                "current": ",".join(
                    (
                        "temperature_2m",
                        "apparent_temperature",
                        "relative_humidity_2m",
                        "wind_speed_10m",
                        "weather_code",
                    )
                ),
                "timezone": "auto",
                "wind_speed_unit": "kmh",
            },
            provider_endpoint="forecast",
        )
        try:
            forecast = _ForecastResponse.model_validate(forecast_data)
            observed_at = forecast.current.time
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(
                    tzinfo=timezone(timedelta(seconds=forecast.utc_offset_seconds))
                )
        except (ValidationError, ValueError, OverflowError) as exc:
            self._invalid_response("forecast", exc)

        current = forecast.current
        logger.info(
            "Weather request completed",
            extra={"event": "weather_request_succeeded", "provider": "open-meteo"},
        )
        return WeatherResult(
            city=location.name,
            country=location.country,
            latitude=location.latitude,
            longitude=location.longitude,
            temperature=current.temperature_2m,
            apparent_temperature=current.apparent_temperature,
            relative_humidity=current.relative_humidity_2m,
            wind_speed=current.wind_speed_10m,
            weather_code=current.weather_code,
            weather_description=WEATHER_DESCRIPTIONS_RU.get(
                current.weather_code, "Неизвестные погодные условия"
            ),
            observed_at=observed_at.isoformat(),
            timezone=forecast.timezone,
        )

    async def _request_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        provider_endpoint: str,
    ) -> Any:
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
        except httpx2.TimeoutException as exc:
            logger.warning(
                "Weather provider request timed out",
                extra={"event": "weather_provider_error", "provider": "open-meteo", "error_category": "timeout"},
            )
            raise ToolError(PROVIDER_TIMEOUT) from exc
        except httpx2.HTTPStatusError as exc:
            logger.warning(
                "Weather provider returned an error status",
                extra={
                    "event": "weather_provider_error",
                    "provider": "open-meteo",
                    "error_category": "unavailable",
                    "status_code": exc.response.status_code,
                },
            )
            raise ToolError(PROVIDER_UNAVAILABLE) from exc
        except httpx2.RequestError as exc:
            logger.warning(
                "Weather provider request failed",
                extra={"event": "weather_provider_error", "provider": "open-meteo", "error_category": "unavailable"},
            )
            raise ToolError(PROVIDER_UNAVAILABLE) from exc

        try:
            return response.json()
        except ValueError as exc:
            self._invalid_response(provider_endpoint, exc)

    @staticmethod
    def _invalid_response(provider_endpoint: str, exc: Exception) -> None:
        logger.warning(
            "Weather provider returned an invalid response",
            extra={
                "event": "weather_provider_error",
                "provider": "open-meteo",
                "error_category": "invalid_response",
            },
        )
        raise ToolError(INVALID_RESPONSE) from exc
