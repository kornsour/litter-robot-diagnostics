from lr4_diagnostics.redact import REDACTED, pseudonym, redact_payload


def test_pseudonym_is_stable_and_does_not_expose_input() -> None:
    first = pseudonym("LR4-SECRET-SERIAL")
    second = pseudonym("LR4-SECRET-SERIAL")

    assert first == second
    assert first.startswith("lr4-")
    assert "SECRET" not in first


def test_redact_payload_recurses_and_preserves_diagnostics() -> None:
    result = redact_payload(
        {
            "serial": "LR4-SECRET",
            "mbDeviceId": "main-board-device-id",
            "RTCChipId": "rtc-chip-id",
            "userId": "user-123",
            "sessionId": "session-123",
            "petId": "pet-secret",
            "robotSerial": "LR4-SECRET",
            "traceId": "trace-useful-for-support",
            "weightSensor": 12.34,
            "nested": [{"password": "nope", "motorFaultAmperage": 42}],
        }
    )

    assert result["serial"].startswith("lr4-")
    assert result["mbDeviceId"] == REDACTED
    assert result["RTCChipId"] == REDACTED
    assert result["userId"] == REDACTED
    assert result["sessionId"] == REDACTED
    assert result["petId"].startswith("pet-")
    assert result["robotSerial"].startswith("lr4-")
    assert result["traceId"] == "trace-useful-for-support"
    assert result["weightSensor"] == 12.34
    assert result["nested"][0]["password"] == REDACTED
    assert result["nested"][0]["motorFaultAmperage"] == 42
