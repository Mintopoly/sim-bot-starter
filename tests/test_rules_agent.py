"""The rules agent must run Companion rules exactly as the Companion does.

A player who moves their rules from the in-game Companion to the API expects the same rules to
do the same thing, so each test pins one rule of the Companion's executor (moon-v2
src/lib/sim-companion-rules.ts and the sim-companion cron): condition matching, the buy
quantity, which rule set runs, and one action per tick with fall-through to the next rule.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx

from examples import rules_agent as ra
from simbot import Client, PerceptionState
from tests.fakesim import BASE_URL, FakeSim


class StopBot(BaseException):
    pass


@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_API_KEY", "sim_" + "a" * 64)
    monkeypatch.setenv("SIM_API_URL", BASE_URL)


@pytest.fixture
def sim() -> Iterator[FakeSim]:
    game = FakeSim()
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        game.install(router)
        yield game


def state_of(sim: FakeSim) -> PerceptionState:
    return PerceptionState._from_json(sim.solo_perception())


def rule(action: dict[str, Any], *conditions: dict[str, Any], rid: str = "r",
         enabled: bool = True) -> dict[str, Any]:
    return {"id": rid, "enabled": enabled, "conditions": list(conditions), "action": action}


# ─── conditions ──────────────────────────────────────────────────────────


def test_conditions_compare_cash_count_block_and_fork(sim: FakeSim) -> None:
    sim.owned = {0: 7}
    sim.forks = 2
    s = state_of(sim)
    match = ra.conditions_match
    assert match([], s)
    assert match([{"field": "CASH", "op": "GTE", "value": 0.0000005}], s)
    assert not match([{"field": "CASH", "op": "GTE", "value": 1.0}], s)
    assert match([{"field": "COUNT", "op": "EQ", "value": 7, "investmentId": 0}], s)
    assert match([{"field": "COUNT", "op": "LTE", "value": 0, "investmentId": 1}], s)
    assert match([{"field": "BLOCK", "op": "GTE", "value": 100}], s)
    assert match([{"field": "FORK", "op": "EQ", "value": 2}], s)
    # ALL conditions must hold
    assert not match([{"field": "FORK", "op": "EQ", "value": 2},
                      {"field": "FORK", "op": "EQ", "value": 3}], s)


def test_an_unknown_condition_never_matches(sim: FakeSim) -> None:
    assert not ra.conditions_match([{"field": "MOOD", "op": "EQ", "value": 1}], state_of(sim))


# ─── buy quantity (computeBuyQuantity) ───────────────────────────────────


def test_buy_quantity_fixed_is_clamped_to_1_through_100(sim: FakeSim) -> None:
    sim.cash = 1e9
    s = state_of(sim)
    assert ra.buy_quantity({"type": "BUY", "investmentId": 0, "quantity": 0}, s) == 1
    assert ra.buy_quantity({"type": "BUY", "investmentId": 0, "quantity": 3}, s) == 3
    assert ra.buy_quantity({"type": "BUY", "investmentId": 0, "quantity": 500}, s) == 100


def test_buy_quantity_max_stops_at_cash_price_cap_and_target(sim: FakeSim) -> None:
    s = state_of(sim)  # 0.000001 cash, first unit 0.00000005, x1.06 per unit
    by_cash = ra.buy_quantity({"type": "BUY", "investmentId": 0, "buyMax": True}, s)
    assert 0 < by_cash < 100
    capped = ra.buy_quantity(
        {"type": "BUY", "investmentId": 0, "buyMax": True, "maxUnitPrice": 0.00000006}, s)
    assert capped == 4  # 5.0e-8, 5.3e-8, 5.6e-8, 5.96e-8 fit; the 5th is 6.3e-8
    sim.owned = {0: 3}
    s = state_of(sim)
    assert ra.buy_quantity(
        {"type": "BUY", "investmentId": 0, "buyMax": True, "untilCount": 5}, s) == 2
    assert ra.buy_quantity(
        {"type": "BUY", "investmentId": 0, "buyMax": True, "untilCount": 3}, s) == 0


def test_buy_quantity_is_zero_for_a_locked_investment(sim: FakeSim) -> None:
    sim.cash = 1e9
    assert ra.buy_quantity({"type": "BUY", "investmentId": 1, "buyMax": True}, state_of(sim)) == 0


# ─── which rules run ─────────────────────────────────────────────────────


def test_the_committed_snapshot_runs_over_the_draft() -> None:
    locked = [rule({"type": "BUY", "investmentId": 0}, rid="committed")]
    draft = [rule({"type": "BUY", "investmentId": 0}, rid="edited")]
    assert [r["id"] for r in ra.rules_from_companion({"locked": locked, "rules": draft})] == [
        "committed"]
    assert [r["id"] for r in ra.rules_from_companion({"locked": None, "rules": draft})] == [
        "edited"]


def test_pausing_a_rule_in_the_draft_pauses_the_committed_rule() -> None:
    locked = [rule({"type": "BUY", "investmentId": 0}, rid="a")]
    draft = [rule({"type": "BUY", "investmentId": 0}, rid="a", enabled=False)]
    (only,) = ra.rules_from_companion({"locked": locked, "rules": draft})
    assert only["enabled"] is False


# ─── firing ──────────────────────────────────────────────────────────────


def test_first_rule_that_can_act_fires_and_only_one(sim: FakeSim) -> None:
    rules = [
        rule({"type": "BUY", "investmentId": 1, "buyMax": True}, rid="locked-line"),  # can't act
        rule({"type": "BUY", "investmentId": 0, "quantity": 2}, rid="rig"),
        rule({"type": "BUY", "investmentId": 0, "quantity": 5}, rid="never"),
    ]
    with Client() as client:
        outcome = ra.fire_one(client, state_of(sim), rules)
    assert outcome == "rule rig: bought 2x Mining Rig"
    assert (sim.purchases, sim.purchase_requests) == (2, 1)


def test_disabled_and_non_matching_rules_are_skipped(sim: FakeSim) -> None:
    rules = [
        rule({"type": "BUY", "investmentId": 0, "quantity": 9}, rid="off", enabled=False),
        rule({"type": "BUY", "investmentId": 0, "quantity": 9},
             {"field": "BLOCK", "op": "GTE", "value": 10**9}, rid="later"),
    ]
    with Client() as client:
        assert ra.fire_one(client, state_of(sim), rules) == "no rule could act"
    assert sim.purchases == 0


def test_unlock_is_skipped_once_unlocked(sim: FakeSim) -> None:
    sim.cash = 1.0
    rules = [rule({"type": "UNLOCK", "investmentId": 1}, rid="open"),
             rule({"type": "BUY", "investmentId": 1, "quantity": 1}, rid="buy")]
    with Client() as client:
        assert ra.fire_one(client, state_of(sim), rules) == "rule open: unlocked Validator Node"
        assert ra.fire_one(client, state_of(sim), rules) == "rule buy: bought 1x Validator Node"


# ─── end to end ──────────────────────────────────────────────────────────


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str], ticks: int) -> None:
    slept = 0

    def fake_sleep(_s: float) -> None:
        nonlocal slept
        slept += 1
        if slept >= ticks:
            raise StopBot

    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(sys, "argv", ["rules_agent.py", *argv])
    with pytest.raises(StopBot):
        ra.main()


def test_runs_the_players_companion_rules_from_the_api(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim.companion = {"name": "Kernel", "enabled": False, "locked": None,
                     "rules": [rule({"type": "BUY", "investmentId": 0, "quantity": 3})]}
    _run(monkeypatch, [], ticks=2)
    assert (sim.purchases, sim.purchase_requests) == (6, 2)


def test_runs_rules_from_a_file(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([rule({"type": "BUY", "investmentId": 0, "quantity": 4})]))
    _run(monkeypatch, ["--rules", str(path)], ticks=1)
    assert sim.purchases == 4


def test_the_bundled_example_file_is_valid(sim: FakeSim, monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(ra.__file__).with_name("rules.example.json")
    _run(monkeypatch, ["--rules", str(example)], ticks=2)
    assert sim.purchases > 0


def test_falls_back_to_starter_rules_without_a_companion(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(monkeypatch, [], ticks=2)
    assert sim.purchases > 0
