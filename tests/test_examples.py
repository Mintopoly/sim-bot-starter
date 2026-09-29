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
import random
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
    sim.cash = 0.1  # projected bonus 10%, above the 5% trigger
    run_ticks(monkeypatch, example("greedy_agent").run, ticks=3)
    assert (sim.forks, sim.path) == (1, "PRODUCTION")


def test_tournament_agent_completes_its_first_fork(
    sim: FakeSim, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim.cash = 0.1
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
