"""Run every example agent against the offline game in `fakesim` and check it actually plays.

The unit tests prove the client parses the API. These prove the TEMPLATE works: that an agent
started on a fresh round buys, forks and contributes, the things a new user will judge the kit
by in the first five minutes. Each agent's own loop runs unmodified; only `time.sleep` is
replaced, so a run of N ticks takes milliseconds and then stops.

Nothing here reaches the network, and the key and API URL are always set explicitly, so a local
`.env` can never leak into a test run.
"""

from __future__ import annotations

import importlib
import logging
import random
import re
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import respx

from tests.fakesim import BASE_URL, FakeSim


class StopBot(BaseException):
    """Ends an agent's endless loop. BaseException, so the agents' own `except Exception`
    handlers cannot swallow it."""


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


def run_ticks(monkeypatch: pytest.MonkeyPatch, entry: Callable[[], object], ticks: int) -> None:
    """Run an agent's main loop until it has slept `ticks` times."""
    slept = 0

    def fake_sleep(_seconds: float) -> None:
        nonlocal slept
        slept += 1
        if slept >= ticks:
            raise StopBot

    monkeypatch.setattr(time, "sleep", fake_sleep)
    with pytest.raises(StopBot):
        entry()


def example(name: str):  # type: ignore[no-untyped-def]
    return importlib.import_module(f"examples.{name}")


# ─── a fresh round: 0.000001 cash, first unit 0.00000005 ─────────────────


