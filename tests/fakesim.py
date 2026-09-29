"""A small, deterministic stand-in for the Sim API, for running the example agents offline.

It models only what the examples touch, but with the rules that decide whether a bot works at
all, each one checked against the live server code:

* A round starts with 0.000001 cash and the first unit costs 0.00000005 (economy preset 0).
  A bot that waits for an absolute cash amount therefore never buys anything.
* The first fork needs a path AND a bonus. A call with only the path returns the bonus draft and
  does NOT store the path, so the path has to be sent again together with the bonus.
* A contribution only has to be greater than zero.

Every perception read advances the round by one block, so earnings accrue as a bot polls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

BASE_URL = "https://sim.test"
TOURNAMENT_ID = "rnd_test"
STARTING_CASH = 0.000001

# Economy preset 0 ("Classic"), investments 0 and 1, as the server defines them.
INVESTMENTS: dict[int, dict[str, Any]] = {
    0: {"name": "Mining Rig", "tier": 1, "unlock": 0.0, "price": 0.00000005,
        "coef": 1.06, "earn": 0.0000001},
    1: {"name": "Validator Node", "tier": 1, "unlock": 0.02736301, "price": 0.00053514,
        "coef": 1.117, "earn": 0.00000219},
}
DRAFT = ["PROD_EARNINGS_1", "PROD_COST_1", "PROD_UNLOCK"]
MAX_FORKS = 7


def _bad(message: str, code: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"error": message}
    if code:
        body["code"] = code
    return httpx.Response(400, json=body)


def _ok(**payload: Any) -> httpx.Response:
    return httpx.Response(200, json={"success": True, **payload})


@dataclass
class FakeSim:
    cash: float = STARTING_CASH
    block: int = 100
    owned: dict[int, int] = field(default_factory=dict)
    unlocked: set[int] = field(default_factory=lambda: {0})
    staked: float = 0.0
    path: str | None = None
    forks: int = 0
    fork_bonus: float = 0.0
    draft: list[str] = field(default_factory=list)
    contributed: float = 0.0
    purchases: int = 0
    purchase_requests: int = 0
    # Every action the server refused, e.g. "stake": a bot that acts on stale state shows
    # up here even when it catches the error and carries on.
    rejected: list[str] = field(default_factory=list)
    # What GET /api/sim/companions returns as myCompanion (None = the player has none).
    companion: dict[str, Any] | None = None

    # ─── game math ────────────────────────────────────────────────────────

    def price(self, inv_id: int) -> float:
        spec = INVESTMENTS[inv_id]
        return float(spec["price"] * spec["coef"] ** self.owned.get(inv_id, 0))

    def earnings_per_block(self) -> float:
        base = sum(n * INVESTMENTS[i]["earn"] for i, n in self.owned.items())
        return float(base * (1 + self.fork_bonus))

    def investment_value(self) -> float:
        return float(sum(n * INVESTMENTS[i]["price"] for i, n in self.owned.items()))

    def net_worth(self) -> float:
        return self.cash + self.staked + self.investment_value()

    def projected_bonus(self) -> float:
        return self.net_worth() / 5**self.forks

    def can_fork(self) -> bool:
        return self.net_worth() > 0 and self.forks < MAX_FORKS

    def advance(self) -> None:
        self.block += 1
        self.cash += self.earnings_per_block()

    # ─── actions (shared by both surfaces) ────────────────────────────────

    def buy(self, inv_id: int, quantity: int) -> httpx.Response:
        self.purchase_requests += 1
        if inv_id not in self.unlocked:
            return _bad("Investment is locked", "INVESTMENT_LOCKED")
        spec, have = INVESTMENTS[inv_id], self.owned.get(inv_id, 0)
        cost = sum(spec["price"] * spec["coef"] ** (have + k) for k in range(quantity))
        if cost > self.cash:
            self.rejected.append("buy")
            return _bad("Not enough cash for this purchase", "INSUFFICIENT_CASH")
        self.cash -= cost
        self.owned[inv_id] = self.owned.get(inv_id, 0) + quantity
        self.purchases += quantity
        return _ok(action="buy")

    def unlock(self, inv_id: int) -> httpx.Response:
        if inv_id in self.unlocked:
            return _bad("Already unlocked")
        cost = INVESTMENTS[inv_id]["unlock"]
        if cost > self.cash:
            return _bad("Not enough cash to unlock", "INSUFFICIENT_CASH")
        self.cash -= cost
        self.unlocked.add(inv_id)
        return _ok(action="unlock")

    def stake(self, amount: float) -> httpx.Response:
        if not amount > 0 or amount > self.cash:
            self.rejected.append("stake")
            return _bad("Not enough cash to stake", "INSUFFICIENT_CASH")
        self.cash -= amount
        self.staked += amount
        return _ok()

    def unstake(self, amount: float) -> httpx.Response:
        if not amount > 0 or amount > self.staked:
            return _bad("Not enough staked")
        self.staked -= amount
        self.cash += amount
        return _ok()

    def contribute(self, amount: float) -> httpx.Response:
        if not amount > 0 or amount > self.cash:
            self.rejected.append("contribute")
            return _bad("Amount must be a positive finite number")
        self.cash -= amount
        self.contributed += amount
        return _ok()

    def fork(self, body: dict[str, Any]) -> httpx.Response:
        if not self.can_fork():
            return _bad("Fork not allowed")
        chosen_path = self.path or body.get("path")
        if chosen_path is None:
            return httpx.Response(200, json={
                "success": False, "code": "FORK_REQUIRES_PATH_SELECTION",
                "requiresPathSelection": True,
                "availablePaths": [{"id": p} for p in ("PRODUCTION", "STAKING", "DARK_WEB")],
            })
        bonus = body.get("bonusId")
        if bonus is None:
            # The server stores the draft, and only ECHOES the path: nothing is committed yet.
            self.draft = list(DRAFT)
            return httpx.Response(200, json={
                "success": False, "code": "FORK_REQUIRES_BONUS_SELECTION",
                "requiresBonusSelection": True,
                "availableBonuses": [{"id": b, "name": b} for b in self.draft],
                "pathSet": body.get("path") if self.path is None else None,
            })
        if self.draft and bonus not in self.draft:
            return _bad("Bonus was not in your draft options")
        self.fork_bonus += self.projected_bonus()
        self.path = chosen_path
        self.forks += 1
        self.cash, self.staked, self.owned, self.unlocked = STARTING_CASH, 0.0, {}, {0}
        self.draft = []
        return _ok(fork={"forkNumber": self.forks})

    # ─── payloads ─────────────────────────────────────────────────────────

    def _round(self) -> dict[str, Any]:
        return {
            "id": "r1", "number": 1, "currentBlock": self.block, "totalBlocks": 120_000,
            "stakingRate": 0.0001, "contributionGoal": 350_000_000.0,
            "currentContributions": self.contributed, "contributionProgress": 0.0,
            "startTime": "2026-09-29T00:00:00Z",
            "economy": {"id": 0, "name": "Classic", "i18nKey": "classic", "description": ""},
        }

    def solo_perception(self) -> dict[str, Any]:
        investments = []
        for inv_id, spec in INVESTMENTS.items():
            unlocked = inv_id in self.unlocked
            cost = self.price(inv_id)
            investments.append({
                "id": inv_id, "name": spec["name"], "tier": spec["tier"],
                "owned": self.owned.get(inv_id, 0), "unlocked": unlocked,
                "nextCost": cost, "unlockCost": None if unlocked else spec["unlock"],
                "canAfford": unlocked and self.cash >= cost,
                "canAffordUnlock": (not unlocked) and self.cash >= spec["unlock"],
                "earningsPerUnit": spec["earn"],
                "totalEarnings": spec["earn"] * self.owned.get(inv_id, 0),
                "costMultiplier": 1, "earningsMultiplier": 1, "coefficient": spec["coef"],
            })
        return {
            "active": True,
            "round": {**self._round(), "blocksRemaining": 120_000 - self.block,
                      "blockLengthSeconds": 15},
            "player": {
                "cash": self.cash, "stakedValue": self.staked,
                "investmentValue": self.investment_value(), "netWorth": self.net_worth(),
                "earningsPerBlock": self.earnings_per_block(), "rank": 1, "totalPlayers": 10,
                "forkNumber": self.forks, "forkBonus": self.fork_bonus,
                "forkBonusPercent": self.fork_bonus * 100, "path": self.path, "pathBonuses": [],
                "tickets": 0, "isBlackedOut": False, "blackoutEndsIn": 0, "hasImmunity": False,
                "immunityEndsIn": 0, "autoStakeEnabled": False, "shortCircuit": None,
            },
            "investments": investments,
            "events": {"active": [], "upcoming": [], "recent": []},
            "fork": {
                "canFork": self.can_fork(), "projectedBonus": self.projected_bonus(),
                "projectedBonusPercent": self.projected_bonus() * 100,
                "formula": "netWorth / 5^n",
                "needsPathSelection": self.path is None and self.forks == 0,
                "bonusDraftPreview": None, "cooldownBlocksRemaining": 0,
            },
            "darkWeb": {"powers": [{"id": "immunity", "name": "Immunity", "cost": 1,
                                    "canAfford": False, "requiresTarget": False}],
                        "targets": []},
            "staking": {"baseRate": 0.0001, "effectiveRate": 0.0001, "lockoutBlocksRemaining": 0,
                        "canStake": self.cash > 0, "canUnstake": self.staked > 0,
                        "hasUnlimitedStaking": False, "unlimitedStakingEndsIn": 0},
            "leaderboard": [],
            "meta": {"tick": self.block},
        }

    def tournament_state(self) -> dict[str, Any]:
        """The trainer's full-state shape, with its own field names."""
        investments = []
        for inv_id, spec in INVESTMENTS.items():
            unlocked = inv_id in self.unlocked
            cost = self.price(inv_id)
            investments.append({
                "id": inv_id, "name": spec["name"], "icon": "", "tier": spec["tier"],
                "count": self.owned.get(inv_id, 0), "isUnlocked": unlocked,
                "unlockCost": None if unlocked else spec["unlock"],
                "canUnlock": (not unlocked) and self.cash >= spec["unlock"],
                "baseCost": spec["price"], "coefficient": spec["coef"],
                "baseEarnings": spec["earn"], "nextCost": cost, "baseCostNext": cost,
                "canAfford": unlocked and self.cash >= cost,
                "currentEarnings": spec["earn"] * self.owned.get(inv_id, 0),
            })
        return {
            "success": True, "active": True,
            "round": {**self._round(), "blockLength": 15, "entryCount": 10,
                      "entryMode": "BOT_ONLY", "visibility": "PUBLIC",
                      "disableFork": False, "disableDarkWeb": False, "disableCardBoosts": False},
            "state": {
                "cash": self.cash, "stakedValue": self.staked,
                "investmentValue": self.investment_value(), "netWorth": self.net_worth(),
                "earningsPerBlock": self.earnings_per_block(),
                "forks": {"number": self.forks, "bonus": self.fork_bonus},
                "isBlackedOut": False, "blackoutEndsIn": 0, "hasImmunity": False,
                "immunityEndsIn": 0, "hasUnlimitedStaking": False, "unlimitedStakingEndsIn": 0,
                "stakingLockoutRemaining": 0,
            },
            "investments": investments,
            "forkConfig": {
                "canFork": self.can_fork(), "cooldownBlocksRemaining": 0,
                "projectedBonusGain": self.projected_bonus(),
                "projectedBonusGainPercent": self.projected_bonus() * 100,
                "currentBonusPercent": self.fork_bonus * 100, "formula": "netWorth / 5^n",
            },
            "pathSystem": {"enabled": True, "hasSelectedPath": self.path is not None,
                           "currentPath": self.path, "selectedBonuses": []},
            "leaderboard": [], "userRank": 1, "tickets": 0, "autoStake": False,
            "shortCircuit": None,
            "events": {"active": [], "upcoming": [], "past": []},
        }

    # ─── routing ──────────────────────────────────────────────────────────

    def install(self, router: respx.MockRouter) -> None:
        """Register every route the examples call on `router`."""

        def body(request: httpx.Request) -> dict[str, Any]:
            return json.loads(request.content or b"{}")

        def perception(_: httpx.Request) -> httpx.Response:
            self.advance()
            return httpx.Response(200, json=self.solo_perception())

        def purchase(request: httpx.Request) -> httpx.Response:
            b = body(request)
            if b.get("action") == "unlock":
                return self.unlock(int(b["investmentId"]))
            return self.buy(int(b["investmentId"]), int(b.get("quantity", 1)))

        def stake(request: httpx.Request) -> httpx.Response:
            b = body(request)
            if b.get("action") == "unstake":
                return self.unstake(float(b["amount"]))
            return self.stake(float(b["amount"]))

        sse = (b'data: {"type": "connected"}\n\n'
               b'data: {"type": "block:advance", "block": 101}\n\n')

        router.get("/api/sim/perception").mock(side_effect=perception)
        router.get("/api/sim").mock(side_effect=lambda _: httpx.Response(
            200, json={"success": True, "active": True, "round": self._round()}))
        for read in ("events", "paths", "leaderboard", "profile", "history", "cards",
                     "autostake", "power", "buy-keys"):
            router.get(f"/api/sim/{read}").mock(return_value=httpx.Response(200, json={"ok": True}))
        router.post("/api/sim/purchase").mock(side_effect=purchase)
        router.post("/api/sim/stake").mock(side_effect=stake)
        router.post("/api/sim/contribute").mock(
            side_effect=lambda r: self.contribute(float(body(r)["amount"])))
        router.post("/api/sim/fork").mock(side_effect=lambda r: self.fork(body(r)))
        router.post("/api/sim/power").mock(return_value=_bad("Not enough encryption keys"))
        router.post("/api/sim/autostake").mock(
            return_value=_bad("Auto-staking requires the Titanbot card"))
        router.post("/api/sim/dismiss").mock(return_value=_ok())
        router.get("/api/sim/companions").mock(side_effect=lambda _: httpx.Response(
            200, json={"items": [], "myCompanion": self.companion}))
        router.get("/api/sim/stream").mock(return_value=httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}))

        t = f"/api/trainer/tournament/{TOURNAMENT_ID}"
        router.get("/api/trainer/tournament").mock(return_value=httpx.Response(200, json=[{
            "id": TOURNAMENT_ID, "roundNumber": 1, "status": "ACTIVE",
            "entryMode": "BOT_ONLY", "visibility": "PUBLIC", "title": "Test Cup"}]))
        router.post(f"{t}/register").mock(return_value=_ok())
        router.get(t).mock(side_effect=lambda _: httpx.Response(200, json=self.tournament_state()))

        def t_perception(_: httpx.Request) -> httpx.Response:
            self.advance()
            return httpx.Response(200, json=self.tournament_state())

        router.get(f"{t}/perception").mock(side_effect=t_perception)
        router.post(f"{t}/purchase").mock(side_effect=lambda r: self.buy(
            int(body(r)["investmentId"]), int(body(r).get("quantity", 1))))
        router.post(f"{t}/unlock").mock(
            side_effect=lambda r: self.unlock(int(body(r)["investmentId"])))
        router.post(f"{t}/stake").mock(side_effect=lambda r: self.stake(float(body(r)["amount"])))
        router.post(f"{t}/unstake").mock(
            side_effect=lambda r: self.unstake(float(body(r)["amount"])))
        router.post(f"{t}/contribute").mock(
            side_effect=lambda r: self.contribute(float(body(r)["amount"])))
        router.post(f"{t}/fork").mock(side_effect=lambda r: self.fork(body(r)))
        router.post(f"{t}/power").mock(return_value=_bad("Not enough encryption keys"))
