"""Read-only raw LR4 activity subscription."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from aiohttp import WSMsgType
from pylitterbot import Account

from .queries import LR4_ENDPOINT

_LOGGER = logging.getLogger(__name__)
_HOST = "lr4.iothings.site"

ACTIVITY_SUBSCRIPTION = """
subscription Activity($serial: String!, $consumer: String) {
  litterRobot4ActivitySubscriptionBySerial(
    serial: $serial, consumer: $consumer
  ) {
    serial measure timestamp value actionValue originalHex valueString stateString
    consumer commandSource
  }
}
"""


async def stream_activity(
    account: Account,
    serial: str,
    on_row: Callable[[dict[str, Any]], None],
) -> None:
    """Continuously stream raw activity, reconnecting without affecting polling."""
    delay = 1.0
    while True:
        try:
            await _stream_once(account, serial, on_row)
            delay = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "Raw activity subscription unavailable (%s); retrying in %.1fs",
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)


async def _stream_once(
    account: Account,
    serial: str,
    on_row: Callable[[dict[str, Any]], None],
) -> None:
    authorization = await account.get_bearer_authorization()
    if authorization is None:
        raise RuntimeError("Whisker did not provide subscription authorization.")

    query = urlencode(
        {
            "header": _encode({"Authorization": authorization, "host": _HOST}),
            "payload": _encode({}),
        }
    )
    url = f"{LR4_ENDPOINT}/realtime?{query}"
    subscription_id = str(uuid4())

    async with account.session.websession.ws_connect(
        url,
        headers={"sec-websocket-protocol": "graphql-ws"},
    ) as websocket:
        await websocket.send_json(_subscription_start(subscription_id, serial, authorization))
        _LOGGER.info("Raw activity subscription connected.")
        try:
            async for message in websocket:
                if message.type == WSMsgType.TEXT:
                    row = parse_activity_message(message.json())
                    if row is not None:
                        on_row(row)
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            if not websocket.closed:
                await websocket.send_json({"id": subscription_id, "type": "stop"})


def _subscription_start(subscription_id: str, serial: str, authorization: str) -> dict[str, Any]:
    return {
        "id": subscription_id,
        "payload": {
            "data": json.dumps(
                {
                    "query": ACTIVITY_SUBSCRIPTION,
                    "variables": {"serial": serial, "consumer": "app"},
                }
            ),
            "extensions": {
                "authorization": {
                    "Authorization": authorization,
                    "host": _HOST,
                }
            },
        },
        "type": "start",
    }


def parse_activity_message(message: object) -> dict[str, Any] | None:
    """Extract an activity row without logging subscription response contents."""
    if not isinstance(message, dict):
        return None
    message_type = message.get("type")
    if message_type == "error":
        raise RuntimeError("Whisker rejected the raw activity subscription.")
    if message_type != "data":
        return None
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    row = data.get("litterRobot4ActivitySubscriptionBySerial")
    return row if isinstance(row, dict) else None


def _encode(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()
