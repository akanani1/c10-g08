"""Tests for the shared retry policy in incident_suite/integrations/__init__.py."""

from __future__ import annotations

import httpx
import pytest

from incident_suite.integrations import MAX_WAIT_SECONDS, send_with_retry


def _client(*replies: httpx.Response) -> tuple[httpx.Client, list[httpx.Request]]:
    queue = list(replies)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return queue.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


@pytest.mark.parametrize(
    ("headers", "waits"),
    [
        ({}, [1.0, 2.0]),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, [1.0, 2.0]),
        ({"Retry-After": "120"}, [MAX_WAIT_SECONDS, MAX_WAIT_SECONDS]),
        ({"Retry-After": "-5"}, [0.0, 0.0]),
    ],
)
def test_wait_between_tries(headers: dict[str, str], waits: list[float]) -> None:
    client, requests = _client(
        httpx.Response(502, headers=headers),
        httpx.Response(502, headers=headers),
        httpx.Response(200),
    )
    slept: list[float] = []
    response = send_with_retry(client, "GET", "https://api.example/x", slept.append)
    assert response.status_code == 200
    assert len(requests) == 3
    assert slept == waits


def test_client_errors_are_not_retried() -> None:
    client, requests = _client(httpx.Response(400))
    slept: list[float] = []
    assert send_with_retry(client, "POST", "https://api.example/x", slept.append).status_code == 400
    assert len(requests) == 1
    assert slept == []
