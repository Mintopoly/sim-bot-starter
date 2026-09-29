"""Random agent - baseline for the Sim bot starter kit.

Picks a random valid action each tick. Useful as a "is anything wired up?"
smoke test. Will NOT climb the leaderboard.

Usage:
    SIM_API_KEY=sim_... python examples/random_agent.py

Stop with Ctrl-C.
"""

from __future__ import annotations

import logging
import random
import time

from simbot import (
    Client,
    NoActiveRoundError,
    PerceptionState,
    RateLimitError,
    SimBotError,
)

# Tick once per block (~15s). Shorter is wasted; longer drops responsiveness.
TICK_SECONDS = 15
# Sleep when no round is active.
NO_ROUND_SLEEP = 300

log = logging.getLogger("random_agent")


def fmt_money(v: float) -> str:
    """Format money so both micro-balances (early round) and mega-balances
    (late round, post-fork) are legible: 1.23K, 4.56M, 0.000137, 0.42."""
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


def pick_random_action(client: Client, state: PerceptionState) -> str:
    """Pick and execute one random action. Returns a one-liner describing it."""
    if state.player is None:
        return "no player state"

    options = ["nothing", "nothing"]  # bias toward doing nothing
    if any(i.unlocked and i.can_afford for i in state.investments):
        options.append("buy")
    if state.staking and state.staking.can_stake and state.player.cash > 100:
        options.append("stake")
    if state.staking and state.staking.can_unstake and state.player.staked_value > 100:
        options.append("unstake")
    if state.fork and state.fork.can_fork and not state.fork.needs_path_selection:
        options.append("fork")

    choice = random.choice(options)

    if choice == "buy":
        affordable = [i for i in state.investments if i.unlocked and i.can_afford]
        target = random.choice(affordable)
        client.purchase(investment_id=target.id, quantity=1)
        return f"bought 1x {target.name} (id={target.id}) for ${fmt_money(target.next_cost)}"

    if choice == "stake":
        amount = state.player.cash * random.uniform(0.1, 0.5)
        client.stake(amount=amount)
        return f"staked ${fmt_money(amount)}"

    if choice == "unstake":
        amount = state.player.staked_value * random.uniform(0.1, 0.5)
        client.unstake(amount=amount)
        return f"unstaked ${fmt_money(amount)}"

    if choice == "fork":
        # Try to fork - if path/bonus selection is needed, the API tells us
        # what to pick; a real bot would respond. For randomness we just
        # log the response and carry on.
        result = client.fork()
        if result.get("requiresPathSelection"):
            return "fork blocked: needs path selection (random bot doesn't pick paths)"
        if result.get("requiresBonusSelection"):
            return "fork blocked: needs bonus draft selection"
        return f"forked! new fork#{result.get('fork', {}).get('forkNumber', '?')}"

    return "did nothing"


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    log.info("Starting random_agent. Ctrl-C to stop.")

    with Client() as client:
        while True:
            try:
                state = client.perception()
                if not state.active or state.player is None:
                    log.info("No active round. Sleeping %ds", NO_ROUND_SLEEP)
                    time.sleep(NO_ROUND_SLEEP)
                    continue

                action = pick_random_action(client, state)
                log.info(
                    "block %d  cash=$%s  nw=$%s  rank=%d/%d  -> %s",
                    state.round.current_block if state.round else -1,
                    fmt_money(state.player.cash),
                    fmt_money(state.player.net_worth),
                    state.player.rank,
                    state.player.total_players,
                    action,
                )

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
            except Exception as exc:  # noqa: BLE001 - bots run forever, log everything
                log.exception("Unexpected error: %s", exc)

            time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nbye.")
