import json

import pytest

from lr4_diagnostics.subscriptions import (
    ACTIVITY_SUBSCRIPTION,
    _subscription_start,
    parse_activity_message,
)


def test_activity_subscription_is_read_only_and_requests_raw_fields() -> None:
    payload = _subscription_start(
        "subscription-id",
        "LR4-TEST",
        "Bearer secret-token",
    )
    document = json.loads(payload["payload"]["data"])

    assert payload["type"] == "start"
    assert document["variables"] == {
        "serial": "LR4-TEST",
        "consumer": "app",
    }
    assert "litterRobot4ActivitySubscriptionBySerial" in ACTIVITY_SUBSCRIPTION
    assert "originalHex" in ACTIVITY_SUBSCRIPTION
    assert "mutation" not in ACTIVITY_SUBSCRIPTION.lower()


def test_parse_activity_message_extracts_only_the_activity_row() -> None:
    row = {
        "timestamp": "2026-07-27T12:00:00Z",
        "value": "robotCycleStateCatDetect",
        "originalHex": "0x4F0004",
    }
    message = {
        "type": "data",
        "payload": {
            "data": {
                "litterRobot4ActivitySubscriptionBySerial": row,
            }
        },
    }

    assert parse_activity_message(message) == row
    assert parse_activity_message({"type": "ka"}) is None


def test_parse_activity_message_reports_rejection_without_response_data() -> None:
    with pytest.raises(
        RuntimeError,
        match="rejected the raw activity subscription",
    ) as captured:
        parse_activity_message(
            {
                "type": "error",
                "payload": {"authorization": "must-not-leak"},
            }
        )

    assert "must-not-leak" not in str(captured.value)
