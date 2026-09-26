from __future__ import annotations

import logging
from datetime import timedelta
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from weather_mcp.persistence import WeatherRepository, WeatherSample, WeatherSchedule, utc_now
from weather_mcp.weather import WeatherResult, WeatherService

logger = logging.getLogger(__name__)
MIN_SCHEDULE_INTERVAL_SECONDS = 30


class WeatherScheduler:
    def __init__(self, repository: WeatherRepository, weather: WeatherService) -> None:
        self._repository = repository
        self._weather = weather
        self._scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30},
        )

    def start(self) -> None:
        self._scheduler.start()
        for schedule in self._repository.list_schedules(active_only=True):
            self._register(schedule)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def create_schedule(self, city: str, interval_seconds: int) -> tuple[WeatherSchedule, bool]:
        if interval_seconds < MIN_SCHEDULE_INTERVAL_SECONDS:
            raise ValueError(
                f"Weather schedule interval must be at least {MIN_SCHEDULE_INTERVAL_SECONDS} seconds."
            )
        now = utc_now()
        schedule = WeatherSchedule(
            schedule_id=str(uuid4()),
            city=city,
            interval_seconds=interval_seconds,
            active=True,
            created_at=now,
            last_run_at=None,
            next_run_at=now + timedelta(seconds=interval_seconds),
        )
        saved, created = self._repository.create_schedule(schedule)
        if created:
            self._register(saved)
        return saved, created

    def stop_schedule(self, schedule_id: str) -> WeatherSchedule | None:
        schedule = self._repository.stop_schedule(schedule_id)
        if schedule is not None:
            if self._scheduler.get_job(schedule_id) is not None:
                self._scheduler.remove_job(schedule_id)
        return schedule

    async def collect_now(self, city: str) -> WeatherSample:
        weather = await self._weather.get_current_weather(city)
        return self._save_weather(city, weather)

    def _register(self, schedule: WeatherSchedule) -> None:
        next_run_at = utc_now() + timedelta(seconds=schedule.interval_seconds)
        self._repository.set_next_run(schedule.schedule_id, next_run_at)
        self._scheduler.add_job(
            self._run_schedule,
            "interval",
            args=[schedule.schedule_id],
            seconds=schedule.interval_seconds,
            id=schedule.schedule_id,
            replace_existing=True,
            next_run_time=next_run_at,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )

    async def _run_schedule(self, schedule_id: str) -> None:
        schedule = self._repository.get_schedule(schedule_id)
        if schedule is None or not schedule.active:
            return
        ran_at = utc_now()
        self._repository.mark_schedule_run(
            schedule_id, ran_at, ran_at + timedelta(seconds=schedule.interval_seconds)
        )
        try:
            await self.collect_now(schedule.city)
        except Exception:
            # A provider failure is isolated to this execution; APScheduler retains the job.
            logger.exception("Scheduled weather collection failed", extra={"schedule_id": schedule_id})

    def _save_weather(self, city: str, weather: WeatherResult) -> WeatherSample:
        return self._repository.save_sample(
            WeatherSample(
                city=city,
                collected_at=utc_now(),
                temperature_c=weather.temperature_c,
                humidity_percent=weather.humidity_percent,
                wind_speed_kmh=weather.wind_speed_kmh,
                feels_like_c=weather.feels_like_c,
                pressure_hpa=weather.pressure_hpa,
                precipitation_mm=weather.precipitation_mm,
                cloud_cover_percent=weather.cloud_cover_percent,
                visibility_km=weather.visibility_km,
                condition=weather.condition,
            )
        )
