"""SQLite persistence for redacted diagnostic events."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from typing import Any

from .redact import redact_payload
from .sensors import diagnostics_sample_fields, state_sample_fields

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    robot_id TEXT NOT NULL,
    source_timestamp TEXT,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS events_dedupe
ON events(source, robot_id, payload_hash);
CREATE INDEX IF NOT EXISTS events_observed_at
ON events(observed_at);
CREATE INDEX IF NOT EXISTS events_source
ON events(source);

-- Sensor readings are intentionally NOT deduplicated: an unchanged value at a
-- later time is itself evidence, and the events dedupe index would drop it.
CREATE TABLE IF NOT EXISTS sensor_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at TEXT NOT NULL,
    robot_id TEXT NOT NULL,
    source TEXT NOT NULL,
    robot_status TEXT,
    cycle_state TEXT,
    cycle_status TEXT,
    litter_level_mm REAL,
    litter_level_pct REAL,
    dfi_level_mm REAL,
    dfi_level_pct REAL,
    weight_sensor REAL,
    cat_weight REAL,
    is_laser_dirty INTEGER,
    tof_left REAL,
    tof_middle REAL,
    tof_right REAL,
    tof_slope_left REAL,
    tof_slope_middle REAL,
    tof_slope_right REAL
);
CREATE INDEX IF NOT EXISTS sensor_samples_sampled_at
ON sensor_samples(sampled_at);
"""

SENSOR_FIELDS = (
    "robot_status",
    "cycle_state",
    "cycle_status",
    "litter_level_mm",
    "litter_level_pct",
    "dfi_level_mm",
    "dfi_level_pct",
    "weight_sensor",
    "cat_weight",
    "is_laser_dirty",
    "tof_left",
    "tof_middle",
    "tof_right",
    "tof_slope_left",
    "tof_slope_middle",
    "tof_slope_right",
)


class EventStore:
    """Append-only, deduplicated event storage."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        # Capture and the autoreset watchdog are separate processes writing to
        # one database; wait for the lock rather than failing the write.
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        """Commit pending writes and close the database."""
        self.connection.commit()
        self.connection.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def add(
        self,
        *,
        observed_at: str,
        source: str,
        robot_id: str,
        payload: Any,
        source_timestamp: str | None = None,
    ) -> bool:
        """Persist a redacted record, returning whether it was newly inserted."""
        redacted = redact_payload(payload)
        payload_json = json.dumps(redacted, sort_keys=True, separators=(",", ":"), default=str)
        payload_hash = sha256(payload_json.encode("utf-8")).hexdigest()
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO events(
                observed_at, source, robot_id, source_timestamp, payload_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (observed_at, source, robot_id, source_timestamp, payload_hash, payload_json),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def add_sensor_sample(
        self,
        *,
        sampled_at: str,
        robot_id: str,
        source: str,
        **fields: Any,
    ) -> None:
        """Append one sensor reading; unchanged repeats are kept on purpose."""
        unknown = set(fields) - set(SENSOR_FIELDS)
        if unknown:
            raise ValueError(f"Unknown sensor fields: {sorted(unknown)}")
        columns = ["sampled_at", "robot_id", "source", *SENSOR_FIELDS]
        values: list[Any] = [sampled_at, robot_id, source]
        values.extend(fields.get(name) for name in SENSOR_FIELDS)
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO sensor_samples({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.connection.commit()

    def sensor_samples(self) -> Iterator[dict[str, Any]]:
        """Yield every sensor reading in observation order."""
        columns = ["sampled_at", "robot_id", "source", *SENSOR_FIELDS]
        rows = self.connection.execute(
            f"SELECT {', '.join(columns)} FROM sensor_samples ORDER BY sampled_at, id"
        )
        for row in rows:
            yield dict(zip(columns, row, strict=True))

    def backfill_sensor_samples(self) -> int:
        """Derive sensor rows from already-recorded events, once.

        Returns the number of rows inserted. Recorded events predate the
        sensor table, so without this the analysis sees no sensor history.
        """
        (existing,) = self.connection.execute("SELECT COUNT(*) FROM sensor_samples").fetchone()
        if existing:
            return 0
        rows = self.connection.execute(
            """
            SELECT observed_at, robot_id, source, payload_json
            FROM events
            WHERE source IN ('state', 'diagnostics')
            ORDER BY id
            """
        ).fetchall()
        inserted = 0
        for observed_at, robot_id, source, payload_json in rows:
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                continue
            fields = (
                state_sample_fields(payload)
                if source == "state"
                else diagnostics_sample_fields(payload)
            )
            self.add_sensor_sample(
                sampled_at=observed_at,
                robot_id=robot_id,
                source=source,
                **fields,
            )
            inserted += 1
        return inserted

    def payloads(self, source: str) -> Iterator[dict[str, Any]]:
        """Yield decoded payloads recorded for one source."""
        rows = self.connection.execute(
            "SELECT payload_json FROM events WHERE source = ? ORDER BY id",
            (source,),
        )
        for (payload_json,) in rows:
            payload = json.loads(payload_json)
            if isinstance(payload, dict):
                yield payload

    def counts(self) -> dict[str, int]:
        """Return record counts grouped by source."""
        rows = self.connection.execute(
            "SELECT source, COUNT(*) FROM events GROUP BY source ORDER BY source"
        )
        return {str(source): int(count) for source, count in rows}

    def recent(self, limit: int = 10) -> Iterator[dict[str, Any]]:
        """Yield the most recently observed records."""
        rows = self.connection.execute(
            """
            SELECT observed_at, source, robot_id, source_timestamp, payload_json
            FROM events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        for observed_at, source, robot_id, source_timestamp, payload_json in rows:
            yield {
                "observed_at": observed_at,
                "source": source,
                "robot_id": robot_id,
                "source_timestamp": source_timestamp,
                "payload": json.loads(payload_json),
            }
