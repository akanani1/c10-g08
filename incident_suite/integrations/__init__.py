"""Slack and JIRA clients, and the pure functions that build their payloads.

Both clients send their requests through :func:`send_with_retry`, so they share one policy:
a timeout on every call, and up to three tries on a rate limit (429) or a server error (5xx),
waiting for ``Retry-After`` when the service sends it and backing off when it doesn't.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
ATTEMPTS = 3
BACKOFF_SECONDS = 1.0
MAX_WAIT_SECONDS = 30.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

Sleep = Callable[[float], None]


def _wait_seconds(response: httpx.Response | None, attempt: int) -> float:
    """``Retry-After`` when it's a number of seconds, else 1 s, 2 s, 4 s... Never above 30 s."""
    backoff: float = BACKOFF_SECONDS * 2.0**attempt
    header = response.headers.get("Retry-After") if response is not None else None
    if header is not None:
        with contextlib.suppress(ValueError):  # an HTTP date: keep our own backoff
            backoff = float(header)
    return min(max(backoff, 0.0), MAX_WAIT_SECONDS)


def send_with_retry(
    client: httpx.Client, method: str, url: str, sleep: Sleep = time.sleep, **kwargs: Any
) -> httpx.Response:
    """Send one request, retrying rate limits, server errors and failed connections.

    A failed connection is retried, but a read timeout isn't: the service may already have
    acted on the request, and a ticket sent twice is worse than one error. Raises
    ``httpx.HTTPError`` if the last try gets no response; otherwise returns the last response,
    whatever its status.
    """
    for attempt in range(ATTEMPTS - 1):
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            sleep(_wait_seconds(None, attempt))
            continue
        if response.status_code not in _RETRY_STATUSES:
            return response
        sleep(_wait_seconds(response, attempt))
    return client.request(method, url, **kwargs)
