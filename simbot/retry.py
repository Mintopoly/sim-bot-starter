"""Backoff and rate-limit handling.

The Sim API rate-limits API-key reads at 240/min and actions at 60/min, plus
extra caps on fork (30/min) and power (20/min). On 429, the response includes
`Retry-After` (seconds). This module centralises the wait+retry policy.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from simbot.exceptions import RateLimitError

T = TypeVar("T")


def parse_retry_after(response: httpx.Response) -> float | None:
    """Read `Retry-After` header. Returns seconds as a float, or None."""
    val = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def with_rate_limit_retry(
    fn: Callable[[], httpx.Response],
    *,
    max_retries: int = 3,
    base_backoff: float = 1.0,
    max_backoff: float = 30.0,
) -> httpx.Response:
    """Call fn() up to (1 + max_retries) times, retrying on HTTP 429.

    The first wait honors the server's Retry-After header. Subsequent waits use
    exponential backoff with jitter, capped at max_backoff. Returns the final
    response (which may still be a 429 if all retries are exhausted — caller
    decides whether to raise).
    """
    response: httpx.Response | None = None
    for attempt in range(max_retries + 1):
        response = fn()
        if response.status_code != 429:
            return response

        if attempt == max_retries:
            return response

        retry_after = parse_retry_after(response)
        if retry_after is None or retry_after <= 0:
            # Exponential backoff with full jitter
            backoff = min(base_backoff * (2**attempt), max_backoff)
            wait_s = random.uniform(0, backoff)
        else:
            wait_s = min(retry_after, max_backoff)
        time.sleep(wait_s)

    # Unreachable in practice (loop returns), but mypy-pleasing.
    assert response is not None
    return response


def raise_for_rate_limit(response: httpx.Response) -> None:
    """Raise RateLimitError if the response is a 429."""
    if response.status_code == 429:
        retry_after = parse_retry_after(response)
        try:
            body = response.json()
            message = str(body.get("error", "Rate limit exceeded"))
            code = (
                body.get("code", "RATE_LIMIT_EXCEEDED")
                if isinstance(body, dict)
                else "RATE_LIMIT_EXCEEDED"
            )
        except (ValueError, KeyError):
            message = "Rate limit exceeded"
            body = None
            code = "RATE_LIMIT_EXCEEDED"
        raise RateLimitError(
            message=message,
            retry_after_seconds=retry_after,
            response=body,
            code=code,
        )
