"""Greedy heuristic agent — a reasonable single-page Sim strategy.

Strategy:
  * Each tick, look at every affordable+unlocked investment and buy the one
    with the best earnings/cost ratio. Buy in batches as cash allows.
    It never unlocks a new tier, so it stays on the investments it starts
    with; tier_climber.py shows how to unlock.
  * Stake any cash above the cash buffer once the staking lockout clears.
  * Fork when projected bonus exceeds 5% (or 2% past block ~80% — last-stretch
    forks compound less, so we relax the threshold).
  * Contribute 1% of net worth to the round goal once a minute (cheap goodwill,
    plus the ticket reward for finishing rounds is meaningful long-term).
  * Defend with IMMUNITY when affordable to mitigate blackout attacks.
  * On first fork: pick PRODUCTION (most stat-boost flexibility). Bonus draft:
    pick the first available — a smarter bot would weight by current state.

A deliberately simple starting point that leaves plenty of room for your own
improvements.

Usage:
    SIM_API_KEY=sim_... python examples/greedy_agent.py
"""

from __future__ import annotations

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

# Tunables
TICK_SECONDS = 15
# Share of cash kept liquid. It has to be a fraction: a round starts with 0.000001
# cash and the first unit costs 0.00000005, while a late round runs into millions,
# so any fixed amount stops the bot at one end of the economy. A fixed 1,000 meant
# it never bought its first unit and sat on its starting cash all round.
CASH_BUFFER_FRACTION = 0.10
FORK_THRESHOLD_PERCENT = 5.0  # default fork trigger
FORK_LATE_THRESHOLD_PERCENT = 2.0  # relaxed in late round
LATE_ROUND_PROGRESS = 0.80  # ≥80% block progress is "late"
CONTRIB_INTERVAL_SECONDS = 60.0
CONTRIB_FRACTION = 0.01  # 1% of net worth per minute — cheap goodwill
IMMUNITY_RESERVE_FRACTION = 0.30  # don't blow >30% of keys on defense
DEFAULT_FORK_PATH = Path.PRODUCTION
NO_ROUND_SLEEP = 300

log = logging.getLogger("greedy_agent")


@dataclass
class State:
    """In-process state — survives ticks, not bot restarts."""

    last_contribution_at: float = 0.0
    forks_attempted: int = 0


# ─── Helpers ──────────────────────────────────────────────────────────────


def best_affordable(state: PerceptionState) -> Investment | None:
    """Pick the affordable+unlocked investment with the best earnings/cost ratio."""
    candidates = [i for i in state.investments if i.unlocked and i.can_afford]
    if not candidates:
        return None
    # `earnings_per_unit` already bakes in current event multipliers; same for
    # `next_cost`. We rank by post-event ratio to chase the optimal short-term
    # buy under whatever event is live.
    return max(candidates, key=lambda i: i.earnings_per_unit / max(i.next_cost, 1.0))


def should_fork(state: PerceptionState) -> bool:
    if state.fork is None or state.round is None or state.player is None:
        return False
    if not state.fork.can_fork:
        return False

    progress = state.round.current_block / max(state.round.total_blocks, 1)
    threshold = (
        FORK_LATE_THRESHOLD_PERCENT
        if progress >= LATE_ROUND_PROGRESS
        else FORK_THRESHOLD_PERCENT
    )
    return state.fork.projected_bonus_percent >= threshold


def maybe_buy_immunity(client: Client, state: PerceptionState) -> str | None:
    """Defensive: buy IMMUNITY if affordable, blacked-out, and have spare keys."""
    if state.player is None:
        return None
    if state.player.has_immunity:
        return None
    if not state.player.is_blacked_out:
        return None  # only buy reactively for the starter kit

    immunity = next(
        (p for p in state.powers if p.id == DarkWebPower.IMMUNITY),
        None,
    )
    if immunity is None or not immunity.can_afford or immunity.cost is None:
        return None

    # Don't blow more than IMMUNITY_RESERVE_FRACTION of total tickets on defense
    if immunity.cost > state.player.tickets * IMMUNITY_RESERVE_FRACTION:
        return None

    client.use_power(DarkWebPower.IMMUNITY)
    return f"bought IMMUNITY ({immunity.cost} keys)"


