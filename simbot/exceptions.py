"""Exception hierarchy for the Sim API client.

All errors raised by `simbot.Client` derive from `SimBotError` so users can catch
broadly when they don't care about the specific failure mode.
"""

from __future__ import annotations


class SimBotError(Exception):
    """Base class for all simbot errors."""


class APIError(SimBotError):
    """Generic non-2xx response from the API.

    Attributes:
        status_code: HTTP status returned by the server.
        message:     Human-readable reason from the server, if available.
        code:        Stable machine-readable error code (e.g. INSUFFICIENT_CASH).
                     None if the server didn't return one (older deployments).
        response:    Raw response body (best-effort decoded).
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        response: object | None = None,
        code: str | None = None,
    ) -> None:
        suffix = f" ({code})" if code else ""
        super().__init__(f"[{status_code}] {message}{suffix}")
        self.status_code = status_code
        self.message = message
        self.code = code
        self.response = response


class AuthError(APIError):
    """401 — missing, invalid, or expired API key."""


class SubscriptionRequiredError(APIError):
    """403 — endpoint requires an active API subscription.

    `/api/sim/perception` and `/api/sim/stream` are subscription-only. Session-
    cookie auth (browser play) cannot reach them.
    """


class BadRequestError(APIError):
    """400 — invalid input or game-rule violation (e.g. insufficient cash)."""


class NoActiveRoundError(BadRequestError):
    """No round is currently active. The next round starts after a cooldown.

    Bots should sleep and retry rather than treating this as a hard failure.
    """


class RateLimitError(APIError):
    """429 — too many requests. The client retries automatically with backoff.

    If you see this from user code, it means automatic retries were exhausted
    or you disabled them. Check `retry_after_seconds` and back off.
    """

    def __init__(
        self,
        message: str,
        retry_after_seconds: float | None = None,
        response: object | None = None,
        code: str | None = "RATE_LIMIT_EXCEEDED",
    ) -> None:
        super().__init__(429, message, response, code=code)
        self.retry_after_seconds = retry_after_seconds
