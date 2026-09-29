"""Synchronous HTTP client for the Children of Titan Sim API.

    from simbot import Client

    client = Client(api_key="sim_...")  # or omit and set SIM_API_KEY
    state = client.perception()                              # solo (OPEN sim)
    state = client.perception(tournament_id="rnd_abc")       # tournament round

The client is intentionally simple. It does:
  * Bearer auth (one header, sent on every request)
  * JSON request/response
  * Automatic retries on 429 (Retry-After-aware, exponential backoff fallback)
  * Translation of error responses into typed exceptions
  * A streaming iterator over Server-Sent Events from `/api/sim/stream`
  * Solo + tournament routing on a single method surface

It does NOT do:
  * Async (sync is enough for one bot per process — open an issue if you need async)
  * Caching (every call hits the network)
  * Background event loops (you call methods, you wait for the response)

Tournament context
------------------
Most read/write methods accept a `tournament_id: str | None = None` kwarg.
Pass it to target a specific tournament round; omit (default) to target the
OPEN sim round. Response DTO shapes are identical between the two surfaces
so existing parsers continue to work.

Methods that have NO tournament equivalent on the bot surface (sim-only):
`profile`, `history`, `cards`, `autostake_status`, `power_info`,
`buy_keys_info`, `buy_keys`, `stream`. Passing `tournament_id` to them raises
`ValueError`. For tournament cards / autostake / power state, read it off
`perception(tournament_id=...)` instead.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from simbot.exceptions import (
    APIError,
    AuthError,
    BadRequestError,
    NoActiveRoundError,
    SubscriptionRequiredError,
)
from simbot.retry import raise_for_rate_limit, with_rate_limit_retry
from simbot.stream import stream_events
from simbot.types import DarkWebPower, Path, PerceptionState, SimEvent

DEFAULT_BASE_URL = "https://www.childrenoftitan.com"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
STREAM_TIMEOUT = httpx.Timeout(None, connect=10.0)  # SSE stays open indefinitely

VALID_KEY_QUANTITIES = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)


def _sim_only(method_name: str, tournament_id: str | None) -> None:
    """Raise if a sim-only method is called with a tournament_id."""
    if tournament_id is not None:
        raise ValueError(
            f"{method_name}() has no tournament equivalent. Drop the tournament_id "
            f"kwarg, or use the appropriate tournament-only method."
        )


def _path_for(suffix: str, tournament_id: str | None) -> str:
    """Build the URL path for solo or tournament context.

    `suffix` is the final URL segment under /api/sim or /api/trainer/tournament/{id}.
    Pass an empty string for the bare round-state endpoint:
        _path_for("", None)             -> "/api/sim"
        _path_for("", "rnd_abc")        -> "/api/trainer/tournament/rnd_abc"
        _path_for("perception", None)   -> "/api/sim/perception"
        _path_for("perception", "abc")  -> "/api/trainer/tournament/abc/perception"
    """
    if tournament_id is None:
        return f"/api/sim/{suffix}".rstrip("/")
    return f"/api/trainer/tournament/{tournament_id}/{suffix}".rstrip("/")


class Client:
    """Sim API client.

    Args:
        api_key: Your Bearer token. If omitted, read from `SIM_API_KEY` env var.
        base_url: API host. Defaults to production. Override for staging.
        timeout: httpx Timeout, or a float-seconds value.
        retries: Max retries on HTTP 429. Default 3.
        user_agent: Custom UA string. Defaults to "simbot/<version>".
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: httpx.Timeout | float | None = None,
        retries: int = 3,
        user_agent: str | None = None,
    ) -> None:
        # Load .env if present, searching the directory the bot runs in and
        # its parents. Won't override vars already set in the shell, so
        # explicit env always wins.
        #
        # `usecwd=True` is load-bearing. Without it python-dotenv starts from
        # the directory of the code that CALLS it, which is this package: once
        # the kit is pip-installed outside the project, the project's .env is
        # never found and the README's first step fails with "API key
        # required". Inside a checkout it went the other way and always loaded
        # the kit's own .env, whatever directory the bot was started from.
        try:
            from dotenv import find_dotenv, load_dotenv

            dotenv_path = find_dotenv(usecwd=True)
            if dotenv_path:
                load_dotenv(dotenv_path)
        except ImportError:
            pass

        key = api_key or os.environ.get("SIM_API_KEY")
        if not key:
            raise ValueError(
                "API key required. Pass `api_key=...` or set the SIM_API_KEY environment "
                "variable. Get one at https://www.childrenoftitan.com/sim"
            )
        if not key.startswith("sim_"):
            # Deliberately repeats nothing of what was passed: a value that fails
            # this check is usually a different secret pasted by mistake, and this
            # message lands in logs and bug reports.
            raise ValueError(
                "API key must start with 'sim_'. Did you paste the wrong value?"
            )

        from simbot import __version__

        self._base_url = (base_url or os.environ.get("SIM_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._retries = max(0, retries)
        self._http = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "User-Agent": user_agent or f"simbot/{__version__}",
            },
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying connection pool. Safe to call multiple times."""
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ─── Internal request plumbing ────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        def call() -> httpx.Response:
            return self._http.request(method, path, json=json, params=params)

        response = with_rate_limit_retry(call, max_retries=self._retries)

        if response.status_code == 200:
            return response.json()

        # Translate to a typed exception
        try:
            body = response.json()
            message = str(body.get("error", "Unknown error"))
            code = body.get("code") if isinstance(body, dict) else None
        except (ValueError, KeyError):
            body = response.text
            message = response.text or "Unknown error"
            code = None

        if response.status_code == 401:
            raise AuthError(401, message, body, code=code)
        if response.status_code == 403:
            raise SubscriptionRequiredError(403, message, body, code=code)
        if response.status_code == 429:
            raise_for_rate_limit(response)  # raises RateLimitError
        if response.status_code == 400:
            # Prefer the stable code; fall back to message sniffing for older
            # API versions that don't yet return `code`.
            lc = message.lower()
            no_round = "no active sim round" in lc or "no active round" in lc
            if code == "NO_ACTIVE_ROUND" or (code is None and no_round):
                raise NoActiveRoundError(400, message, body, code=code or "NO_ACTIVE_ROUND")
            raise BadRequestError(400, message, body, code=code)

        raise APIError(response.status_code, message, body, code=code)

    # ─── Read endpoints (dual-mode: solo + tournament) ───────────────────

    def perception(self, *, tournament_id: str | None = None) -> PerceptionState:
        """Get the bot-optimized full game state. Subscription-only.

        Recommended polling endpoint — one call returns round, player,
        investments (with affordability/multipliers), events, fork analysis,
        dark web powers/targets, staking info, and leaderboard.

        Args:
            tournament_id: target a specific tournament round; omit for OPEN sim.
        """
        data = self._request("GET", _path_for("perception", tournament_id))
        return PerceptionState._from_json(data)

    def full_state(self, *, tournament_id: str | None = None) -> dict[str, Any]:
        """Get the full-state response.

        Solo: `GET /api/sim` (legacy DTO, less structured than `perception()`).
        Tournament: `GET /api/trainer/tournament/{id}` (round metadata + player
        state + leaderboard + events).

        Returned as raw dict so you can introspect freely.
        """
        return self._request("GET", _path_for("", tournament_id))  # type: ignore[no-any-return]

    def events(
        self,
        *,
        limit_active: int = 10,
        limit_upcoming: int = 5,
        limit_past: int = 10,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        """Active, upcoming, and recent newsfeed events plus your event effects.

        The `limit_*` query params are supported on the solo surface and
        ignored on the tournament surface (which currently returns all
        events; trim client-side if needed).
        """
        params = (
            None
            if tournament_id is not None
            else {
                "limitActive": limit_active,
                "limitUpcoming": limit_upcoming,
                "limitPast": limit_past,
            }
        )
        return self._request(  # type: ignore[no-any-return]
            "GET", _path_for("events", tournament_id), params=params
        )

    def paths(self, *, tournament_id: str | None = None) -> dict[str, Any]:
        """Path system info — your selected path, bonuses, aggregated effects."""
        return self._request("GET", _path_for("paths", tournament_id))  # type: ignore[no-any-return]

    def leaderboard(self, *, limit: int = 100, tournament_id: str | None = None) -> dict[str, Any]:
        """Leaderboard — top N players ranked by net worth."""
        return self._request(  # type: ignore[no-any-return]
            "GET",
            _path_for("leaderboard", tournament_id),
            params={"limit": min(max(limit, 1), 100)},
        )

    def buy_keys_info(self, *, tournament_id: str | None = None) -> dict[str, Any]:
        """Get key purchase options and current key + gem balance.

        Solo: hits `GET /api/sim/buy-keys`. Tournament: hits
        `GET /api/trainer/tournament/{id}/buy-keys` and returns the
        round-scoped key balance. Both shapes
        return `{currentKeys, gemsBalance, gemsPerKey, options: [...]}`.
        """
        return self._request("GET", _path_for("buy-keys", tournament_id))  # type: ignore[no-any-return]

    # ─── Account economy ─────────────────────────────────────────────────

    def gems_balance(self) -> dict[str, Any]:
        """Get your current gem balance and last 20 gem transactions.

        Gems are the user-level economy (not round-scoped) that funds key
        purchases, tournament entry/creation fees, and card purchases.
        Plan your spend with this before calling `buy_keys`,
        `register_for_tournament`, or `create_tournament`.

        Returns `{gems: number, transactions: [{id, amount, balance, type,
        description, createdAt}, ...]}`.
        """
        return self._request("GET", "/api/gems/balance")  # type: ignore[no-any-return]

    # ─── Read endpoints (sim-only) ───────────────────────────────────────

    def profile(
        self,
        *,
        user_id: str | None = None,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        """Your sim profile — lifetime stats, top finishes, tickets.

        Sim-only: tournament results come with `tournament_state(id)`.

        Pass `user_id` to view another player's public profile.
        """
        _sim_only("profile", tournament_id)
        params = {"userId": user_id} if user_id else None
        return self._request("GET", "/api/sim/profile", params=params)  # type: ignore[no-any-return]

    def history(self, *, tournament_id: str | None = None) -> dict[str, Any]:
        """Your transaction history for the current round (sim-only)."""
        _sim_only("history", tournament_id)
        return self._request("GET", "/api/sim/history")  # type: ignore[no-any-return]

    def cards(self, *, tournament_id: str | None = None) -> dict[str, Any]:
        """Your cards with sim boost effects (sim-only)."""
        _sim_only("cards", tournament_id)
        return self._request("GET", "/api/sim/cards")  # type: ignore[no-any-return]

    def autostake_status(self, *, tournament_id: str | None = None) -> dict[str, Any]:
        """Get current auto-stake status (sim-only).

        For tournament autostake state, read
        `perception(tournament_id=...).player.autoStakeEnabled`.
        """
        _sim_only("autostake_status", tournament_id)
        return self._request("GET", "/api/sim/autostake")  # type: ignore[no-any-return]

    def power_info(self, *, tournament_id: str | None = None) -> dict[str, Any]:
        """List dark-web powers, costs, your ticket balance, and targetable players (sim-only).

        For tournament power info, read `perception(tournament_id=...).dark_web`.
        """
        _sim_only("power_info", tournament_id)
        return self._request("GET", "/api/sim/power")  # type: ignore[no-any-return]

    # ─── Action endpoints (dual-mode: solo + tournament) ─────────────────

    def purchase(
        self,
        *,
        investment_id: int,
        action: str = "buy",
        quantity: int = 1,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        """Buy an investment or unlock a tier.

        Args:
            investment_id: 0..6
            action: "buy" or "unlock"
            quantity: ≥1 (ignored for unlock)
            tournament_id: target a specific tournament round; omit for OPEN sim.

        URL routing differs between surfaces:
          * Solo: POST /api/sim/purchase  body={investmentId, action, quantity?}
          * Tournament: POST /api/trainer/tournament/{id}/{purchase|unlock}
            body={investmentId, quantity?} or {investmentId}
        """
        if action not in {"buy", "unlock"}:
            raise ValueError("action must be 'buy' or 'unlock'")
        if not 0 <= investment_id <= 6:
            raise ValueError("investment_id must be 0..6")
        if action == "buy" and quantity < 1:
            raise ValueError("quantity must be ≥1 for buy")

        if tournament_id is None:
            # Solo: single endpoint with `action` discriminator.
            body: dict[str, Any] = {"investmentId": investment_id, "action": action}
            if action == "buy":
                body["quantity"] = quantity
            return self._request("POST", "/api/sim/purchase", json=body)  # type: ignore[no-any-return]

        # Tournament: separate /purchase vs /unlock endpoints.
        if action == "unlock":
            return self._request(  # type: ignore[no-any-return]
                "POST",
                _path_for("unlock", tournament_id),
                json={"investmentId": investment_id},
            )
        return self._request(  # type: ignore[no-any-return]
            "POST",
            _path_for("purchase", tournament_id),
            json={"investmentId": investment_id, "quantity": quantity},
        )

    def stake(self, *, amount: float, tournament_id: str | None = None) -> dict[str, Any]:
        """Stake cash. Subject to a staking lockout of 500 blocks after each stake.

        The Staking path cuts it to 100 blocks at most, and the Unlimited
        Staking power bypasses it while active. `state.staking.lockout_blocks_remaining`
        says when the next stake is allowed.

        Solo: POST /api/sim/stake body={action: "stake", amount}
        Tournament: POST /api/trainer/tournament/{id}/stake body={amount}
        """
        if amount <= 0:
            raise ValueError("amount must be positive")
        if tournament_id is None:
            return self._request(  # type: ignore[no-any-return]
                "POST", "/api/sim/stake", json={"action": "stake", "amount": amount}
            )
        return self._request(  # type: ignore[no-any-return]
            "POST", _path_for("stake", tournament_id), json={"amount": amount}
        )

    def unstake(self, *, amount: float, tournament_id: str | None = None) -> dict[str, Any]:
        """Unstake cash.

        Solo: POST /api/sim/stake body={action: "unstake", amount}
        Tournament: POST /api/trainer/tournament/{id}/unstake body={amount}
        """
        if amount <= 0:
            raise ValueError("amount must be positive")
        if tournament_id is None:
            return self._request(  # type: ignore[no-any-return]
                "POST", "/api/sim/stake", json={"action": "unstake", "amount": amount}
            )
        return self._request(  # type: ignore[no-any-return]
            "POST", _path_for("unstake", tournament_id), json={"amount": amount}
        )

    def fork(
        self,
        *,
        path: Path | str | None = None,
        bonus_id: str | None = None,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one fork request (prestige). Most bots want `complete_fork`.

        A fork takes two requests. The first returns what is still missing:
        the path on the first fork of a round, and a bonus draft on every
        fork. The second commits, and must carry the chosen `bonus_id`.

        On the first fork the path has to go out AGAIN together with the
        bonus: a request with only `path` returns the draft but does not
        store the path, so a bot that sends the path, waits a tick and then
        sends only the bonus is asked for a path again, for ever.
        `complete_fork` sends both requests correctly.

        Tournaments may have forking disabled (`disableFork` flag); in that
        case the call returns an error.
        """
        body: dict[str, Any] = {}
        if path is not None:
            body["path"] = path.value if isinstance(path, Path) else path
        if bonus_id is not None:
            body["bonusId"] = bonus_id
        return self._request("POST", _path_for("fork", tournament_id), json=body)  # type: ignore[no-any-return]

    def complete_fork(
        self,
        *,
        path: Path | str | None = None,
        choose: Callable[[list[dict[str, Any]]], str] | None = None,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        """Perform a whole fork in one call: fetch the draft, pick a bonus, commit.

        This is the safe way to fork. It sends the path with both requests,
        which the first fork of a round requires (see `fork`). Passing `path`
        on later forks is harmless: the server keeps the path chosen on the
        first fork and ignores it.

        Args:
            path: Required on the first fork of a round, ignored afterwards.
            choose: Picks a bonus id from the draft, a list of dicts with
                `id`, `name`, `description`, `effectType`, `value`, ...
                Defaults to the first option.
            tournament_id: target a tournament round; omit for the OPEN sim.

        Returns the committing response. If the server sent an empty draft,
        returns that response unchanged (`success` is False).

        Raises:
            ValueError: the first fork of a round was requested without a path.
        """
        prompt = self.fork(path=path, tournament_id=tournament_id)
        if prompt.get("success"):
            return prompt
        if prompt.get("requiresPathSelection"):
            raise ValueError(
                "The first fork of a round needs a path: pass path=Path.PRODUCTION, "
                "Path.STAKING or Path.DARK_WEB."
            )
        options = prompt.get("availableBonuses") or []
        if not options:
            return prompt
        bonus_id = choose(options) if choose is not None else str(options[0]["id"])
        return self.fork(path=path, bonus_id=bonus_id, tournament_id=tournament_id)

    def use_power(
        self,
        power: DarkWebPower | str,
        *,
        target_user_id: str | None = None,
        tournament_id: str | None = None,
    ) -> dict[str, Any]:
        """Use a dark-web power. Some powers (BLACKOUT, SHORT_CIRCUIT) require a target.

        Tournaments may have dark-web disabled (`disableDarkWeb` flag); in
        that case the call returns an error.
        """
        body: dict[str, Any] = {"power": power.value if isinstance(power, DarkWebPower) else power}
        if target_user_id is not None:
            body["targetUserId"] = target_user_id
        return self._request("POST", _path_for("power", tournament_id), json=body)  # type: ignore[no-any-return]

    def contribute(self, *, amount: float, tournament_id: str | None = None) -> dict[str, Any]:
        """Contribute cash to the round contribution goal."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        return self._request(  # type: ignore[no-any-return]
            "POST", _path_for("contribute", tournament_id), json={"amount": amount}
        )

    def set_autostake(self, enabled: bool, *, tournament_id: str | None = None) -> dict[str, Any]:
        """Toggle auto-staking. Requires the Auto-Staker (Titanbot) card."""
        return self._request(  # type: ignore[no-any-return]
            "POST",
            _path_for("autostake", tournament_id),
            json={"enabled": bool(enabled)},
        )

    def dismiss(self, *, tournament_id: str | None = None) -> dict[str, Any]:
        """Dismiss a short-circuit notification (mark as seen)."""
        return self._request("POST", _path_for("dismiss", tournament_id), json={})  # type: ignore[no-any-return]

    def buy_keys(self, *, quantity: int, tournament_id: str | None = None) -> dict[str, Any]:
        """Buy encryption keys with Gems.

        Solo: keys go to your sim key balance and stay spendable across
        future rounds. Tournament: keys go to that round's own key balance
        and transfer back to your sim balance when the round is finalized.
        Both surfaces deduct the gems in one step and record the purchase in
        your gem history.

        Valid quantities: 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000.
        Cost: `quantity * 3` gems.
        """
        if quantity not in VALID_KEY_QUANTITIES:
            raise ValueError(
                f"quantity must be one of {VALID_KEY_QUANTITIES}, got {quantity}"
            )
        return self._request(  # type: ignore[no-any-return]
            "POST", _path_for("buy-keys", tournament_id), json={"quantity": quantity}
        )

    # ─── Tournament-only endpoints ───────────────────────────────────────

    def list_tournaments(
        self,
        *,
        entry_mode: str | None = None,
        visibility: str | None = None,
    ) -> list[dict[str, Any]]:
        """List active and pending tournament rounds.

        Args:
            entry_mode: filter by "BOT_ONLY" | "HUMAN_ONLY" | "MIXED".
            visibility: filter by "PUBLIC" | "UNLISTED" | "PRIVATE".

        Returns the raw round-summary list. Each item has at minimum:
        `id`, `roundNumber`, `status`, `entryMode`, `visibility`, `title`,
        `startTime`, `registrationOpensAt`, `registrationEndsAt`,
        `totalBlocks`, `prizePoolJson`.
        """
        params: dict[str, Any] = {}
        if entry_mode is not None:
            params["entryMode"] = entry_mode
        if visibility is not None:
            params["visibility"] = visibility
        return self._request(  # type: ignore[no-any-return]
            "GET", "/api/trainer/tournament", params=params or None
        )

    def tournament_state(self, tournament_id: str) -> dict[str, Any]:
        """Fetch the full state of a tournament round.

        Alias for `full_state(tournament_id=...)`.
        """
        return self.full_state(tournament_id=tournament_id)

    def register_for_tournament(
        self,
        tournament_id: str,
        *,
        bot_name: str | None = None,
        bot_author_url: str | None = None,
        invite_code: str | None = None,
    ) -> dict[str, Any]:
        """Register the authenticated user for a tournament round.

        For BOT_ONLY tournaments, the API key must belong to a user with an
        active Sim API subscription (so the server records the entry as a bot).

        For PRIVATE tournaments, you need either an explicit invite from the
        round's creator or the inviteCode link parameter; pass it as
        `invite_code`.

        Atomically deducts entryFeeGems if the round has an entry fee.
        """
        body: dict[str, Any] = {}
        if bot_name is not None:
            body["botName"] = bot_name
        if bot_author_url is not None:
            body["botAuthorUrl"] = bot_author_url
        if invite_code is not None:
            body["inviteCode"] = invite_code
        return self._request(  # type: ignore[no-any-return]
            "POST", _path_for("register", tournament_id), json=body
        )

    def withdraw_from_tournament(self, tournament_id: str) -> dict[str, Any]:
        """Withdraw your registration. Allowed only while the round is PENDING.

        Atomically refunds the entry fee you paid back to your gem balance
        and removes the entry. You can re-register later if the round is
        still PENDING.
        """
        return self._request(  # type: ignore[no-any-return]
            "DELETE", _path_for("register", tournament_id)
        )

    def create_tournament(
        self,
        *,
        title: str,
        entry_mode: str,
        creation_fee_gems: int,
        visibility: str = "PUBLIC",
        description: str | None = None,
        total_blocks: int = 120000,
        block_length: int = 15,
        config_preset: int = 0,
        starting_cash: float | None = None,
        contribution_goal: float | None = None,
        total_events: int = 25,
        disable_card_boosts: bool = False,
        disable_dark_web: bool = False,
        disable_fork: bool = False,
        registration_opens_at: str | None = None,
        registration_ends_at: str | None = None,
        min_participants: int = 15,
        max_participants: int | None = None,
        entry_fee_gems: int = 0,
        creation_fee_gold: int = 0,
        prize_distribution_json: str | None = None,
        platform_fee_bps: int = 0,
        invite_code: str | None = None,
    ) -> dict[str, Any]:
        """Create a new tournament round programmatically.

        Atomically deducts ``creation_fee_gems`` from your gem balance
        (and ``creation_fee_gold`` from your in-game Gold balance, if set)
        and seeds them into the prize pool. Both layers are distributed to
        winners by the same per-rank rules in ``prize_distribution_json``.

        Subject to per-user cooldowns: max 1 active/pending tournament per
        user, max 3 creations per 168h window. Creator must have finished
        at least 3 solo (OPEN sim) rounds before they can spawn a tournament.

        Args:
            title: Tournament name (1-120 chars).
            entry_mode: "BOT_ONLY" | "HUMAN_ONLY" | "MIXED".
            creation_fee_gems: ≥ 250. Burned + seeds the gem prize pool.
            visibility: "PUBLIC" | "UNLISTED" | "PRIVATE".
            description: Optional description (≤ 1000 chars).
            total_blocks: Round length in blocks (1000-1,000,000).
            block_length: Seconds per block (5-300).
            config_preset: Investment coefficient preset (0-4).
            starting_cash: Per-player starting cash. Omit for default.
            contribution_goal: Cash required to end the round early. Omit for default.
            total_events: Number of newsfeed events scheduled (0-100).
            disable_card_boosts: Flatten card multipliers to 1.0 for fairness.
            disable_dark_web: Disable PvP powers entirely.
            disable_fork: Disable forking.
            registration_opens_at: ISO 8601. When registration opens. Omit for "now".
            registration_ends_at: ISO 8601. When registration closes. Omit = startTime.
            min_participants: Min entrants needed at startTime, else cancellation+refund.
                Server clamps to [15, 10000]; values below 15 are silently raised.
            max_participants: Hard cap on entrants. Omit for unlimited.
            entry_fee_gems: Per-player gem cost to register. 0 = free entry.
            creation_fee_gold: Optional in-game Gold seed paid on top of gems.
                Distributed to winners by the same per-rank shape. 0 = no gold layer.
            prize_distribution_json: Per-rank distribution as JSON string,
                e.g. '{"1": 0.5, "2": 0.3, "3": 0.2}' (percentages summing to ≤ 1.0)
                or '{"1": 1000, "2": 500}' (absolute amounts). Percentage mode
                requires every value < 1.0 strictly; '{"1": 1}' is read as
                "1 absolute unit to rank 1", not "100%". Single-rank weight
                is capped at 80% of the pool in percentage mode.
            platform_fee_bps: Basis points skimmed from prize pool (≤ 5000 = 50%).
            invite_code: Required for UNLISTED/PRIVATE share-link access.

        Returns the created round.
        """
        # Mirrors the server's minimum creation fee of 250 gems.
        if creation_fee_gems < 250:
            raise ValueError("creation_fee_gems must be at least 250")
        if creation_fee_gold < 0:
            raise ValueError("creation_fee_gold must be ≥ 0")
        if entry_mode not in {"BOT_ONLY", "HUMAN_ONLY", "MIXED"}:
            raise ValueError("entry_mode must be BOT_ONLY, HUMAN_ONLY, or MIXED")
        if visibility not in {"PUBLIC", "UNLISTED", "PRIVATE"}:
            raise ValueError("visibility must be PUBLIC, UNLISTED, or PRIVATE")
        if platform_fee_bps < 0 or platform_fee_bps > 5000:
            raise ValueError("platform_fee_bps must be 0..5000")

        body: dict[str, Any] = {
            "title": title,
            "entryMode": entry_mode,
            "visibility": visibility,
            "creationFeeGems": creation_fee_gems,
            "creationFeeGold": creation_fee_gold,
            "totalBlocks": total_blocks,
            "blockLength": block_length,
            "configPreset": config_preset,
            "totalEvents": total_events,
            "disableCardBoosts": disable_card_boosts,
            "disableDarkWeb": disable_dark_web,
            "disableFork": disable_fork,
            "minParticipants": min_participants,
            "entryFeeGems": entry_fee_gems,
            "platformFeeBps": platform_fee_bps,
        }
        if description is not None:
            body["description"] = description
        if starting_cash is not None:
            body["startingCash"] = starting_cash
        if contribution_goal is not None:
            body["contributionGoal"] = contribution_goal
        if registration_opens_at is not None:
            body["registrationOpensAt"] = registration_opens_at
        if registration_ends_at is not None:
            body["registrationEndsAt"] = registration_ends_at
        if max_participants is not None:
            body["maxParticipants"] = max_participants
        if prize_distribution_json is not None:
            body["prizeDistributionJson"] = prize_distribution_json
        if invite_code is not None:
            body["inviteCode"] = invite_code

        return self._request("POST", "/api/trainer/tournament", json=body)  # type: ignore[no-any-return]

    def my_tournaments(self) -> list[dict[str, Any]]:
        """List tournaments you've created (creator dashboard view)."""
        return self._request("GET", "/api/trainer/tournament/mine")  # type: ignore[no-any-return]

    def list_tournament_invites(self, tournament_id: str) -> list[dict[str, Any]]:
        """List invitees for a PRIVATE tournament you created."""
        return self._request(  # type: ignore[no-any-return]
            "GET", _path_for("invites", tournament_id)
        )

    def invite_to_tournament(
        self, tournament_id: str, *, invited_user_id: str
    ) -> dict[str, Any]:
        """Issue a PRIVATE-tournament invite. Creator only."""
        return self._request(  # type: ignore[no-any-return]
            "POST",
            _path_for("invites", tournament_id),
            json={"invitedUserId": invited_user_id},
        )

    def revoke_tournament_invite(
        self, tournament_id: str, *, invited_user_id: str
    ) -> dict[str, Any]:
        """Revoke a PRIVATE-tournament invite. Creator only."""
        return self._request(  # type: ignore[no-any-return]
            "DELETE",
            f"/api/trainer/tournament/{tournament_id}/invites/{invited_user_id}",
        )

    # ─── Streaming ────────────────────────────────────────────────────────

    @contextmanager
    def stream(self, *, tournament_id: str | None = None) -> Iterator[Iterator[SimEvent]]:
        """Open the SSE event stream and yield SimEvent objects.

        Subscription-only. Sim-only: there is no tournament event stream
        yet, so passing `tournament_id` raises `ValueError`.

        Use as a context manager so the connection closes cleanly:

            with client.stream() as events:
                for event in events:
                    if event.type == "block:advance":
                        print("New block:", event.data["block"])

        Events: connected, ping, block:advance, event:start, event:end,
        round:end, power:received.
        """
        _sim_only("stream", tournament_id)
        with self._http.stream(
            "GET",
            "/api/sim/stream",
            timeout=STREAM_TIMEOUT,
            headers={"Accept": "text/event-stream"},
        ) as response:
            # Translate non-200 into the same exception types as regular calls
            if response.status_code != 200:
                # Read the body for the error message — stream may not have JSON
                body_bytes = response.read()
                try:
                    import json as _json

                    body = _json.loads(body_bytes)
                    message = str(body.get("error", "Stream failed"))
                except Exception:
                    body = body_bytes.decode("utf-8", errors="replace")
                    message = "Stream failed"

                if response.status_code == 401:
                    raise AuthError(401, message, body)
                if response.status_code == 403:
                    raise SubscriptionRequiredError(403, message, body)
                if response.status_code == 429:
                    raise_for_rate_limit(response)
                raise APIError(response.status_code, message, body)

            yield stream_events(response)
