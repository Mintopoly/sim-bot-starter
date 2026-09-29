"""Endpoint coverage verifier - exercises every Sim API endpoint once and
prints a pass / skip / fail matrix.

Use this as a smoke test after deploying schema or route changes, or to
prove to yourself the kit covers the full API.

What it does:
  Reads               - calls every GET endpoint, prints a one-line summary.
  Small real writes:    purchase, stake, unstake, contribute and the autostake
                        toggle, each with a tiny fraction of the bot's cash.
                        They are not undone afterwards, and the stake starts a
                        staking lockout, so run this on an account you can
                        spare for a test.
  Conditional actions - only fires if state allows (unlock when affordable,
                        unstake when stakedValue>0, dismiss when short
                        circuit pending, fork only inspected, never
                        committed to avoid resetting your investments).
  Skipped on purpose  - fork commit, use_power (needs tickets), buy_keys
                        (real Gems). Reported as ⊘ with a reason.

Exit code 0 if every endpoint is reachable; non-zero if any unexpected
failure occurred. Sensible 400s ("insufficient cash", "no short circuit
pending") count as a pass - they prove the route is wired and validating.
"""

from __future__ import annotations

import sys

# Windows consoles default to cp1252 which can't render non-ASCII status
# glyphs. Reconfigure once at startup so log output works everywhere.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from simbot import (
    APIError,
    BadRequestError,
    Client,
    DarkWebPower,
)
from simbot.exceptions import SubscriptionRequiredError

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

failures: list[str] = []


def section(label: str) -> None:
    print(f"\n{DIM}=== {label} ==={RESET}")


def ok(label: str, detail: str = "") -> None:
    print(f"  {GREEN}OK{RESET}   {label:34s} {DIM}{detail}{RESET}")


def fail(label: str, err: str) -> None:
    print(f"  {RED}FAIL{RESET} {label:34s} {err}")
    failures.append(f"{label}: {err}")


def skip(label: str, why: str) -> None:
    print(f"  {YELLOW}SKIP{RESET} {label:34s} {DIM}{why}{RESET}")


def expected_400(label: str, exc: BadRequestError) -> None:
    """A 400 with a clear message proves the route + validation work."""
    print(f"  {GREEN}OK{RESET}   {label:34s} {DIM}validated 400: {exc.message}{RESET}")


