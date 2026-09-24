from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta

from mcp.server.mcpserver.exceptions import ToolError

from weather_mcp.persistence import WeatherRepository, WeatherSample, WeatherSchedule, utc_now
from weather_mcp.scheduler import WeatherScheduler
from weather_mcp.server import _build_summary
from weather_mcp.weather import WeatherResult


class FakeWeatherService:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[str] = []

    async def get_current_weather(self, city: str) -> WeatherResult:
        self.calls.append(city)
        if self.error:
            raise self.error
        return WeatherResult(
            city=city,
            country="Россия",
            latitude=55.0,
            longitude=82.0,
            temperature=12.4,
            apparentTemperature=11.2,
            relativeHumidity=71,
            windSpeed=8.6,
            weatherCode=0,
            weatherDescription="Ясно",
            observedAt="2026-09-24T15:30:00Z",
            timezone="UTC",
        )


class WeatherSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = f"{self.directory.name}/weather.db"
        self.repository = WeatherRepository(self.database_path)
        self.weather = FakeWeatherService()
        self.scheduler = WeatherScheduler(self.repository, self.weather)  # type: ignore[arg-type]
        self.scheduler.start()

    async def asyncTearDown(self) -> None:
        self.scheduler.shutdown()
        self.repository.close()
        self.directory.cleanup()

    async def test_create_duplicate_stop_and_persist_schedule(self) -> None:
        with self.assertRaises(ValueError):
            self.scheduler.create_schedule("Новосибирск", 0)

        created, was_created = self.scheduler.create_schedule("Новосибирск", 60)
        duplicate, duplicate_was_created = self.scheduler.create_schedule("Новосибирск", 60)

        self.assertTrue(was_created)
        self.assertFalse(duplicate_was_created)
        self.assertEqual(created.schedule_id, duplicate.schedule_id)
        self.assertIsNotNone(self.scheduler._scheduler.get_job(created.schedule_id))

        stopped = self.scheduler.stop_schedule(created.schedule_id)
        self.assertIsNotNone(stopped)
        self.assertFalse(stopped.active)  # type: ignore[union-attr]
        self.assertIsNone(self.scheduler._scheduler.get_job(created.schedule_id))

        self.repository.close()
        self.repository = WeatherRepository(self.database_path)
        saved = self.repository.get_schedule(created.schedule_id)
        self.assertIsNotNone(saved)
        self.assertFalse(saved.active)  # type: ignore[union-attr]

    async def test_active_schedule_is_restored_on_startup(self) -> None:
        now = utc_now()
        saved, was_created = self.repository.create_schedule(
            WeatherSchedule(
                schedule_id="restored-schedule",
                city="Омск",
                interval_seconds=60,
                active=True,
                created_at=now,
                last_run_at=None,
                next_run_at=now + timedelta(seconds=60),
            )
        )
        self.assertTrue(was_created)
        self.assertEqual(saved.schedule_id, "restored-schedule")

        recovered = WeatherScheduler(self.repository, self.weather)  # type: ignore[arg-type]
        recovered.start()
        try:
            self.assertIsNotNone(recovered._scheduler.get_job("restored-schedule"))
        finally:
            recovered.shutdown()

    async def test_manual_collection_saves_sample_and_summary(self) -> None:
        sample = await self.scheduler.collect_now("Новосибирск")
        self.assertEqual(sample.city, "Новосибирск")
        self.assertEqual(self.weather.calls, ["Новосибирск"])

        samples = self.repository.samples_since(
            "Новосибирск", utc_now() - timedelta(minutes=1), utc_now()
        )
        summary = _build_summary("Новосибирск", 60, samples)
        self.assertEqual(summary.samples, 1)
        self.assertEqual(summary.temperature.first, 12.4)  # type: ignore[union-attr]
        self.assertEqual(summary.relative_humidity_percent.avg, 71)  # type: ignore[union-attr]

    async def test_summary_without_samples_is_structured(self) -> None:
        summary = _build_summary("Новосибирск", 60, [])
        self.assertEqual(summary.samples, 0)
        self.assertIsNone(summary.temperature)
        self.assertIn("нет", summary.message or "")

    async def test_background_provider_error_keeps_schedule_active(self) -> None:
        schedule, _ = self.scheduler.create_schedule("Новосибирск", 60)
        self.weather.error = ToolError("Open-Meteo временно недоступен.")

        await self.scheduler._run_schedule(schedule.schedule_id)

        self.assertTrue(self.repository.get_schedule(schedule.schedule_id).active)  # type: ignore[union-attr]
        self.assertIsNotNone(self.scheduler._scheduler.get_job(schedule.schedule_id))
        self.assertEqual(self.repository.samples_since("Новосибирск", utc_now() - timedelta(minutes=1), utc_now()), [])

    async def test_summary_aggregates_persisted_samples(self) -> None:
        now = utc_now()
        for offset, temperature, humidity, wind in ((2, 10.0, 65, 3.2), (1, 13.0, 75, 12.4)):
            self.repository.save_sample(
                WeatherSample(
                    city="Новосибирск",
                    collected_at=now - timedelta(minutes=offset),
                    temperature_c=temperature,
                    relative_humidity_percent=humidity,
                    wind_speed_kmh=wind,
                )
            )

        summary = _build_summary(
            "Новосибирск",
            60,
            self.repository.samples_since("Новосибирск", now - timedelta(minutes=60), now),
        )
        self.assertEqual(summary.temperature.min, 10.0)  # type: ignore[union-attr]
        self.assertEqual(summary.temperature.max, 13.0)  # type: ignore[union-attr]
        self.assertEqual(summary.temperature.avg, 11.5)  # type: ignore[union-attr]
        self.assertEqual(summary.temperature.first, 10.0)  # type: ignore[union-attr]
        self.assertEqual(summary.temperature.last, 13.0)  # type: ignore[union-attr]
