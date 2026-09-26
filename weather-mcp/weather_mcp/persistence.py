from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def from_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class WeatherSchedule:
    schedule_id: str
    city: str
    interval_seconds: int
    active: bool
    created_at: datetime
    last_run_at: datetime | None
    next_run_at: datetime | None


@dataclass(frozen=True)
class WeatherSample:
    city: str
    collected_at: datetime
    temperature_c: float
    humidity_percent: int
    wind_speed_kmh: float
    feels_like_c: float | None = None
    pressure_hpa: int | None = None
    precipitation_mm: float | None = None
    cloud_cover_percent: int | None = None
    visibility_km: float | None = None
    condition: str | None = None


class WeatherRepository:
    """Small SQLite store owned only by the weather MCP process."""

    def __init__(self, database_path: str) -> None:
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self._connection.close()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_schedules (
                schedule_id TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                active INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_run_at TEXT,
                next_run_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_weather_schedule_by_city_interval
            ON weather_schedules(city, interval_seconds) WHERE active = 1;
            CREATE TABLE IF NOT EXISTS weather_samples (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                temperature_c REAL NOT NULL,
                relative_humidity_percent INTEGER NOT NULL,
                wind_speed_kmh REAL NOT NULL,
                feels_like_c REAL,
                humidity_percent INTEGER,
                pressure_hpa INTEGER,
                precipitation_mm REAL,
                cloud_cover_percent INTEGER,
                visibility_km REAL,
                condition TEXT
            );
            CREATE INDEX IF NOT EXISTS weather_samples_by_city_collected_at
            ON weather_samples(city, collected_at);
            """
        )
        self._migrate_weather_samples()
        self._connection.commit()

    def _migrate_weather_samples(self) -> None:
        """Add nullable measurements while retaining existing schedule history."""
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(weather_samples)")
        }
        additions = {
            "feels_like_c": "REAL",
            "humidity_percent": "INTEGER",
            "pressure_hpa": "INTEGER",
            "precipitation_mm": "REAL",
            "cloud_cover_percent": "INTEGER",
            "visibility_km": "REAL",
            "condition": "TEXT",
        }
        for name, column_type in additions.items():
            if name not in columns:
                self._connection.execute(f"ALTER TABLE weather_samples ADD COLUMN {name} {column_type}")
        self._connection.execute(
            """UPDATE weather_samples SET humidity_percent = relative_humidity_percent
            WHERE humidity_percent IS NULL"""
        )

    def create_schedule(self, schedule: WeatherSchedule) -> tuple[WeatherSchedule, bool]:
        existing = self._connection.execute(
            """SELECT * FROM weather_schedules
            WHERE city = ? AND interval_seconds = ? AND active = 1""",
            (schedule.city, schedule.interval_seconds),
        ).fetchone()
        if existing is not None:
            return self._row_to_schedule(existing), False
        self._connection.execute(
            """INSERT INTO weather_schedules (
                schedule_id, city, interval_seconds, active, created_at, last_run_at, next_run_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                schedule.schedule_id,
                schedule.city,
                schedule.interval_seconds,
                schedule.active,
                to_timestamp(schedule.created_at),
                to_timestamp(schedule.last_run_at) if schedule.last_run_at else None,
                to_timestamp(schedule.next_run_at) if schedule.next_run_at else None,
            ),
        )
        self._connection.commit()
        return schedule, True

    def get_schedule(self, schedule_id: str) -> WeatherSchedule | None:
        row = self._connection.execute(
            "SELECT * FROM weather_schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        return self._row_to_schedule(row) if row else None

    def list_schedules(self, *, active_only: bool = False) -> list[WeatherSchedule]:
        query = "SELECT * FROM weather_schedules"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY created_at DESC"
        return [self._row_to_schedule(row) for row in self._connection.execute(query)]

    def stop_schedule(self, schedule_id: str) -> WeatherSchedule | None:
        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            return None
        self._connection.execute(
            "UPDATE weather_schedules SET active = 0, next_run_at = NULL WHERE schedule_id = ?",
            (schedule_id,),
        )
        self._connection.commit()
        return self.get_schedule(schedule_id)

    def mark_schedule_run(self, schedule_id: str, ran_at: datetime, next_run_at: datetime) -> None:
        self._connection.execute(
            """UPDATE weather_schedules SET last_run_at = ?, next_run_at = ?
            WHERE schedule_id = ? AND active = 1""",
            (to_timestamp(ran_at), to_timestamp(next_run_at), schedule_id),
        )
        self._connection.commit()

    def set_next_run(self, schedule_id: str, next_run_at: datetime) -> None:
        self._connection.execute(
            "UPDATE weather_schedules SET next_run_at = ? WHERE schedule_id = ? AND active = 1",
            (to_timestamp(next_run_at), schedule_id),
        )
        self._connection.commit()

    def save_sample(self, sample: WeatherSample) -> WeatherSample:
        self._connection.execute(
            """INSERT INTO weather_samples (
                city, collected_at, temperature_c, relative_humidity_percent, wind_speed_kmh,
                feels_like_c, humidity_percent, pressure_hpa, precipitation_mm,
                cloud_cover_percent, visibility_km, condition
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sample.city,
                to_timestamp(sample.collected_at),
                sample.temperature_c,
                sample.humidity_percent,
                sample.wind_speed_kmh,
                sample.feels_like_c,
                sample.humidity_percent,
                sample.pressure_hpa,
                sample.precipitation_mm,
                sample.cloud_cover_percent,
                sample.visibility_km,
                sample.condition,
            ),
        )
        self._connection.commit()
        return sample

    def samples_since(self, city: str, since: datetime, until: datetime) -> list[WeatherSample]:
        rows = self._connection.execute(
            """SELECT city, collected_at, temperature_c,
            COALESCE(humidity_percent, relative_humidity_percent) AS humidity_percent,
            wind_speed_kmh, feels_like_c, pressure_hpa, precipitation_mm, cloud_cover_percent,
            visibility_km, condition
            FROM weather_samples WHERE city = ? AND collected_at >= ? AND collected_at <= ?
            ORDER BY collected_at ASC""",
            (city, to_timestamp(since), to_timestamp(until)),
        )
        return [
            WeatherSample(
                city=row["city"],
                collected_at=from_timestamp(row["collected_at"]),
                temperature_c=row["temperature_c"],
                humidity_percent=row["humidity_percent"],
                wind_speed_kmh=row["wind_speed_kmh"],
                feels_like_c=row["feels_like_c"],
                pressure_hpa=row["pressure_hpa"],
                precipitation_mm=row["precipitation_mm"],
                cloud_cover_percent=row["cloud_cover_percent"],
                visibility_km=row["visibility_km"],
                condition=row["condition"],
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> WeatherSchedule:
        return WeatherSchedule(
            schedule_id=row["schedule_id"],
            city=row["city"],
            interval_seconds=row["interval_seconds"],
            active=bool(row["active"]),
            created_at=from_timestamp(row["created_at"]),
            last_run_at=from_timestamp(row["last_run_at"]),
            next_run_at=from_timestamp(row["next_run_at"]),
        )