def test_greedy_agent_buys_from_a_fresh_round(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_ticks(monkeypatch, example("greedy_agent").run, ticks=5)
    assert sim.purchases > 0, "greedy_agent never bought anything on a fresh round"


def test_tier_climber_buys_from_a_fresh_round(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_ticks(monkeypatch, example("tier_climber").run, ticks=5)
    assert sim.purchases > 0


def test_tournament_agent_buys_from_a_fresh_round(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["tournament_agent.py"])
    run_ticks(monkeypatch, example("tournament_agent").main, ticks=5)
    assert sim.purchases > 0, "tournament_agent never bought anything on a fresh round"


def test_random_agent_runs_without_errors(sim: FakeSim, monkeypatch: pytest.MonkeyPatch) -> None:
    random.seed(7)
    run_ticks(monkeypatch, example("random_agent").run, ticks=20)
    assert sim.block > 100


def test_q_learning_agent_runs_and_saves_its_table(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("numpy")
    monkeypatch.chdir(tmp_path)  # the agent writes qtable.npy into the working directory
    run_ticks(monkeypatch, example("q_learning_agent").run, ticks=12)
    assert (tmp_path / "qtable.npy").exists()


# ─── forking: the first fork needs the path AND a bonus in one go ───────


def test_greedy_agent_completes_its_first_fork(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim.cash = 5000.0  # first fork adds 5000: 5000x the current multiplier of 1
    run_ticks(monkeypatch, example("greedy_agent").run, ticks=3)
    assert (sim.forks, sim.path) == (1, "PRODUCTION")


# ─── the example fork rule: gain relative to the current multiplier ─────


@pytest.mark.parametrize("bot", ["greedy_agent", "tournament_agent"])
def test_example_bots_do_not_fork_at_the_old_five_percent_trigger(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch, bot: str
) -> None:
    """projectedBonusPercent 10 means net worth 0.1, minutes into a round. The old
    '>= 5%' rule forked there and spent all seven forks almost at once."""
    sim.cash = 0.1
    monkeypatch.setattr(sys, "argv", [f"{bot}.py"])
    run_ticks(monkeypatch, example(bot).main if bot == "tournament_agent"
              else example(bot).run, ticks=3)
    assert sim.forks == 0


def test_fork_gain_is_measured_against_the_current_multiplier(sim: FakeSim) -> None:
    """A second fork adding 5000 is a big gain at bonus 0, and nothing worth a
    reset at bonus 100,000 (it would add 5% to the multiplier)."""
    from simbot import PerceptionState

    greedy = example("greedy_agent")
    sim.cash, sim.forks = 25_000.0, 1  # adds 25,000 / 5 = 5,000
    assert greedy.fork_is_worth_it(PerceptionState._from_json(sim.solo_perception()))
    sim.fork_bonus = 100_000.0
    assert not greedy.fork_is_worth_it(PerceptionState._from_json(sim.solo_perception()))


def test_fork_rule_never_forks_in_the_last_tenth_of_the_round(sim: FakeSim) -> None:
    """A fork resets net worth to the starting cash; with too little round left
    the run cannot be rebuilt, and the final ranking is taken from net worth."""
    from simbot import PerceptionState

    greedy = example("greedy_agent")
    sim.cash = 5000.0
    sim.block = 110_000
    assert not greedy.fork_is_worth_it(PerceptionState._from_json(sim.solo_perception()))
    sim.block = 100_000
    assert greedy.fork_is_worth_it(PerceptionState._from_json(sim.solo_perception()))


def test_tournament_agent_completes_its_first_fork(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim.cash = 5000.0
    monkeypatch.setattr(sys, "argv", ["tournament_agent.py"])
    run_ticks(monkeypatch, example("tournament_agent").main, ticks=3)
    assert (sim.forks, sim.path) == (1, "PRODUCTION"), (
        "sending only the path never completes a fork: the server does not store it"
    )


# ─── utilities ───────────────────────────────────────────────────────────


def test_verify_endpoints_passes_and_contributes(sim: FakeSim) -> None:
    assert example("verify_endpoints").run() == 0
    assert sim.contributed > 0, "a contribution only has to be greater than zero"


def test_stream_smoke_reads_events(sim: FakeSim, capsys: pytest.CaptureFixture[str]) -> None:
    example("stream_smoke").run()
    assert "block:advance" in capsys.readouterr().out


# ─── output a new user reads ─────────────────────────────────────────────

BOTS = [
    ("greedy_agent", "run"),
    ("tier_climber", "run"),
    ("random_agent", "run"),
    ("q_learning_agent", "run"),
    ("tournament_agent", "main"),
    ("rules_agent", "main"),
]


def _run_bot_capturing(
    name: str, entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if name == "q_learning_agent":
        pytest.importorskip("numpy")
        monkeypatch.chdir(tmp_path)
    if name in {"tournament_agent", "rules_agent"}:
        monkeypatch.setattr(sys, "argv", [f"{name}.py"])
    caplog.set_level(logging.INFO)
    random.seed(3)
    run_ticks(monkeypatch, getattr(example(name), entry), ticks=4)


@pytest.mark.parametrize(("name", "entry"), BOTS)
def test_bot_output_is_plain_ascii(
    name: str, entry: str, sim: FakeSim, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    """A Windows console or a redirected log file is cp1252 by default: arrows,
    multiplication signs and dashes came out as escape codes or replacement marks."""
    _run_bot_capturing(name, entry, monkeypatch, tmp_path, caplog)
    text = caplog.text + capsys.readouterr().out
    assert text.isascii(), sorted({c for c in text if ord(c) > 127})


def test_verify_endpoints_output_is_plain_ascii(
    sim: FakeSim, capsys: pytest.CaptureFixture[str]
) -> None:
    example("verify_endpoints").run()
    out = capsys.readouterr().out
    assert out.isascii(), sorted({c for c in out if ord(c) > 127})


@pytest.mark.parametrize(
    ("name", "entry"), [b for b in BOTS if b[0] in {"greedy_agent", "q_learning_agent",
                                                    "tournament_agent"}]
)
def test_bot_never_shows_zero_cash_while_holding_some(
    name: str, entry: str, sim: FakeSim, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The economy starts at 0.000001: rounding to whole units printed 'cash=$0'
    for a bot that held cash, and a new user reads that as a broken bot."""
    _run_bot_capturing(name, entry, monkeypatch, tmp_path, caplog)
    lines = [r.getMessage() for r in caplog.records if "cash=" in r.getMessage()]
    assert lines, "no status line was logged"
    for line in lines:
        assert not re.search(r"cash=\$?0(\.0+)?(\s|$)", line), line


@pytest.mark.parametrize(("name", "entry"), BOTS)
def test_bot_never_acts_on_stale_cash(
    name: str, entry: str, sim: FakeSim, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Buying spends cash, so a bot that stakes or contributes afterwards from the
    state it read BEFORE the purchase asks for money it no longer has, and the
    server refuses. The error is caught, so the only visible sign is that the
    stake quietly never happens."""
    _run_bot_capturing(name, entry, monkeypatch, tmp_path, caplog)
    assert sim.rejected == [], f"{name} was refused: {sim.rejected}"


@pytest.mark.parametrize("name", ["greedy_agent", "tier_climber", "tournament_agent"])
def test_bot_buys_many_units_in_one_request(
    name: str, sim: FakeSim, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A multi-unit purchase is priced along the curve by the server, so buying
    one unit, re-reading state and buying again cost up to eleven requests a
    tick for what one request does."""
    entry = dict(BOTS)[name]
    _run_bot_capturing(name, entry, monkeypatch, tmp_path, caplog)
    assert sim.purchase_requests >= 1
    assert sim.purchases > sim.purchase_requests, (sim.purchases, sim.purchase_requests)
    assert sim.purchase_requests <= 4  # at most one purchase request per tick


def test_greedy_ranks_by_payback_not_by_earnings_at_micro_prices() -> None:
    """Prices start around 0.00000005. Dividing by max(next_cost, 1.0) made every
    denominator 1, so greedy picked the line that EARNS most, however expensive.
    Line 1 here earns 5x more but costs 100x more: a 20x worse payback."""
    from examples import greedy_agent
    from simbot import PerceptionState

    def line(iid: int, cost: float, earn: float) -> dict[str, object]:
        return {"id": iid, "name": f"L{iid}", "unlocked": True, "canAfford": True,
                "nextCost": cost, "earningsPerUnit": earn, "coefficient": 1.06}

    state = PerceptionState._from_json({
        "active": True,
        "round": {"id": "r", "currentBlock": 1, "totalBlocks": 10},
        "player": {"cash": 1.0},
        "investments": [line(0, 5e-8, 1e-7), line(1, 5e-6, 5e-7)],
    })
    best = greedy_agent.best_affordable(state)
    assert best is not None and best.id == 0
