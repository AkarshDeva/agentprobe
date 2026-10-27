import time
import uuid

import requests

from config import TARGET_URL
from schemas import TargetReply

# TripMate can take 20-40 seconds, so we wait up to 150 seconds.
TIMEOUT_SECONDS = 150

# Different agents name their reply field differently. We try these in order.
REPLY_KEYS = ("answer", "response", "reply", "output", "text", "message")
ERROR_KEYS = ("error", "detail")


def _pick(data, keys):
    """Return the first non-empty value found under any of the keys."""
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value:
                return str(value)
    return ""


def send_to_target(message, url=None):
    """Send ONE message to the target agent and describe what happened."""
    url = url or TARGET_URL
    payload = {"message": message, "thread_id": f"probe-{uuid.uuid4().hex[:12]}"}
    started = time.perf_counter()

    try:
        response = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    except requests.exceptions.Timeout:
        return TargetReply(
            ok=False,
            latency_seconds=round(time.perf_counter() - started, 2),
            error=f"Timed out after {TIMEOUT_SECONDS} seconds.",
        )
    except requests.exceptions.RequestException as exc:
        return TargetReply(
            ok=False,
            latency_seconds=round(time.perf_counter() - started, 2),
            error=f"Could not reach the target: {type(exc).__name__}",
        )

    latency = round(time.perf_counter() - started, 2)

    try:
        data = response.json()
    except ValueError:
        data = None

    if response.status_code == 200:
        text = _pick(data, REPLY_KEYS) or response.text[:4000]
        return TargetReply(
            ok=True, status_code=200, text=text[:6000], latency_seconds=latency
        )

    # Any non-200 answer is still evidence: the judge will decide if it was fine.
    detail = _pick(data, ERROR_KEYS) or response.text[:500]
    return TargetReply(
        ok=False,
        status_code=response.status_code,
        text=detail,
        latency_seconds=latency,
        error=f"HTTP {response.status_code}",
    )