"""Tournament-mode reference agent.

Demonstrates the tournament surface - same client, every call takes a
`tournament_id` to target a specific tournament round instead of the OPEN
sim.

Strategy (intentionally simple - like greedy_agent.py but tournament-scoped):
  * On startup: list active tournaments, pick the first BOT_ONLY tournament
    we're eligible for, register, then enter the tick loop.
  * Each tick: best earnings/cost ratio investment, stake spare cash,
    fork when it multiplies earnings enough (see greedy_agent.py), contribute
    1% of cash per minute.
  * Honors the round's feature flags - if disableFork is set, skip forking;
    if disableDarkWeb is set, skip immunity.

Roughly tracks the greedy_agent's behaviour, scoped to one tournament.

Usage:
    SIM_API_KEY=sim_... python examples/tournament_agent.py [--round-id rnd_xyz]

If --round-id is omitted, picks the first PENDING/ACTIVE BOT_ONLY tournament
you're eligible for.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass

from simbot import (
    Client,
    DarkWebPower,
    Investment,
    NoActiveRoundError,
    Path,
    PerceptionState,
    RateLimitError,
    SimBotError,
)

TICK_SECONDS = 15
# Share of cash kept liquid. A fraction, not an amount: a round starts with
# 0.000001 cash, so a fixed 1,000 meant the agent never bought anything.
CASH_BUFFER_FRACTION = 0.10
# Same example fork rule as greedy_agent.py (read the comment there). A
# starting point to tune, not a winning setting. The end guard is a share of
# the round because tournaments can be far shorter than the open round.
FORK_MIN_GAIN = 1000.0
FORK_MIN_ROUND_LEFT = 0.10
CONTRIB_INTERVAL_SECONDS = 60.0
CONTRIB_FRACTION = 0.01
IMMUNITY_RESERVE_FRACTION = 0.30
DEFAULT_FORK_PATH = Path.PRODUCTION
NO_ROUND_SLEEP = 60

log = logging.getLogger("tournament_agent")


@dataclass
class State:
    last_contribution_at: float = 0.0


def select_tournament(client: Client) -> str | None:
    """Find a BOT_ONLY or MIXED tournament we can register for."""
    tournaments = client.list_tournaments()
    # Prefer ACTIVE > PENDING (already running > about to start)
    for status_priority in ("ACTIVE", "PENDING"):
        for t in tournaments:
            if t.get("status") != status_priority:
                continue
            if t.get("entryMode") not in ("BOT_ONLY", "MIXED"):
                continue
            if t.get("visibility") not in ("PUBLIC", None):
                # Skip UNLISTED/PRIVATE - would need an invite_code
                continue
            log.info(
                "Selecting tournament %s (round %s, entryMode=%s, status=%s)",
                t.get("id"),
                t.get("roundNumber"),
                t.get("entryMode"),
                t.get("status"),
            )
            return str(t["id"])
    return None


def ensure_registered(client: Client, tournament_id: str) -> None:
    """Idempotent registration. The trainer service returns the existing entry
    if already registered, so this is safe to call on every restart."""
    try:
        client.register_for_tournament(
            tournament_id, bot_name="tournament_agent_demo"
        )
        log.info("Registered for tournament %s", tournament_id)
    except SimBotError as e:
        # Already registered → acceptable; tournament closed → fatal
        msg = str(e).lower()
        if "already registered" in msg or "already" in msg:
            log.info("Already registered for %s", tournament_id)
            return
        raise


def best_investment(state: PerceptionState, max_spend: float) -> Investment | None:
    """Pick the best earnings-per-cost investment costing at most `max_spend`."""
    candidates = [
        i
        for i in state.investments
        if i.unlocked
        and i.can_afford
        and i.next_cost is not None
        and 0 < i.next_cost <= max_spend
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda i: i.payout_per_unit / i.next_cost)


def maybe_buy(client: Client, state: PerceptionState, tournament_id: str) -> bool:
    """Buy as many units of the best investment as fit into ONE purchase.
    Returns True if cash was spent."""
    if not state.player or state.player.is_blacked_out:
        return False
    budget = state.player.cash * (1 - CASH_BUFFER_FRACTION)
    target = best_investment(state, budget)
    if target is None:
        return False
    units = target.units_affordable(budget)
    if units == 0:
        return False
    try:
        client.purchase(investment_id=target.id, quantity=units, tournament_id=tournament_id)
        return True
    except SimBotError as e:
        log.warning("purchase failed: %s", e)
        return False


def maybe_stake(client: Client, state: PerceptionState, tournament_id: str) -> None:
    if not state.staking or not state.staking.can_stake or not state.player:
        return
    surplus = state.player.cash * (1 - CASH_BUFFER_FRACTION)
    if surplus <= 0:
        return
    try:
        client.stake(amount=surplus, tournament_id=tournament_id)
    except SimBotError as e:
        log.warning("stake failed: %s", e)


def maybe_fork(
    client: Client,
    state: PerceptionState,
    tournament_id: str,
    round_disable_fork: bool,
) -> bool:
    """Fork when the projected bonus is worth it. Returns True if it forked."""
    if round_disable_fork:
        return False
    if not state.fork or not state.fork.can_fork:
        return False
    if state.player is None or state.round is None:
        return False
    left = state.round.blocks_remaining / max(state.round.total_blocks, 1)
    if left < FORK_MIN_ROUND_LEFT:
        return False
    if state.fork.projected_bonus / (1 + state.player.fork_bonus) < FORK_MIN_GAIN:
        return False
    try:
        # One call does the whole fork: it fetches the draft, takes the first
        # bonus and sends the path again with it. The first fork needs that:
        # a request with only the path does not store it, and an earlier
        # version of this agent sent the path alone and asked for the draft
        # every tick, for ever. Pass `choose=` to pick a bonus by your own rule.
        result = client.complete_fork(path=DEFAULT_FORK_PATH, tournament_id=tournament_id)
        return bool(result.get("success"))
    except SimBotError as e:
        log.warning("fork failed: %s", e)
        return False


def maybe_immunity(
    client: Client, state: PerceptionState, tournament_id: str, round_disable_dark_web: bool
) -> None:
    if round_disable_dark_web:
        return
    if not state.player or not state.powers:
        return
    if state.player.has_immunity:
        return
    keys = state.player.tickets
    if keys < 1:
        return
    # Reserve 30% of keys; only spend when we have spare AND we're being attacked
    if not state.player.is_blacked_out or keys * IMMUNITY_RESERVE_FRACTION < 1:
        return
    try:
        client.use_power(DarkWebPower.IMMUNITY, tournament_id=tournament_id)
    except SimBotError as e:
        log.warning("immunity failed: %s", e)


def maybe_contribute(
    client: Client, state: PerceptionState, tournament_id: str, agent_state: State
) -> None:
    now = time.time()
    if now - agent_state.last_contribution_at < CONTRIB_INTERVAL_SECONDS:
        return
    if not state.player:
        return
    amount = state.player.cash * CONTRIB_FRACTION
    if amount <= 0 or amount > state.player.cash:
        return
    try:
        client.contribute(amount=amount, tournament_id=tournament_id)
        agent_state.last_contribution_at = now
    except SimBotError as e:
        log.warning("contribute failed: %s", e)


def fmt_money(v: float) -> str:
    """Format money so both micro-balances (early round) and mega-balances
    (late round) stay legible: 1.23K, 4.56M, 0.000137, 3.20e-07. Rounding to
    whole units printed 'cash=$0' for a bot holding 0.0000031, which reads as
    a broken bot."""
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e3:
        return f"{v / 1e3:.2f}K"
    if a >= 1:
        return f"{v:.2f}"
    if a >= 1e-4:
        return f"{v:.6f}"
    return f"{v:.2e}"


def run(client: Client, tournament_id: str) -> None:
    agent_state = State()

    # Read the round's feature flags once at start. The flags don't change
    # mid-round so caching is safe; if you want defense-in-depth, re-read
    # each tick (cheap - same /perception call).
    initial_state = client.tournament_state(tournament_id)
    round_meta = initial_state.get("round", {}) if isinstance(initial_state, dict) else {}
    disable_fork = bool(round_meta.get("disableFork"))
    disable_dark_web = bool(round_meta.get("disableDarkWeb"))
    disable_card_boosts = bool(round_meta.get("disableCardBoosts"))
    log.info(
        "Round flags: disableFork=%s, disableDarkWeb=%s, disableCardBoosts=%s",
        disable_fork,
        disable_dark_web,
        disable_card_boosts,
    )

    while True:
        try:
            state = client.perception(tournament_id=tournament_id)
            if not state.active:
                log.info("Round inactive, sleeping %ds", NO_ROUND_SLEEP)
                time.sleep(NO_ROUND_SLEEP)
                continue
            if not state.player:
                log.info("No player state yet, sleeping a tick")
                time.sleep(TICK_SECONDS)
                continue

            log.info(
                "Block %s/%s | cash=%s networth=%s rank=%s/%s tickets=%s",
                state.round.current_block if state.round else "?",
                state.round.total_blocks if state.round else "?",
                fmt_money(state.player.cash),
                fmt_money(state.player.net_worth),
                state.player.rank,
                state.player.total_players,
                state.player.tickets,
            )

            maybe_immunity(client, state, tournament_id, disable_dark_web)
            # Forking and buying both change cash, so re-read the state before
            # the next step spends from it. Staking 90% of the cash read BEFORE
            # a purchase asked the server for money that was already gone.
            if maybe_fork(client, state, tournament_id, disable_fork):
                state = client.perception(tournament_id=tournament_id)
            if maybe_buy(client, state, tournament_id):
                state = client.perception(tournament_id=tournament_id)
            maybe_stake(client, state, tournament_id)
            maybe_contribute(client, state, tournament_id, agent_state)
        except NoActiveRoundError:
            log.info("Tournament not active, sleeping %ds", NO_ROUND_SLEEP)
            time.sleep(NO_ROUND_SLEEP)
            continue
        except RateLimitError as e:
            wait = e.retry_after_seconds or 30
            log.warning("Rate limited, backing off %.1fs", wait)
            time.sleep(wait)
            continue
        except SimBotError as e:
            log.error("Tick failed: %s", e)
        time.sleep(TICK_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tournament-mode reference agent.")
    parser.add_argument(
        "--round-id",
        help="Specific tournament round id. If omitted, auto-selects the first "
        "BOT_ONLY/MIXED tournament you're eligible for.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with Client() as client:
        round_id = args.round_id or select_tournament(client)
        if round_id is None:
            log.error(
                "No eligible tournament found. List with `client.list_tournaments()` "
                "or pass --round-id explicitly."
            )
            return
        ensure_registered(client, round_id)
        run(client, round_id)


if __name__ == "__main__":
    main()