def maybe_contribute(client: Client, state: PerceptionState, mem: State) -> str | None:
    """Contribute 1% of net worth to the round goal at most once per minute."""
    if state.player is None or state.round is None:
        return None
    if (time.time() - mem.last_contribution_at) < CONTRIB_INTERVAL_SECONDS:
        return None
    amount = state.player.cash * CONTRIB_FRACTION
    if amount < 1.0:
        return None
    if amount > state.player.cash:
        return None

    client.contribute(amount=amount)
    mem.last_contribution_at = time.time()
    return f"contributed ${amount:,.2f}"


def maybe_fork(client: Client, state: PerceptionState, mem: State) -> str | None:
    if not should_fork(state):
        return None

    # One call does the whole fork: it fetches the bonus draft, takes the first
    # option and sends the path again with it, which the first fork requires.
    # Pass `choose=` to pick a bonus by your own rule.
    result = client.complete_fork(path=DEFAULT_FORK_PATH)
    if result.get("success"):
        mem.forks_attempted += 1
        return f"forked ({DEFAULT_FORK_PATH.value})"
    return f"fork→ {result.get('message', 'pending')}"


# ─── Tick ─────────────────────────────────────────────────────────────────


def tick(client: Client, mem: State) -> None:
    state = client.perception()

    if not state.active or state.player is None or state.round is None:
        log.info("No active round. Sleeping %ds", NO_ROUND_SLEEP)
        time.sleep(NO_ROUND_SLEEP)
        return

    actions: list[str] = []

    # 1. Defense first — being blacked out is bad
    defense = maybe_buy_immunity(client, state)
    if defense:
        actions.append(defense)

    # 2. Fork analysis (resets investments, so do this BEFORE buying)
    fork_action = maybe_fork(client, state, mem)
    if fork_action:
        actions.append(fork_action)
        # After forking, refresh state — investments are reset, cash starts over
        state = client.perception()
        if not state.active or state.player is None:
            return

    # 3. Buy investments greedily until we hit the cash buffer
    bought_count = 0
    while True:
        target = best_affordable(state)
        buffer = state.player.cash * CASH_BUFFER_FRACTION
        if target is None or target.next_cost > (state.player.cash - buffer):
            break
        client.purchase(investment_id=target.id, quantity=1)
        bought_count += 1
        # We could loop client.perception() here for the latest cash, but
        # that's expensive — assume the cost is roughly constant within a tick
        # and break if we've made many buys.
        state = client.perception()
        if not state.active or state.player is None:
            return
        if bought_count >= 5:
            break  # avoid burning all action rate-limit budget on one tick
    if bought_count:
        actions.append(f"bought {bought_count} investments")

    # 4. Stake any remaining cash above the buffer (if lockout clear)
    amount = state.player.cash * (1 - CASH_BUFFER_FRACTION)
    if state.staking and state.staking.can_stake and amount > 0:
        client.stake(amount=amount)
        actions.append(f"staked ${amount:.6g}")

    # 5. Contribute to round goal — small but adds up
    contrib = maybe_contribute(client, state, mem)
    if contrib:
        actions.append(contrib)

    # 6. Log the tick
    log.info(
        "block %d/%d  nw=$%s  cash=$%.0f  rank=%d/%d  forks=%d  → %s",
        state.round.current_block,
        state.round.total_blocks,
        _short(state.player.net_worth),
        state.player.cash,
        state.player.rank,
        state.player.total_players,
        state.player.fork_number,
        ", ".join(actions) if actions else "no-op",
    )


# ─── Helpers (display) ────────────────────────────────────────────────────


def _short(value: float) -> str:
    """Compact net-worth display: 1234567 → '1.23M'."""
    for suffix, scale in (("Q", 1e15), ("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= scale:
            return f"{value / scale:.2f}{suffix}"
    return f"{value:.0f}"


# ─── Main ─────────────────────────────────────────────────────────────────


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    log.info("Starting greedy_agent. Ctrl-C to stop.")

    mem = State()
    with Client() as client:
        while True:
            try:
                tick(client, mem)
            except NoActiveRoundError:
                log.info("Round ended. Sleeping %ds", NO_ROUND_SLEEP)
                time.sleep(NO_ROUND_SLEEP)
                continue
            except RateLimitError as exc:
                wait = exc.retry_after_seconds or 30
                log.warning("Rate limited. Sleeping %.0fs", wait)
                time.sleep(wait)
                continue
            except SimBotError as exc:
                log.warning("API error: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected error: %s", exc)

            time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nbye.")
