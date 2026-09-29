"""Tabular Q-learning agent — RL reference for the Sim bot starter kit.

This is illustrative, not competitive. The Sim's true state is high-dimensional
(continuous cash, investment counts, event mix, fork progress, lockout timers,
encryption keys, etc.). Tabular Q-learning needs a small discrete state space,
so we hash everything down to a coarse bucket. Result: it learns to make
broadly-okay decisions but won't beat the greedy agent.

What it demonstrates:
  * The perception → decide → act → reward loop
  * State discretization for a continuous game
  * Q-table updates with the standard Bellman backup
  * Epsilon-greedy exploration
  * Persistence: save the Q-table to disk so learning compounds across runs

What it doesn't do:
  * Function approximation (neural nets) — needed for a real-strategy bot
  * Multi-agent modeling of opponent behavior
  * Long-horizon credit assignment

Replace tabular Q with PPO/SAC and a state encoder that captures investment
mix, fork bonus, event multipliers, and you'd have something competitive.

Usage:
    pip install -e ".[rl]"          # pulls numpy
    SIM_API_KEY=sim_... python examples/q_learning_agent.py

The Q-table is saved to qtable_<round_id>.npy each tick and reloaded on
restart. Delete it to reset learning.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path as FsPath
from typing import NamedTuple

import numpy as np

from simbot import (
    Client,
    Investment,
    NoActiveRoundError,
    PerceptionState,
    RateLimitError,
    SimBotError,
)

# ─── Hyperparameters ──────────────────────────────────────────────────────

TICK_SECONDS = 15
NO_ROUND_SLEEP = 300

# Discretization
N_CASH_BUCKETS = 6          # log-scale cash buckets
N_PROGRESS_BUCKETS = 5      # round progress 0..1
N_EVENT_BUCKETS = 2         # event presence: yes/no
N_FORK_BUCKETS = 3          # 0 / 1-3 / 4+
N_RANK_BUCKETS = 4          # quartile rank

# Action space (8 discrete actions)
ACTION_NOOP = 0
ACTION_BUY_T1 = 1
ACTION_BUY_T2 = 2
ACTION_BUY_T3 = 3
ACTION_STAKE_HALF = 4
ACTION_UNSTAKE_HALF = 5
ACTION_FORK = 6
ACTION_CONTRIBUTE = 7
N_ACTIONS = 8
ACTION_NAMES = [
    "noop", "buy_t1", "buy_t2", "buy_t3",
    "stake/2", "unstake/2", "fork", "contribute",
]

# Q-learning
ALPHA = 0.10              # learning rate
GAMMA = 0.95              # discount
EPSILON_START = 0.30      # initial exploration
EPSILON_END = 0.05        # floor
EPSILON_DECAY_TICKS = 200 # linear decay length

# Reward shaping
REWARD_NETWORTH_GAIN_SCALE = 1e-6  # scales raw net-worth delta to ~unit reward
REWARD_FORK_BONUS_GAIN_SCALE = 100.0  # scales fork-bonus % gains heavily

QTABLE_FILE = FsPath("qtable.npy")
META_FILE = FsPath("qtable_meta.json")

log = logging.getLogger("q_learning_agent")


# ─── State discretization ─────────────────────────────────────────────────


class Bucket(NamedTuple):
    cash: int
    progress: int
    events: int
    fork: int
    rank: int

    def index(self) -> int:
        """Flatten to a single integer index into the Q-table."""
        return (
            self.cash
            * N_PROGRESS_BUCKETS * N_EVENT_BUCKETS * N_FORK_BUCKETS * N_RANK_BUCKETS
            + self.progress * N_EVENT_BUCKETS * N_FORK_BUCKETS * N_RANK_BUCKETS
            + self.events * N_FORK_BUCKETS * N_RANK_BUCKETS
            + self.fork * N_RANK_BUCKETS
            + self.rank
        )


N_STATES = N_CASH_BUCKETS * N_PROGRESS_BUCKETS * N_EVENT_BUCKETS * N_FORK_BUCKETS * N_RANK_BUCKETS


def bucket_state(state: PerceptionState) -> Bucket:
    if state.player is None or state.round is None:
        return Bucket(0, 0, 0, 0, 0)

    p, r = state.player, state.round

    # Log-scale cash bucket
    cash_log = math.log10(max(p.cash, 1.0))
    cash_b = min(N_CASH_BUCKETS - 1, int(cash_log))

    # Round progress
    progress = r.current_block / max(r.total_blocks, 1)
    progress_b = min(N_PROGRESS_BUCKETS - 1, int(progress * N_PROGRESS_BUCKETS))

    # Event presence — coarse but sufficient for tabular
    events_b = 1 if state.events_active else 0

    # Fork count buckets
    if p.fork_number == 0:
        fork_b = 0
    elif p.fork_number < 4:
        fork_b = 1
    else:
        fork_b = 2

    # Rank quartile (1 = top 25%)
    rank_quartile = (p.rank - 1) // max(p.total_players // 4, 1)
    rank_b = min(N_RANK_BUCKETS - 1, max(0, rank_quartile))

    return Bucket(cash_b, progress_b, events_b, fork_b, rank_b)


# ─── Action helpers ───────────────────────────────────────────────────────


def affordable_in_tier(state: PerceptionState, tier: int) -> Investment | None:
    """Cheapest affordable investment in a given tier (1, 2, or 3)."""
    candidates = [
        i for i in state.investments
        if i.unlocked and i.can_afford and i.tier == tier
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda i: i.next_cost)


def execute_action(client: Client, state: PerceptionState, action: int) -> str:
    """Apply the chosen action. Returns a one-line description."""
    if state.player is None:
        return "no state"

    if action == ACTION_NOOP:
        return "noop"

    if action in (ACTION_BUY_T1, ACTION_BUY_T2, ACTION_BUY_T3):
        tier = action - ACTION_BUY_T1 + 1
        target = affordable_in_tier(state, tier)
        if target is None:
            return f"buy_t{tier} unavailable"
        client.purchase(investment_id=target.id, quantity=1)
        return f"bought {target.name}"

    if action == ACTION_STAKE_HALF:
        if not state.staking or not state.staking.can_stake or state.player.cash < 100:
            return "can't stake"
        amount = state.player.cash * 0.5
        client.stake(amount=amount)
        return f"staked ${amount:,.2f}"

    if action == ACTION_UNSTAKE_HALF:
        if (
            not state.staking
            or not state.staking.can_unstake
            or state.player.staked_value < 100
        ):
            return "can't unstake"
        amount = state.player.staked_value * 0.5
        client.unstake(amount=amount)
        return f"unstaked ${amount:,.2f}"

    if action == ACTION_FORK:
        if state.fork is None or not state.fork.can_fork:
            return "can't fork"
        # Tabular bot uses the simplest fork policy: PRODUCTION + first bonus
        result = client.complete_fork(path="PRODUCTION")
        return "forked" if result.get("success") else "fork pending"

    if action == ACTION_CONTRIBUTE:
        amount = max(1.0, state.player.cash * 0.01)
        if amount > state.player.cash:
            return "can't contribute"
        client.contribute(amount=amount)
        return f"contributed ${amount:.2f}"

    return f"unknown action {action}"


def reward_signal(prev_state: PerceptionState, new_state: PerceptionState) -> float:
    """Reward = scaled net-worth delta + bonus for fork-bonus gain.

    Net worth doesn't capture fork value (forking RESETS net worth), so we
    explicitly reward the post-fork bonus increase too.
    """
    if prev_state.player is None or new_state.player is None:
        return 0.0

    nw_delta = new_state.player.net_worth - prev_state.player.net_worth
    bonus_delta = new_state.player.fork_bonus_percent - prev_state.player.fork_bonus_percent

    # Fork resets net worth, so a fork looks like a huge negative reward unless
    # we credit the bonus gain. Cap the negative tail.
    nw_reward = max(nw_delta * REWARD_NETWORTH_GAIN_SCALE, -1.0)
    bonus_reward = bonus_delta * REWARD_FORK_BONUS_GAIN_SCALE

    return nw_reward + bonus_reward


# ─── Q-table persistence ──────────────────────────────────────────────────


def load_qtable() -> tuple[np.ndarray, int]:
    """Load Q-table and global tick counter. Initializes if missing."""
    if QTABLE_FILE.exists() and META_FILE.exists():
        try:
            q = np.load(QTABLE_FILE)
            meta = json.loads(META_FILE.read_text())
            if q.shape == (N_STATES, N_ACTIONS):
                log.info("Loaded Q-table: %d states, %d ticks of history",
                         N_STATES, meta.get("ticks", 0))
                return q, int(meta.get("ticks", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load Q-table: %s. Starting fresh.", exc)

    return np.zeros((N_STATES, N_ACTIONS), dtype=np.float32), 0


def save_qtable(q: np.ndarray, ticks: int) -> None:
    np.save(QTABLE_FILE, q)
    META_FILE.write_text(json.dumps({"ticks": ticks, "n_states": N_STATES}))


# ─── Main loop ────────────────────────────────────────────────────────────


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    log.info("Starting q_learning_agent. State space: %d states × %d actions",
             N_STATES, N_ACTIONS)

    q, ticks_done = load_qtable()
    rng = np.random.default_rng(42)

    with Client() as client:
        prev_state: PerceptionState | None = None
        prev_bucket_idx: int | None = None
        prev_action: int | None = None

        while True:
            try:
                state = client.perception()

                if not state.active or state.player is None:
                    log.info("No active round. Sleeping %ds", NO_ROUND_SLEEP)
                    time.sleep(NO_ROUND_SLEEP)
                    continue

                bucket = bucket_state(state)
                bidx = bucket.index()

                # Q-update from previous transition
                have_prev = (
                    prev_state is not None
                    and prev_bucket_idx is not None
                    and prev_action is not None
                )
                if have_prev:
                    r = reward_signal(prev_state, state)  # type: ignore[arg-type]
                    best_next = float(np.max(q[bidx]))
                    td_target = r + GAMMA * best_next
                    td_error = td_target - q[prev_bucket_idx, prev_action]
                    q[prev_bucket_idx, prev_action] += ALPHA * td_error

                # Epsilon-greedy action selection (linear decay)
                decay = (EPSILON_START - EPSILON_END) * (ticks_done / EPSILON_DECAY_TICKS)
                eps = max(EPSILON_END, EPSILON_START - decay)
                if rng.random() < eps:
                    action = int(rng.integers(0, N_ACTIONS))
                    why = "explore"
                else:
                    action = int(np.argmax(q[bidx]))
                    why = "exploit"

                # Execute
                action_desc = execute_action(client, state, action)

                log.info(
                    "tick %d  cash=%s  rank=%d  fork=%d  bucket=%s  ε=%.2f  %s→%s  : %s",
                    ticks_done,
                    _short(state.player.cash),
                    state.player.rank,
                    state.player.fork_number,
                    bucket,
                    eps,
                    why,
                    ACTION_NAMES[action],
                    action_desc,
                )

                prev_state = state
                prev_bucket_idx = bidx
                prev_action = action
                ticks_done += 1

                # Persist every 10 ticks
                if ticks_done % 10 == 0:
                    save_qtable(q, ticks_done)

            except NoActiveRoundError:
                save_qtable(q, ticks_done)
                log.info("Round ended. Sleeping %ds", NO_ROUND_SLEEP)
                prev_state = None  # don't bridge across rounds
                prev_bucket_idx = None
                prev_action = None
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


def _short(value: float) -> str:
    for suffix, scale in (("Q", 1e15), ("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= scale:
            return f"{value / scale:.2f}{suffix}"
    return f"{value:.0f}"


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nbye.")
