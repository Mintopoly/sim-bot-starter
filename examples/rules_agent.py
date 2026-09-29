"""Rules agent: runs AI Companion rules through the Sim API, every block.

The AI Companion is the in-game rule bot many players already use: a list of
"if these conditions hold, buy or unlock this" rules that the game runs for you
about every five minutes. This agent speaks exactly the same rule language, so
you can:

  * run the rules you already set up in the browser (the default: they are
    fetched with your API key), or
  * load rules from a JSON file in the same shape (--rules my_rules.json; see
    rules.example.json),

and it evaluates them every block instead of every five minutes. It is the
shortest path from "I have a Companion" to "I have a bot I can extend".

Semantics, identical to the Companion's:

  * rules run in order, and a disabled rule is skipped;
  * a rule matches when ALL its conditions hold. A condition compares CASH,
    COUNT (units owned of `investmentId`), BLOCK or FORK with GTE, LTE or EQ;
  * UNLOCK is skipped when the investment is already unlocked or unaffordable;
  * BUY takes a fixed `quantity` or `buyMax` (up to 100), stops at `untilCount`
    without overshooting, and never pays more than `maxUnitPrice` per unit;
    if not even one unit fits, the next rule gets its turn;
  * at most ONE action fires per tick.

If your Companion is enabled as well, it acts on the same cash every five
minutes. Pause it in the browser while this bot runs, or they compete.

Usage:
    SIM_API_KEY=sim_... python examples/rules_agent.py                  # your Companion's rules
    SIM_API_KEY=sim_... python examples/rules_agent.py --rules r.json   # rules from a file
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path as FsPath
from typing import Any

from simbot import (
    BadRequestError,
    Client,
    NoActiveRoundError,
    PerceptionState,
    RateLimitError,
    SimBotError,
)

TICK_SECONDS = 15
NO_ROUND_SLEEP = 300
MAX_BUY_QTY = 100  # the Companion's and the manual buy cap

log = logging.getLogger("rules_agent")

Rule = dict[str, Any]

# Used when there is no Companion and no --rules file. Deliberately free of cash
# amounts: the economy starts at 0.000001 cash, so a CASH threshold only makes
# sense for a round you are looking at (see the README).
DEFAULT_RULES: list[Rule] = [
    {"id": "rig", "enabled": True, "conditions": [],
     "action": {"type": "BUY", "investmentId": 0, "buyMax": True, "untilCount": 50}},
    {"id": "open-node", "enabled": True,
     "conditions": [{"field": "COUNT", "op": "GTE", "value": 50, "investmentId": 0}],
     "action": {"type": "UNLOCK", "investmentId": 1}},
    {"id": "node", "enabled": True, "conditions": [],
     "action": {"type": "BUY", "investmentId": 1, "buyMax": True}},
]


# ─── Rule evaluation (mirrors the Companion) ─────────────────────────────


def _compare(left: float, op: str, right: float) -> bool:
    if op == "GTE":
        return left >= right
    if op == "LTE":
        return left <= right
    return left == right  # "EQ"


def conditions_match(conditions: list[dict[str, Any]], state: PerceptionState) -> bool:
    """True when every condition holds. An unknown field never matches."""
    if state.player is None or state.round is None:
        return False
    counts = {i.id: i.owned for i in state.investments}
    for c in conditions:
        field = c.get("field")
        if field == "CASH":
            left = state.player.cash
        elif field == "COUNT":
            left = counts.get(int(c.get("investmentId", -1)), 0)
        elif field == "BLOCK":
            left = state.round.current_block
        elif field == "FORK":
            left = state.player.fork_number
        else:
            return False
        if not _compare(float(left), str(c.get("op")), float(c.get("value", 0))):
            return False
    return True


def buy_quantity(action: dict[str, Any], state: PerceptionState) -> int:
    """Units a BUY action purchases this tick, the Companion's way."""
    inv = next((i for i in state.investments if i.id == action.get("investmentId")), None)
    if inv is None or state.player is None:
        return 0
    if action.get("buyMax"):
        limit = MAX_BUY_QTY
    else:
        limit = max(1, min(MAX_BUY_QTY, int(action.get("quantity", 1))))
    until = action.get("untilCount")
    return inv.units_affordable(
        state.player.cash,
        max_unit_price=action.get("maxUnitPrice"),
        limit=limit,
        until_owned=int(until) if until is not None else None,
    )


def rules_from_companion(companion: dict[str, Any]) -> list[Rule]:
    """The rules the Companion itself would run: the committed snapshot if there
    is one this round, otherwise the draft. A rule's on/off switch always comes
    from the draft, because pausing a rule in the browser takes effect at once."""
    draft = companion.get("rules") or []
    locked = companion.get("locked")
    rules = locked if isinstance(locked, list) and locked else draft
    live = {r.get("id"): r.get("enabled", True) is not False for r in draft}
    return [{**r, "enabled": live.get(r.get("id"), r.get("enabled", True) is not False)}
            for r in rules]


def fire_one(client: Client, state: PerceptionState, rules: list[Rule]) -> str:
    """Run the first rule that can act, and return what happened."""
    by_id = {i.id: i for i in state.investments}
    for rule in rules:
        if rule.get("enabled") is False:
            continue
        if not conditions_match(rule.get("conditions") or [], state):
            continue
        action = rule.get("action") or {}
        iid = action.get("investmentId")
        inv = by_id.get(iid) if isinstance(iid, int) else None
        if inv is None:
            continue
        if action.get("type") == "UNLOCK":
            if inv.unlocked or not inv.can_afford_unlock:
                continue
            try:
                client.purchase(investment_id=inv.id, action="unlock")
            except BadRequestError:
                continue
            return f"rule {rule.get('id')}: unlocked {inv.name}"
        if action.get("type") == "BUY":
            units = buy_quantity(action, state)
            if units < 1:
                continue
            try:
                client.purchase(investment_id=inv.id, quantity=units)
            except BadRequestError:
                continue
            return f"rule {rule.get('id')}: bought {units}x {inv.name}"
    return "no rule could act"


# ─── Main ─────────────────────────────────────────────────────────────────


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


def load_rules(client: Client, rules_file: str | None) -> list[Rule]:
    if rules_file:
        rules = json.loads(FsPath(rules_file).read_text(encoding="utf-8"))
        log.info("Loaded %d rules from %s", len(rules), rules_file)
        return list(rules)
    companion = client.my_companion()
    if companion:
        rules = rules_from_companion(companion)
        if rules:
            log.info("Running the %d rules of your Companion %s", len(rules),
                     companion.get("name", ""))
            if companion.get("enabled"):
                log.warning("Your Companion is enabled too and spends the same cash. "
                            "Pause it in the browser while this bot runs.")
            return rules
    log.info("No Companion rules found, using the built-in starter rules")
    return DEFAULT_RULES


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AI Companion rules every block.")
    parser.add_argument("--rules", help="JSON file with a list of rules (Companion format).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    with Client() as client:
        rules = load_rules(client, args.rules)
        while True:
            try:
                state = client.perception()
                if not state.active or state.player is None or state.round is None:
                    log.info("No active round. Sleeping %ds", NO_ROUND_SLEEP)
                    time.sleep(NO_ROUND_SLEEP)
                    continue
                outcome = fire_one(client, state, rules)
                log.info("block %d  cash=$%s  -> %s", state.round.current_block,
                         fmt_money(state.player.cash), outcome)
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
            time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye.")
