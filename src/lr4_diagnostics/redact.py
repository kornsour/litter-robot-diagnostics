"""Redact direct identifiers before diagnostic data is persisted."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

REDACTED = "[redacted]"

_SERIAL_KEYS = {"serial", "robotserial"}
_PET_KEYS = {"frompetid", "petid", "topetid"}
_DIRECT_IDENTIFIER_KEYS = {
    "clientid",
    "deviceid",
    "email",
    "lbdeviceid",
    "name",
    "mbdeviceid",
    "principalidentifier",
    "rtcchipid",
    "sessionid",
    "unitid",
    "userid",
}
_SECRET_KEYS = {
    "accesstoken",
    "authorization",
    "idtoken",
    "password",
    "refreshtoken",
    "token",
}


def pseudonym(value: object, prefix: str = "lr4") -> str:
    """Return a stable, one-way short identifier."""
    digest = sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def redact_payload(payload: Any) -> Any:
    """Recursively remove secrets and direct identifiers from a payload."""
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            normalized = str(key).replace("_", "").lower()
            if normalized in _SECRET_KEYS:
                redacted[str(key)] = REDACTED
            elif normalized in _SERIAL_KEYS and value is not None:
                redacted[str(key)] = pseudonym(value)
            elif normalized in _PET_KEYS and value is not None:
                redacted[str(key)] = pseudonym(value, prefix="pet")
            elif normalized in _DIRECT_IDENTIFIER_KEYS and value is not None:
                redacted[str(key)] = REDACTED
            else:
                redacted[str(key)] = redact_payload(value)
        return redacted
    if isinstance(payload, list):
        return [redact_payload(value) for value in payload]
    if isinstance(payload, tuple):
        return [redact_payload(value) for value in payload]
    return payload