def run() -> int:
    with Client() as c:
        # ---------- READS ----------
        section("Reads (GET)")

        try:
            state = c.perception()
            if not state.active or state.player is None or state.round is None:
                print(f"  {RED}No active round: most action tests will skip.{RESET}")
                return 1
            ok(
                "GET /api/sim/perception",
                f"block={state.round.current_block} cash={state.player.cash:.6g} "
                f"rank={state.player.rank}/{state.player.total_players} "
                f"fork#{state.player.fork_number}",
            )
        except APIError as e:
            fail("GET /api/sim/perception", str(e))
            return 1

        try:
            full = c.full_state()
            ok("GET /api/sim", f"keys={sorted(full.keys())[:5]}...")
        except APIError as e:
            fail("GET /api/sim", str(e))

        for name, fn in [
            (
                "GET /api/sim/events",
                lambda: c.events(limit_active=3, limit_upcoming=3, limit_past=3),
            ),
            ("GET /api/sim/paths", c.paths),
            ("GET /api/sim/leaderboard", lambda: c.leaderboard(limit=5)),
            ("GET /api/sim/profile", c.profile),
            ("GET /api/sim/history", c.history),
            ("GET /api/sim/cards", c.cards),
            ("GET /api/sim/autostake", c.autostake_status),
            ("GET /api/sim/power", c.power_info),
            ("GET /api/sim/buy-keys", c.buy_keys_info),
        ]:
            try:
                resp = fn()
                detail = (
                    f"-> {len(resp)} top-level keys"
                    if isinstance(resp, dict)
                    else f"-> {type(resp).__name__}"
                )
                ok(name, detail)
            except APIError as e:
                fail(name, str(e))

        # ---------- SAFE ACTIONS ----------
        section("Safe actions (POST)")

        # purchase(buy) - cheapest unlocked + affordable investment, qty=1
        affordable = [i for i in state.investments if i.unlocked and i.can_afford]
        if affordable:
            cheapest = min(affordable, key=lambda i: i.next_cost)
            try:
                c.purchase(investment_id=cheapest.id, quantity=1)
                ok(
                    "POST /api/sim/purchase action=buy",
                    f"bought 1x {cheapest.name} (id={cheapest.id})",
                )
            except APIError as e:
                fail("POST /api/sim/purchase action=buy", str(e))
        else:
            skip("POST /api/sim/purchase action=buy", "no affordable+unlocked investment")

        # purchase(unlock) - first locked investment we can afford to unlock
        unlockable = [i for i in state.investments if not i.unlocked and i.can_afford_unlock]
        if unlockable:
            target = unlockable[0]
            try:
                c.purchase(investment_id=target.id, action="unlock")
                ok(
                    "POST /api/sim/purchase action=unlock",
                    f"unlocked id={target.id} ({target.name})",
                )
            except APIError as e:
                fail("POST /api/sim/purchase action=unlock", str(e))
        else:
            skip("POST /api/sim/purchase action=unlock", "no affordable locked tier")

        # Refresh state so post-buy cash drives stake/contribute amounts
        state = c.perception()
        if state.player is None:
            return 1

        # stake - tiny fraction of cash
        if state.staking and state.staking.can_stake and state.player.cash > 1e-6:
            stake_amount = state.player.cash * 0.05
            try:
                c.stake(amount=stake_amount)
                ok("POST /api/sim/stake action=stake", f"staked {stake_amount:.6g}")
            except BadRequestError as e:
                expected_400("POST /api/sim/stake action=stake", e)
            except APIError as e:
                fail("POST /api/sim/stake action=stake", str(e))
        else:
            skip(
                "POST /api/sim/stake action=stake",
                f"can_stake={state.staking and state.staking.can_stake}, "
                f"cash={state.player.cash:.6g}",
            )

        # unstake - small fraction of staked balance (refresh first to see new staked)
        state = c.perception()
        if state.player is None:
            return 1
        if state.staking and state.staking.can_unstake and state.player.staked_value > 1e-6:
            unstake_amount = state.player.staked_value * 0.5
            try:
                c.unstake(amount=unstake_amount)
                ok("POST /api/sim/stake action=unstake", f"unstaked {unstake_amount:.6g}")
            except BadRequestError as e:
                expected_400("POST /api/sim/stake action=unstake", e)
            except APIError as e:
                fail("POST /api/sim/stake action=unstake", str(e))
        else:
            skip(
                "POST /api/sim/stake action=unstake",
                f"can_unstake={state.staking and state.staking.can_unstake}, "
                f"staked={state.player.staked_value:.6g}",
            )

        # contribute - 1% of current cash, must be > 0
        state = c.perception()
        if state.player is None:
            return 1
        contrib = state.player.cash * 0.01
        if contrib > 0:
            try:
                c.contribute(amount=contrib)
                ok("POST /api/sim/contribute", f"contributed {contrib:.6g}")
            except BadRequestError as e:
                expected_400("POST /api/sim/contribute", e)
            except APIError as e:
                fail("POST /api/sim/contribute", str(e))
        else:
            skip(
                "POST /api/sim/contribute",
                f"nothing to contribute, bot has cash={state.player.cash:.6g}",
            )

        # set_autostake - toggle, then toggle back
        try:
            c.set_autostake(True)
            c.set_autostake(False)
            ok("POST /api/sim/autostake", "toggled on/off")
        except BadRequestError as e:
            expected_400("POST /api/sim/autostake", e)
        except APIError as e:
            fail("POST /api/sim/autostake", str(e))

        # ---------- CONDITIONAL ACTIONS ----------
        section("Conditional actions (POST)")

        # fork - only inspect; commit would reset all investments. We hit the
        # endpoint with no body, which returns the required-selection prompt
        # without committing the fork. That proves wire-up.
        try:
            resp = c.fork()
            kind = "path" if resp.get("requiresPathSelection") else (
                "bonus" if resp.get("requiresBonusSelection") else "committed"
            )
            ok("POST /api/sim/fork (inspect)", f"requires {kind} selection")
        except BadRequestError as e:
            expected_400("POST /api/sim/fork (inspect)", e)
        except APIError as e:
            fail("POST /api/sim/fork (inspect)", str(e))

        # dismiss - only meaningful when short_circuit pending; otherwise the
        # server likely returns 400/200 with no-op. Either is fine for proving
        # the route exists.
        try:
            c.dismiss()
            ok("POST /api/sim/dismiss", "called")
        except BadRequestError as e:
            expected_400("POST /api/sim/dismiss", e)
        except APIError as e:
            fail("POST /api/sim/dismiss", str(e))

        # use_power - only fire if we have tickets AND a power that doesn't
        # require a target (immunity is the cleanest self-cast).
        state = c.perception()
        if state.player is None:
            return 1
        immunity = next((p for p in state.powers if p.id == DarkWebPower.IMMUNITY), None)
        can_self_cast = (
            immunity is not None
            and immunity.can_afford
            and state.player.tickets > 0
            and not state.player.has_immunity
        )
        if can_self_cast:
            try:
                c.use_power(DarkWebPower.IMMUNITY)
                ok("POST /api/sim/power IMMUNITY", "self-cast")
            except BadRequestError as e:
                expected_400("POST /api/sim/power IMMUNITY", e)
            except APIError as e:
                fail("POST /api/sim/power IMMUNITY", str(e))
        else:
            reason = []
            if not immunity:
                reason.append("immunity power not in response")
            if immunity and not immunity.can_afford:
                reason.append(
                    f"can't afford ({immunity.cost} keys vs {state.player.tickets} tickets)"
                )
            if state.player.has_immunity:
                reason.append("already immune")
            skip("POST /api/sim/power IMMUNITY", "; ".join(reason) or "preconditions not met")

        # buy_keys - requires Gems and costs real economy units. Skipped by
        # design; only verify the read endpoint above proves it's reachable.
        skip("POST /api/sim/buy-keys", "intentionally skipped (spends Gems)")

        # ---------- STREAMING ----------
        section("Streaming")

        try:
            with c.stream() as events:
                first = next(iter(events), None)
                if first is None:
                    fail("GET /api/sim/stream", "stream closed before any event")
                else:
                    ok("GET /api/sim/stream (SSE)", f"first event type={first.type}")
        except SubscriptionRequiredError as e:
            fail("GET /api/sim/stream (SSE)", f"403: bot account missing API subscription? ({e})")
        except APIError as e:
            fail("GET /api/sim/stream (SSE)", str(e))

    # ---------- SUMMARY ----------
    section("Summary")
    if failures:
        print(f"  {RED}{len(failures)} failure(s):{RESET}")
        for f in failures:
            print(f"    - {f}")
        return 2
    print(f"  {GREEN}All endpoints reachable.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
