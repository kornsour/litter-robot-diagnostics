from pathlib import Path

import pytest

from lr4_diagnostics.store import EventStore


def test_store_redacts_and_deduplicates(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite"

    with EventStore(database) as store:
        inserted = store.add(
            observed_at="2026-07-25T12:00:00+00:00",
            source="state",
            robot_id="lr4-test",
            source_timestamp="2026-07-25T11:59:59+00:00",
            payload={"serial": "SECRET", "weightSensor": 7.5},
        )
        duplicate = store.add(
            observed_at="2026-07-25T12:00:01+00:00",
            source="state",
            robot_id="lr4-test",
            source_timestamp="2026-07-25T11:59:59+00:00",
            payload={"serial": "SECRET", "weightSensor": 7.5},
        )

        assert inserted is True
        assert duplicate is False
        assert store.counts() == {"state": 1}
        [record] = list(store.recent())
        assert record["payload"]["serial"].startswith("lr4-")
        assert record["payload"]["weightSensor"] == 7.5


def test_sensor_samples_keep_unchanged_repeats(tmp_path: Path) -> None:
    """Identical readings must survive; the events dedupe would drop them."""
    with EventStore(tmp_path / "events.sqlite") as store:
        for moment in ("2026-07-25T12:00:00+00:00", "2026-07-25T12:00:30+00:00"):
            store.add_sensor_sample(
                sampled_at=moment,
                robot_id="lr4-test",
                source="state",
                litter_level_mm=451.0,
                weight_sensor=2.7,
            )
        samples = list(store.sensor_samples())

    assert len(samples) == 2
    assert samples[0]["litter_level_mm"] == 451.0
    assert samples[0]["tof_left"] is None


def test_sensor_samples_reject_unknown_columns(tmp_path: Path) -> None:
    with (
        EventStore(tmp_path / "events.sqlite") as store,
        pytest.raises(ValueError, match="Unknown sensor fields"),
    ):
        store.add_sensor_sample(
            sampled_at="2026-07-25T12:00:00+00:00",
            robot_id="lr4-test",
            source="state",
            bogus_column=1,
        )


def test_backfill_derives_samples_from_recorded_events(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite"
    with EventStore(database) as store:
        store.add(
            observed_at="2026-07-25T12:00:00+00:00",
            source="state",
            robot_id="lr4-test",
            payload={"litterLevel": 451, "weightSensor": 2.7, "isLaserDirty": False},
        )
        store.add(
            observed_at="2026-07-25T12:00:05+00:00",
            source="diagnostics",
            robot_id="lr4-test",
            payload={"ToFSensorSlopes": {"ToFSensorSlopeLeft": 3}},
        )

        assert store.backfill_sensor_samples() == 2
        # Backfill is one-shot so repeated `analyze` runs do not duplicate rows.
        assert store.backfill_sensor_samples() == 0

        samples = list(store.sensor_samples())

    assert samples[0]["litter_level_mm"] == 451.0
    assert samples[0]["is_laser_dirty"] == 0
    assert samples[1]["tof_slope_left"] == 3.0
    assert samples[1]["tof_left"] is None
