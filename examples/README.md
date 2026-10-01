# Example Bots

Eight scripts: six bots and two utilities. Read them top to bottom; none is longer than about 380 lines.

| File | Purpose |
| ---- | ------- |
| [`random_agent.py`](random_agent.py) | Smoke-test bot. Picks a random valid action each tick. |
| [`greedy_agent.py`](greedy_agent.py) | Heuristic bot. Best earnings/cost ratio + stake + an example fork rule. |
| [`tier_climber.py`](tier_climber.py) | Bot that prioritises unlocking higher tiers, no forking. |
| [`q_learning_agent.py`](q_learning_agent.py) | Tabular Q-learning reference. Illustrative, not competitive. |
| [`tournament_agent.py`](tournament_agent.py) | Finds an open bot tournament, registers and plays it greedily. |
| [`rules_agent.py`](rules_agent.py) | Runs your AI Companion's rules every block. |
| [`verify_endpoints.py`](verify_endpoints.py) | Smoke test that exercises every endpoint once. |
| [`stream_smoke.py`](stream_smoke.py) | Prints SSE events for ~70 seconds and exits. |

---

## `random_agent.py`

Picks a random valid action each tick. **Will not climb the leaderboard.** Use as a smoke test that your API key works and the loop is wired up correctly.

```bash
SIM_API_KEY=sim_... python examples/random_agent.py
```

## `greedy_agent.py`

A single-page heuristic strategy:

- Buys the affordable investment with the highest **earnings ÷ cost** ratio, as many units as fit in one purchase (`MAX_UNIT_PRICE` caps what it pays per unit)
- Stakes spare cash above a buffer
- Forks when the new bonus would be at least 1000x its current earnings multiplier, never in the last 10% of the round (`FORK_MIN_GAIN`, `FORK_MIN_ROUND_LEFT`)
- Contributes 1% of net worth to the round goal once per minute
- Defends with the IMMUNITY power if attacked

It never unlocks a new tier, so it stays on the investments a round starts with; `tier_climber.py` shows how to unlock. A good starting point: fork it, beat it.

```bash
SIM_API_KEY=sim_... python examples/greedy_agent.py
```

## `tier_climber.py`

Pure economic climb. Skips forking entirely and focuses on unlocking the next-better investment tier:

- Buys the best ratio investment, but **saves cash** when a better-ratio locked tier is within reach
- Unlocks higher tiers when their post-unlock ratio beats the current best by ≥1.5×
- Stakes any surplus, contributes 1% of cash per minute, defends with IMMUNITY

Useful for understanding the unlock + buy + stake + contribute surface in isolation. Add forking on top once each piece is clear.

```bash
SIM_API_KEY=sim_... python examples/tier_climber.py
```

## `q_learning_agent.py`

Tabular Q-learning over a discretized state space. Demonstrates the API loop and the basic RL pattern. Depends on `numpy`:

```bash
pip install -e ".[rl]"
SIM_API_KEY=sim_... python examples/q_learning_agent.py
```

The Q-table is persisted to `qtable.npy` (with metadata in `qtable_meta.json`) every 10 ticks and reloaded on restart. Delete those files to reset learning.

**Caveats:**
- State buckets are deliberately coarse (~720 states). Fine-grained spaces need value approximation, not tabular Q.
- Reward signal is net-worth delta + scaled fork-bonus delta. Tweak it.
- Illustrative, not competitive - your job is to build a competitive bot.

## `tournament_agent.py`

The same greedy strategy, scoped to one tournament round. On start it lists the open tournaments, picks the first public `BOT_ONLY` or `MIXED` one (or the round you pass), registers and plays it, honouring the round's `disableFork` and `disableDarkWeb` flags. Every call carries `tournament_id`, so it doubles as a reference for the tournament surface.

```bash
SIM_API_KEY=sim_... python examples/tournament_agent.py [--round-id rnd_xyz]
```

## `rules_agent.py`

Runs AI Companion rules through the API, every block instead of every five minutes. With no arguments it fetches your Companion's rules with your API key: the committed set the Companion runs this round, or your draft if nothing is committed yet, with each rule's on/off switch taken from the draft. `--rules file.json` loads rules in the same format instead ([`rules.example.json`](rules.example.json) shows every field). Without either it uses a small built-in starter set.

It evaluates rules the way the Companion does: in order, disabled rules skipped, all conditions must hold, and at most one action per tick. If a rule matches but cannot act (the unlock is unaffordable, or not even one unit fits the budget or price cap), the next rule gets its turn.

Pause your Companion in the browser while this runs: both spend the same cash.

```bash
SIM_API_KEY=sim_... python examples/rules_agent.py
SIM_API_KEY=sim_... python examples/rules_agent.py --rules examples/rules.example.json
```

## `verify_endpoints.py`

End-to-end coverage test. Calls every read endpoint, performs small real writes (purchase, stake, unstake, contribute, autostake toggle) with a tiny fraction of the bot's cash, inspects fork without committing, and opens the SSE stream long enough to see one event. The writes are not undone and the stake starts a staking lockout, so run it on an account you can spare for a test. Prints a pass/skip/fail matrix and exits non-zero on any unexpected failure. Run after schema or route changes:

```bash
SIM_API_KEY=sim_... python examples/verify_endpoints.py
```

## `stream_smoke.py`

Minimal SSE smoke test. Connects to `/api/sim/stream`, prints up to 50 events or runs ~70 seconds, then exits. Useful for verifying realtime delivery.

```bash
SIM_API_KEY=sim_... python examples/stream_smoke.py
```

---

## Running headless / in production

All long-running bots run forever by default. Stop with Ctrl-C. To run as a service:

```bash
docker build -t simbot .
docker run -d --restart unless-stopped \
  -e SIM_API_KEY="sim_..." \
  --name greedy-bot \
  simbot python examples/greedy_agent.py
```
