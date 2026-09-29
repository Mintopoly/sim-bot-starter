"""Tier-climber agent - unlocks higher tiers and buys max affordable.

Strategy (no forks - pure economic climb):

  1. Defense first - buy IMMUNITY if blacked out and affordable.

  2. Unlock decision - for each locked investment we can afford to unlock,
     compute its post-unlock buy ratio (earnings / cost). If it beats the
     best currently-unlocked buy ratio by `UNLOCK_THRESHOLD`, unlock it.
     Threshold > 1 prevents flapping back and forth between near-equal
     options.

  3. Buy decision - find the unlocked investment with the best
     earnings/cost ratio. Buy as many as fit into ONE purchase while
     keeping the cash buffer liquid for opportunistic unlocks, capped at
     `MAX_UNITS_PER_PURCHASE`.

  4. Stake spare cash above 2x buffer if staking lockout is clear.

  5. Contribute 1% to round goal once per minute (cheap goodwill, end-of-
     round ticket reward).

  6. Never forks. Even if `state.fork.can_fork`, we ignore it.

This bot demonstrates the full action surface a user needs to climb the
ladder: unlock + buy + stake + contribute + defensive power. Add forks on
top of this once you understand each piece individually.
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
    PerceptionState,
    RateLimitError,
    SimBotError,
)

# Tunables
TICK_SECONDS = 15
# Fractional buffer scales with the current economy. At fork=0 a player has
# sub-cent balances; at fork=20 they may have millions. A fixed buffer would
# strangle one regime to protect the other.
CASH_BUFFER_FRACTION = 0.10
UNLOCK_THRESHOLD = 1.5  # only unlock if its buy ratio is >=1.5x the current best
MAX_UNITS_PER_PURCHASE = 100  # the manual buy cap; one request per tick either way
CONTRIB_INTERVAL_SECONDS = 60.0
CONTRIB_FRACTION = 0.01
IMMUNITY_RESERVE_FRACTION = 0.30
NO_ROUND_SLEEP = 300

log = logging.getLogger("tier_climber")


@dataclass
class State:
    last_contribution_at: float = 0.0


# ─── Decision helpers ─────────────────────────────────────────────────────


def buy_ratio(inv: Investment) -> float:
    """Earnings per block per cost unit. Higher is better."""
    return inv.earnings_per_unit / max(inv.next_cost, 1e-12)


def best_buy(state: PerceptionState) -> Investment | None:
    candidates = [i for i in state.investments if i.unlocked and i.can_afford]
    return max(candidates, key=buy_ratio) if candidates else None


def best_unlock_candidate(
    state: PerceptionState, current_best_ratio: float
) -> Investment | None:
    """Locked investment we can afford to unlock AND that beats the current
    best buy ratio by UNLOCK_THRESHOLD. Returns None if nothing qualifies.
    """
    candidates = [
        i
        for i in state.investments
        if not i.unlocked
        and i.unlock_cost is not None
        and i.can_afford_unlock
        and buy_ratio(i) > current_best_ratio * UNLOCK_THRESHOLD
    ]
    return max(candidates, key=buy_ratio) if candidates else None


def best_unlock_target(
    state: PerceptionState, current_best_ratio: float
) -> Investment | None:
    """Locked investment worth saving toward - ratio beats threshold regardless
    of whether we can afford it yet. Used to set a cash-preservation floor.
    """
    candidates = [
        i
        for i in state.investments
        if not i.unlocked
        and i.unlock_cost is not None
        and buy_ratio(i) > current_best_ratio * UNLOCK_THRESHOLD
    ]
    return max(candidates, key=buy_ratio) if candidates else None


# ─── Action steps ─────────────────────────────────────────────────────────


def maybe_buy_immunity(client: Client, state: PerceptionState) -> str | None:
    if state.player is None:
        return None
    if state.player.has_immunity or not state.player.is_blacked_out:
        return None
    immunity = next((p for p in state.powers if p.id == DarkWebPower.IMMUNITY), None)
    if immunity is None or not immunity.can_afford or immunity.cost is None:
        return None
    if immunity.cost > state.player.tickets * IMMUNITY_RESERVE_FRACTION:
        return None
    client.use_power(DarkWebPower.IMMUNITY)
    return f"bought IMMUNITY ({immunity.cost} keys)"


def maybe_unlock(client: Client, state: PerceptionState) -> str | None:
    current_best = best_buy(state)
    current_ratio = buy_ratio(current_best) if current_best else 0.0
    target = best_unlock_candidate(state, current_ratio)
    if target is None or target.unlock_cost is None:
        return None
    client.purchase(investment_id=target.id, action="unlock")
    return (
        f"unlocked id={target.id} {target.name} "
        f"for ${target.unlock_cost:.6g} "
        f"(ratio {buy_ratio(target):.4g} vs current {current_ratio:.4g})"
    )


def buy_max_affordable(
    client: Client,
    state: PerceptionState,
    save_target: Investment | None = None,
) -> tuple[int, str | None]:
    """Buy as many units of the best unlocked investment as fit into ONE
    purchase. Returns (units, name).

    units_affordable walks the price curve the way the server charges a
    multi-unit purchase, so one request replaces a buy-and-refresh loop.
    If save_target is set, the cash floor is raised to that investment's
    unlock_cost so we accumulate toward the higher-value tier instead.
    """
    if state.player is None:
        return 0, None
    target = best_buy(state)
    if target is None:
        return 0, None
    buffer = state.player.cash * CASH_BUFFER_FRACTION
    # Raise the floor to the save target's unlock cost so we don't spend
    # down to zero when a better tier is within reach.
    if save_target is not None and save_target.unlock_cost is not None:
        buffer = max(buffer, save_target.unlock_cost)
    units = target.units_affordable(
        state.player.cash - buffer, limit=MAX_UNITS_PER_PURCHASE
    )
    if units == 0:
        return 0, None
    client.purchase(investment_id=target.id, quantity=units)
    return units, target.name


def maybe_stake(client: Client, state: PerceptionState) -> str | None:
    if state.player is None or state.staking is None:
        return None
    if not state.staking.can_stake:
        return None
    buffer = state.player.cash * CASH_BUFFER_FRACTION
    # Need enough surplus over buffer to make staking worthwhile
    if state.player.cash <= buffer * 2:
        return None
    amount = state.player.cash - buffer
    client.stake(amount=amount)
    return f"staked ${amount:.6g}"


def maybe_contribute(
    client: Client, state: PerceptionState, mem: State
) -> str | None:
    if state.player is None:
        return None
    if (time.time() - mem.last_contribution_at) < CONTRIB_INTERVAL_SECONDS:
        return None
    amount = state.player.cash * CONTRIB_FRACTION
    # Skip dust contributions. This is the bot's own choice: the server accepts
    # any amount above zero, at most once per block.
    if amount < 1.0 or amount > state.player.cash:
        return None
    client.contribute(amount=amount)
    mem.last_contribution_at = time.time()
    return f"contributed ${amount:.6g}"


# ─── Tick ─────────────────────────────────────────────────────────────────


def fmt_money(v: float) -> str:
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


def tick(client: Client, mem: State) -> None:
    state = client.perception()
    if not state.active or state.player is None or state.round is None:
        log.info("No active round. Sleeping %ds", NO_ROUND_SLEEP)
        time.sleep(NO_ROUND_SLEEP)
        return

    actions: list[str] = []

    # 1. Defense
    a = maybe_buy_immunity(client, state)
    if a:
        actions.append(a)

    # 2. Unlock if a locked tier dominates the current best
    a = maybe_unlock(client, state)
    if a:
        actions.append(a)
        state = client.perception()
        if not state.active or state.player is None:
            return

    # 3. Buy max affordable of the best unlocked - but save toward a better tier
    current_best = best_buy(state)
    current_ratio = buy_ratio(current_best) if current_best else 0.0
    save_target = best_unlock_target(state, current_ratio)
    if save_target:
        log.debug(
            "saving toward unlock: %s (unlock_cost=%.6g, ratio=%.4g)",
            save_target.name,
            save_target.unlock_cost,
            buy_ratio(save_target),
        )
    bought, last_name = buy_max_affordable(client, state, save_target=save_target)
    if bought:
        actions.append(f"bought {bought}x {last_name}")
        state = client.perception()
        if not state.active or state.player is None:
            return

    # 4. Stake spare cash
    a = maybe_stake(client, state)
    if a:
        actions.append(a)

    # 5. Round-goal contribution
    a = maybe_contribute(client, state, mem)
    if a:
        actions.append(a)

    log.info(
        "block %d/%d  cash=$%s  nw=$%s  rank=%d/%d  fork#%d  -> %s",
        state.round.current_block,
        state.round.total_blocks,
        fmt_money(state.player.cash),
        fmt_money(state.player.net_worth),
        state.player.rank,
        state.player.total_players,
        state.player.fork_number,
        ", ".join(actions) if actions else "no-op",
    )


# ─── Main ─────────────────────────────────────────────────────────────────


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    log.info("Starting tier_climber. Ctrl-C to stop.")

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
