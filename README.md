# Children of Titan — Sim Bot Starter Kit

Build a bot that plays [The Sim](https://www.childrenoftitan.com/sim) — a multiplayer block-based idle economy game where humans and bots compete on the same leaderboard.

```python
from simbot import Client

client = Client(api_key="sim_...")

state = client.perception()
print(f"Cash: ${state.player.cash:.2f}  Rank: {state.player.rank}")

# Buy 5 of the cheapest affordable investment
buys = [i for i in state.investments if i.unlocked and i.can_afford]
buys.sort(key=lambda i: i.next_cost)
if buys:
    client.purchase(investment_id=buys[0].id, quantity=5)
```

That's it. You're playing.

---

## What this is

The Sim is a persistent, multi-week, multi-agent economy game. Every block (~15 seconds) your investments earn cash, your stake compounds, events shift the math, and other players (humans and bots) jostle for the leaderboard. Forks reset your run for a permanent earnings multiplier. Encryption keys buy dark-web powers that mess with rivals. The round ends after ~120,000 blocks (~21 days).

Bots play the same game humans do. A subscription ($12.99/mo) gets you an API key, a perception endpoint that returns full game state in one call, and a server-sent-event stream for real-time block/event/power notifications.

This kit gives you:
- A typed Python client (`simbot.Client`) with retries, typed exceptions, and an SSE event stream
- Reference bots: random, greedy, tier-climber, tabular Q-learning
- Endpoint and stream smoke tests
- A Dockerfile so you can run a bot in 60 seconds
- Tests, type hints, and CI

It's MIT-licensed. Fork it, use it, ship a better bot than the example bots.

---

## Quickstart (60 seconds)

```bash
# 1. Install
pip install -e .

# 2. Set your API key (get one at childrenoftitan.com/sim)
export SIM_API_KEY="sim_..."

# 3. Run the greedy bot for a few blocks
python examples/greedy_agent.py
```

Or with Docker:

```bash
docker build -t simbot .
docker run -e SIM_API_KEY="sim_..." simbot python examples/greedy_agent.py
```

---

## Get an API key

1. Sign up at [childrenoftitan.com](https://www.childrenoftitan.com/register)
2. Go to the Sim subscription page and pay $12.99/mo via Stripe
3. Copy the API key (shown once — store it, you can regenerate but the old one stops working)
4. Set the `SIM_API_KEY` environment variable

Full API docs: [childrenoftitan.com/sim/docs](https://www.childrenoftitan.com/sim/docs)

---

## Example bots & utilities

| File | What it does |
| ---- | ------------ |
| [`examples/random_agent.py`](examples/random_agent.py) | Random valid action each tick — sanity check, not competitive. |
| [`examples/greedy_agent.py`](examples/greedy_agent.py) | Best earnings/cost ratio + stake + fork at 5%. Never unlocks a new tier; a simple starting point. |
| [`examples/tier_climber.py`](examples/tier_climber.py) | Saves toward higher-tier unlocks, no forking. Demonstrates the unlock→buy→stake loop in isolation. |
| [`examples/q_learning_agent.py`](examples/q_learning_agent.py) | Tabular Q-learning reference. Illustrative, not competitive. |
| [`examples/tournament_agent.py`](examples/tournament_agent.py) | Finds an open bot tournament, registers and plays it with the greedy strategy. |
| [`examples/verify_endpoints.py`](examples/verify_endpoints.py) | End-to-end smoke test — exercises every endpoint and prints a pass/skip/fail matrix. |
| [`examples/stream_smoke.py`](examples/stream_smoke.py) | Connects to the SSE stream, prints events for ~70 s, exits. |

Read each example top-to-bottom before modifying; none is longer than about 380 lines. `tests/test_examples.py` runs every one of them against an offline copy of the game, so a change that stops a bot from trading fails the test suite. See [`examples/README.md`](examples/README.md) for the longer walk-through.

---

## Client API

```python
from simbot import Client

client = Client(api_key="sim_...")  # or omit and set SIM_API_KEY

# Read state
state = client.perception()           # Bot-optimized full state
state = client.full_state()           # Same data, plain GET /api/sim format
events = client.events()              # Active / upcoming / recent events
paths = client.paths()                # Path system info
leaderboard = client.leaderboard()    # Top 100 ranks
profile = client.profile()            # Your sim profile
history = client.history()            # Your past round results
cards = client.cards()                # Your cards with active boosts
gems = client.gems_balance()          # User-level gem balance + last 20 transactions
keys = client.buy_keys_info()         # Key purchase options + current key/gem balance
                                      #   pass tournament_id=... for round-scoped balance

# Take actions (amounts as fractions: the economy starts in millionths)
me = client.perception().player                           # Your cash and stake right now
client.purchase(investment_id=0, quantity=5)              # Buy 5 of investment 0
client.purchase(investment_id=3, action="unlock")         # Unlock a tier
client.stake(amount=me.cash * 0.5)                        # Stake half your cash
client.unstake(amount=me.staked_value * 0.5)              # Unstake half your stake
client.complete_fork(path="PRODUCTION")                   # Fork: draft, first bonus, commit
client.use_power("blackout", target_user_id="user_abc")   # Hit a rival
client.contribute(amount=me.cash * 0.01)                  # Help round goal
client.set_autostake(True)                                # Auto-stake earnings
client.dismiss()                                          # Dismiss short circuit notification
client.buy_keys(quantity=10)                              # Buy 10 encryption keys (solo)
client.buy_keys(quantity=10, tournament_id="rnd_abc")     # Buy 10 round-scoped keys

# Real-time events (a context manager, so the connection closes cleanly)
with client.stream() as events:
    for event in events:
        if event.type == "block:advance":
            print(f"Block {event.data['block']}")
        elif event.type == "power:received":
            print(f"Got hit by {event.data['power']}")
```

The client handles auth, rate limits (with exponential backoff on 429), and JSON parsing. All responses are typed dataclasses you can `print()` cleanly.

---

## The economy starts in millionths

Every player starts a round with **0.000001 cash**, and the first unit costs 0.00000005. By the end of a round the same numbers run into millions, and every fork puts you back at the start. So never gate a decision on a fixed amount such as `if cash > 1000` or `stake(amount=100)`: it either blocks your bot for the whole early game or never triggers late. Work with fractions of what you hold (`cash * 0.1`) or with the prices the API hands you (`next_cost`, `unlock_cost`).

This is not theoretical. Two of the example bots once waited for 1,000 cash before buying and so never bought anything at all; `tests/test_examples.py` now plays every example against an offline copy of the game and catches exactly that.

---

## Which economy is this round?

Every round draws one of five coefficient presets at random, and it sets all unit prices, how fast they climb and what each unit pays. The same strategy plays very differently across them, so read it before you commit:

```python
state = client.perception()
if state.round.economy:                      # None on a server that predates the field
    print(state.round.economy.id, state.round.economy.name)
```

| `id` | Name | What it means |
| --- | --- | --- |
| 0 | Classic | The original prices and payouts. Opening a new type costs the least here. |
| 1 | Quick Return | Units pay for themselves fastest, but their price climbs the steepest. |
| 2 | Heavy Industry | Units cost far more and earn far more. The slowest start of the five. |
| 3 | Steep Tiers | Units stay affordable, but opening the next type costs the most income. |
| 4 | Deep Field | Costly all round, and every type is worth about the same. Go deep, not wide. |

The exact numbers are already on every `Investment` (`coefficient`, `next_cost`, `earnings_per_unit`); the preset is the name for them. Tournaments send the same field in the same place.

---

## Fork mechanics your bot needs to know

Forking is the prestige system: reset your investments and staking for a permanent earnings multiplier. Each fork also lets you draft one bonus from your chosen path. There are three paths: **Production**, **Staking**, **Dark Web** — pick once on your first fork, locked for the round.

Key rules to encode into your bot:

- **Fork with `complete_fork`**. A fork takes two requests: one returns the bonus draft, the other commits your pick. On the first fork of a round the path has to go out again with the bonus, because a request with only the path returns the draft without storing it. `client.complete_fork(path=..., choose=...)` sends both correctly; `client.fork()` is the single low-level request.
- **Cooldown**: a minimum of 1000 blocks (~4 hours at 15s/block) between consecutive forks. Read `state.fork.cooldown_blocks_remaining` (0 = forkable). `state.fork.can_fork` already accounts for cooldown, max-forks, and positive-net-worth gates.
- **Max 7 forks per round**. With the 1000-block cooldown, a full 7-fork run takes at least ~25 hours of round time.
- **Bonus draft is deterministic**: calling `client.fork()` without a `bonus_id` returns the full eligible bonus list for your path/fork-count (3-5 options). Re-calling returns the same set — no rerolls. Pick the bonus that fits your strategy.
- **Bonus IDs use SCREAMING_SNAKE_CASE** (e.g. `PROD_EARNINGS_1`, `STAKE_RATE_1`, `DARK_KEY_1`). The exact set is in the round's `client.paths()` response.

### Path bonuses (after the round-194 rebalance)

| Path | Effect | Per-pick cap | Path total ceiling |
| --- | --- | --- | --- |
| Production | `PROD_EARNINGS_1` (+5% earnings) | +25% from this alone | +50% earnings, all Production picks combined |
| Production | `PROD_EARNINGS_2` (+10% earnings) | one-shot, requires 2+ forks | counts toward the +50% |
| Production | `PROD_COST_1` (+6% earnings) | +30% from this alone | counts toward the +50% |
| Production | `PROD_COST_2` (+10% earnings) | one-shot, requires 3+ forks | counts toward the +50% |
| Production | `PROD_UNLOCK` (-100% unlock costs) | one-shot | every remaining unlock free |
| Staking | `STAKE_RATE_1` (+5% rate) | +25% from this alone | +35% rate (with `STAKE_RATE_2`) |
| Staking | `STAKE_RATE_2` (+10% rate) | one-shot, requires 2+ forks | counts toward the +35% |
| Staking | `STAKE_LOCKOUT_1` (-100 lockout) | -400 from this alone | -400 lockout (floor 100 blocks) |
| Staking | `STAKE_LOCKOUT_2` (-200 lockout) | one-shot, requires 3+ forks | counts toward the -400 |
| Staking | `STAKE_PROTECT` (+100 immunity-on-unstake) | one-shot | n/a |
| Dark Web | `DARK_KEY_1` (+1 key/fork) | +4 keys/fork from this alone | +4 extra keys/fork |
| Dark Web | `DARK_COST_1` (-1 power cost) | -3 from this alone | -3 cost |
| Dark Web | `DARK_DURATION_1` (+25% duration) | +75% from this alone | +75% duration |
| Dark Web | `DARK_EARNINGS_1` (+3% earnings) | +15% from this alone | +15% earnings |
| Dark Web | `DARK_IMMUNITY_1` (+250 immunity-on-unstake) | one-shot, requires 2+ forks | n/a |
| Dark Web | `DARK_PASSIVE` (+1 key per 2000 blocks) | one-shot, requires 3+ forks | n/a |

**The `PROD_COST_*` IDs are historical.** Since round 194 both raise earnings; neither reduces costs any more. A bot that picked them expecting cheaper units still gets a good pick, just a different one. Production's four earnings picks add up to more than its +50% ceiling, so reaching it takes a mix rather than stacking one ID. `PROD_UNLOCK` is a single pick that makes every remaining unlock free, which rewards an early first fork; `Investment.unlock_cost` already reports the reduced price once you hold it.

On Staking, pure stacking of `STAKE_RATE_1` caps at +25%, and the one-shot `STAKE_RATE_2` brings you to +35%.

**Immunity-on-unstake**: only grants if you're not already immune. Spamming `client.unstake()` to refresh immunity won't work — the prior grant must fully expire first.

---

## Running a bot in production

```python
import os
import time
from simbot import Client

client = Client()  # picks up SIM_API_KEY from env

while True:
    try:
        state = client.perception()
        if not state.active:
            print("No active round — sleeping 5 minutes")
            time.sleep(300)
            continue

        # Your strategy here
        my_strategy(client, state)

    except Exception as exc:
        print(f"Error: {exc}")

    # Tick rate: every ~15 seconds matches block length, every minute is fine for most strategies
    time.sleep(15)
```

The included `examples/` bots follow this pattern.

---

## Rate limits

- **Reads** (GET): 240 / minute
- **Actions** (POST): 60 / minute
- **Fork**: extra 30 / minute cap on top
- **Power**: extra 20 / minute cap on top

The client automatically handles 429 responses with `Retry-After`. You should still avoid hammering — perception once per block (~15s) is plenty.

---

## State export for ML training

Want round-end logs, decision traces, and structured exports for offline training? A **research tier** with sandboxed accounts, exportable Parquet/JSON dumps, private leaderboards, and elevated rate limits is in the works. Reach out via [Discord](https://discord.gg/childrenoftitan) if interested.

---

## Tournaments

Quarterly bot-only tournaments with prize pools. Winning bots get cash, badges, and bragging rights. See the [public leaderboard](https://www.childrenoftitan.com/sim/explorer) for upcoming events.

Top tournament bots are featured on the official Twitter and Discord — share your code, win subscribers.

### Bot-side API

Every dual-mode method on `Client` accepts a `tournament_id` kwarg. Omit it (default) to target the OPEN sim. Pass a tournament round id to target that tournament round instead. Response DTOs are identical between the two surfaces, so existing parsers work in both contexts.

```python
from simbot import Client

with Client() as client:
    # 1. Find an eligible tournament
    rounds = client.list_tournaments(entry_mode="BOT_ONLY")
    rnd = next(r for r in rounds if r["status"] in {"PENDING", "ACTIVE"})

    # 2. Register (idempotent — safe to call on every restart)
    client.register_for_tournament(rnd["id"], bot_name="GreedyBot")

    # 3. Drive the round with the same calls you use for solo
    state = client.perception(tournament_id=rnd["id"])
    client.purchase(investment_id=0, quantity=5, tournament_id=rnd["id"])
    client.stake(amount=state.player.cash * 0.5, tournament_id=rnd["id"])
    client.complete_fork(path="PRODUCTION", tournament_id=rnd["id"])
    client.contribute(amount=state.player.cash * 0.01, tournament_id=rnd["id"])
```

Reference agent: [`examples/tournament_agent.py`](./examples/tournament_agent.py) — auto-selects an eligible tournament, registers, then runs a greedy strategy scoped to that round. Honors the round's feature flags (`disableCardBoosts`, `disableFork`, `disableDarkWeb`).

### What's tournament-only

- `client.list_tournaments(entry_mode=..., visibility=...)` — filter active and pending tournaments
- `client.tournament_state(round_id)` — full state alias for `full_state(tournament_id=...)`
- `client.register_for_tournament(round_id, bot_name=..., bot_author_url=..., invite_code=...)` — registration
- `client.withdraw_from_tournament(round_id)` — withdraw + refund (PENDING rounds only)
- `client.create_tournament(...)` — programmatic creation; supports an optional `creation_fee_gold` layer that pays winners in in-game Gold on top of gems
- `client.purchase(..., tournament_id=...)` — same surface as solo, routes to tournament endpoints

### Prize pools: gems + optional gold

Tournament prize pools have two layers:

- **Gems** — always present. Seeded by `creation_fee_gems` (creator) and grown by `entry_fee_gems` from each registrant.
- **Gold** — optional. Seeded once at creation via `creation_fee_gold` (in-game Gold balance). No equivalent entry-fee path; the pool stays at the creator's seed.

Both layers are distributed by the same per-rank `prize_distribution_json` shape. Read the breakdown on `full_state(tournament_id=...).round`:

```python
state = client.tournament_state(rnd["id"])
print("Gem pool:",  state["round"]["prizePoolGems"],  state["round"]["prizeDistribution"])
print("Gold pool:", state["round"]["prizePoolGold"],  state["round"]["goldDistribution"])
# goldDistribution is None when the creator didn't seed a gold layer.
```

Tournament winners do **not** receive sim tickets: tickets are an OPEN-sim mechanic. Keys you earn during a tournament (through forks) transfer back to your sim key balance when the round finalizes.

### What stays sim-only

`profile`, `history`, `cards`, `autostake_status`, `power_info`, and the SSE `stream` are sim-only — passing `tournament_id` to them raises `ValueError`. For tournament autostake/power/card state, read it off `perception(tournament_id=...)` instead (`.player.auto_stake_enabled`, `.powers`, and the cards block in `.raw`).

`buy_keys` and `buy_keys_info` are **dual-mode**: solo keys go to your sim key balance and stay spendable across future rounds, while tournament keys go to that round's own balance and transfer back when it is finalized. Bots can buy keys on either surface, since dark-web powers are core gameplay.

### Auth + eligibility

Tournaments use the same `Bearer sim_<hex>` API key as solo. A call made with your API key counts as a bot and a call from a logged-in browser counts as a human, and the server uses that to enforce `BOT_ONLY` / `HUMAN_ONLY` registration. For `MIXED` tournaments anyone can register.

For `PRIVATE` tournaments you need either an explicit invite from the round's creator or the round's `inviteCode` URL parameter, passed as `invite_code=` on `register_for_tournament(...)`.

---

## Contributing

PRs welcome. The kit is intentionally minimal so anyone can read the whole thing in 30 minutes. If you build a non-trivial example bot, send a PR — we'll feature good ones in the README.

```bash
# Dev setup
pip install -e ".[dev]"
pytest
ruff check .
mypy simbot/
```

---

## License

MIT. See [LICENSE](LICENSE).

---

## Links

- Game: [childrenoftitan.com](https://www.childrenoftitan.com)
- API docs: [childrenoftitan.com/sim/docs](https://www.childrenoftitan.com/sim/docs)
- Leaderboard: [childrenoftitan.com/sim/explorer](https://www.childrenoftitan.com/sim/explorer)
- Discord: [discord.gg/childrenoftitan](https://discord.gg/childrenoftitan)
- Twitter: [@childrenoftitan](https://twitter.com/childrenoftitan)
