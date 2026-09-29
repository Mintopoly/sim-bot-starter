"""Typed dataclasses for Sim API responses.

These mirror the shape returned by `/api/sim/perception` and the ancillary
endpoints. Fields not present in a particular response default to `None` rather
than raising — the API evolves, and a starter kit shouldn't break on a new
field appearing.

Naming convention: snake_case Python fields, mapped from the camelCase JSON via
the `_from_json` classmethods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ─── Enums ────────────────────────────────────────────────────────────────


class Path(str, Enum):
    """The three fork paths. Selected once on the first fork; locked for the round."""

    PRODUCTION = "PRODUCTION"
    STAKING = "STAKING"
    DARK_WEB = "DARK_WEB"


class DarkWebPower(str, Enum):
    """Offensive and defensive powers purchased with encryption keys.

    Wire format: camelCase string values. The server's TypeScript enum
    uses `BLACKOUT`/`IMMUNITY`/... as member names but emits the values
    (`"blackout"`, `"shortCircuit"`, ...) on the wire.
    """

    BLACKOUT = "blackout"
    IMMUNITY = "immunity"
    SHORT_CIRCUIT = "shortCircuit"
    CASH_BOOST = "cashBoost"
    UNLIMITED_STAKING = "unlimitedStaking"


# ─── Dataclasses ──────────────────────────────────────────────────────────


def _get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First-non-None pick across a list of camel/snake keys."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


@dataclass(frozen=True)
class EconomyPreset:
    """Which of the five coefficient presets a round drew.

    Every round picks one at random and it sets all unit prices, price growth
    and payouts, so the same strategy plays very differently across them. The
    index is stable: 0 Classic, 1 Quick Return, 2 Heavy Industry, 3 Steep
    Tiers, 4 Deep Field. The per-investment numbers themselves are already on
    `Investment.coefficient` / `next_cost`; this is the name for them.
    """

    id: int
    name: str
    i18n_key: str
    description: str

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> EconomyPreset:
        return cls(
            id=int(d.get("id", 0)),
            name=str(d.get("name", "")),
            i18n_key=str(d.get("i18nKey", "")),
            description=str(d.get("description", "")),
        )


@dataclass(frozen=True)
class Round:
    id: str
    number: int
    current_block: int
    total_blocks: int
    blocks_remaining: int
    block_length_seconds: int
    staking_rate: float
    contribution_goal: float
    current_contributions: float
    contribution_progress: float
    start_time: str
    # None when the server predates the field (added 2026-09-29).
    economy: EconomyPreset | None = None

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> Round:
        total = int(d.get("totalBlocks", 0))
        cur = int(d.get("currentBlock", 0))
        econ = d.get("economy")
        return cls(
            id=str(d.get("id", "")),
            number=int(d.get("number", 0)),
            current_block=cur,
            total_blocks=total,
            blocks_remaining=int(d.get("blocksRemaining", max(0, total - cur))),
            block_length_seconds=int(d.get("blockLengthSeconds", 15)),
            staking_rate=float(d.get("stakingRate", 0.0)),
            contribution_goal=float(d.get("contributionGoal", 0.0)),
            current_contributions=float(d.get("currentContributions", 0.0)),
            contribution_progress=float(d.get("contributionProgress", 0.0)),
            start_time=str(d.get("startTime", "")),
            economy=EconomyPreset._from_json(econ) if isinstance(econ, dict) else None,
        )


@dataclass(frozen=True)
class Player:
    cash: float
    staked_value: float
    investment_value: float
    net_worth: float
    earnings_per_block: float
    rank: int
    total_players: int
    fork_number: int
    fork_bonus: float
    fork_bonus_percent: float
    path: Path | None
    path_bonuses: tuple[str, ...]
    tickets: int
    is_blacked_out: bool
    blackout_ends_in: int
    has_immunity: bool
    immunity_ends_in: int
    auto_stake_enabled: bool
    short_circuit: dict[str, Any] | None

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> Player:
        path_raw = d.get("path")
        path = Path(path_raw) if path_raw in {p.value for p in Path} else None
        return cls(
            cash=float(d.get("cash", 0.0)),
            staked_value=float(d.get("stakedValue", 0.0)),
            investment_value=float(d.get("investmentValue", 0.0)),
            net_worth=float(d.get("netWorth", 0.0)),
            earnings_per_block=float(d.get("earningsPerBlock", 0.0)),
            rank=int(d.get("rank", -1)),
            total_players=int(d.get("totalPlayers", 0)),
            fork_number=int(d.get("forkNumber", 0)),
            fork_bonus=float(d.get("forkBonus", 0.0)),
            fork_bonus_percent=float(d.get("forkBonusPercent", 0.0)),
            path=path,
            path_bonuses=tuple(d.get("pathBonuses") or ()),
            tickets=int(d.get("tickets", 0)),
            is_blacked_out=bool(d.get("isBlackedOut", False)),
            blackout_ends_in=int(d.get("blackoutEndsIn", 0)),
            has_immunity=bool(d.get("hasImmunity", False)),
            immunity_ends_in=int(d.get("immunityEndsIn", 0)),
            auto_stake_enabled=bool(d.get("autoStakeEnabled", False)),
            short_circuit=d.get("shortCircuit"),
        )


@dataclass(frozen=True)
class Investment:
    id: int
    name: str
    tier: int
    owned: int
    unlocked: bool
    next_cost: float
    unlock_cost: float | None
    can_afford: bool
    can_afford_unlock: bool
    earnings_per_unit: float
    total_earnings: float
    cost_multiplier: float
    earnings_multiplier: float
    coefficient: float

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> Investment:
        unlock = d.get("unlockCost")
        return cls(
            id=int(d.get("id", 0)),
            name=str(d.get("name", "")),
            tier=int(d.get("tier", 1)),
            owned=int(d.get("owned", 0)),
            unlocked=bool(d.get("unlocked", False)),
            next_cost=float(d.get("nextCost", 0.0)),
            unlock_cost=float(unlock) if unlock is not None else None,
            can_afford=bool(d.get("canAfford", False)),
            can_afford_unlock=bool(d.get("canAffordUnlock", False)),
            earnings_per_unit=float(d.get("earningsPerUnit", 0.0)),
            total_earnings=float(d.get("totalEarnings", 0.0)),
            cost_multiplier=float(d.get("costMultiplier", 1.0)),
            earnings_multiplier=float(d.get("earningsMultiplier", 1.0)),
            coefficient=float(d.get("coefficient", 1.0)),
        )


@dataclass(frozen=True)
class Event:
    """A live newsfeed event (active, upcoming, or recent)."""

    id: str | None
    title: str
    type: str | None
    investment_id: int | None
    value: float | None
    blocks_remaining: int | None
    blocks_until: int | None
    end_block: int | None

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> Event:
        return cls(
            id=d.get("id"),
            title=str(d.get("title", "")),
            type=d.get("type"),
            investment_id=d.get("investmentId"),
            value=d.get("value"),
            blocks_remaining=d.get("blocksRemaining"),
            blocks_until=d.get("blocksUntil"),
            end_block=d.get("endBlock"),
        )


@dataclass(frozen=True)
class ForkAnalysis:
    can_fork: bool
    projected_bonus: float
    projected_bonus_percent: float
    formula: str
    needs_path_selection: bool
    bonus_draft_preview: tuple[dict[str, Any], ...] | None
    # Blocks until the next fork is allowed. Server enforces a cooldown to
    # prevent stacking path bonuses (STAKE_RATE_1, PROD_EARNINGS_1) by spamming
    # the remaining forks in quick succession. 0 means forkable right now.
    cooldown_blocks_remaining: int

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> ForkAnalysis:
        preview = d.get("bonusDraftPreview")
        return cls(
            can_fork=bool(d.get("canFork", False)),
            projected_bonus=float(d.get("projectedBonus", 0.0)),
            projected_bonus_percent=float(d.get("projectedBonusPercent", 0.0)),
            formula=str(d.get("formula", "")),
            needs_path_selection=bool(d.get("needsPathSelection", False)),
            bonus_draft_preview=tuple(preview) if preview else None,
            cooldown_blocks_remaining=int(d.get("cooldownBlocksRemaining", 0)),
        )


@dataclass(frozen=True)
class PowerInfo:
    id: DarkWebPower
    name: str
    cost: int | None
    can_afford: bool
    requires_target: bool

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> PowerInfo:
        return cls(
            id=DarkWebPower(d["id"]),
            name=str(d.get("name", "")),
            cost=d.get("cost"),
            can_afford=bool(d.get("canAfford", False)),
            requires_target=bool(d.get("requiresTarget", False)),
        )


@dataclass(frozen=True)
class Target:
    """A targetable rival for offensive dark-web powers."""

    user_id: str
    username: str
    net_worth: float
    rank: int

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> Target:
        return cls(
            user_id=str(d.get("userId", "")),
            username=str(d.get("username", "")),
            net_worth=float(d.get("netWorth", 0.0)),
            rank=int(d.get("rank", -1)),
        )


@dataclass(frozen=True)
class StakingInfo:
    base_rate: float
    effective_rate: float
    lockout_blocks_remaining: int
    can_stake: bool
    can_unstake: bool
    has_unlimited_staking: bool
    unlimited_staking_ends_in: int

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> StakingInfo:
        return cls(
            base_rate=float(d.get("baseRate", 0.0)),
            effective_rate=float(d.get("effectiveRate", 0.0)),
            lockout_blocks_remaining=int(d.get("lockoutBlocksRemaining", 0)),
            can_stake=bool(d.get("canStake", False)),
            can_unstake=bool(d.get("canUnstake", False)),
            has_unlimited_staking=bool(d.get("hasUnlimitedStaking", False)),
            unlimited_staking_ends_in=int(d.get("unlimitedStakingEndsIn", 0)),
        )


@dataclass(frozen=True)
class LeaderboardEntry:
    rank: int
    username: str
    net_worth: float
    is_current_user: bool

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> LeaderboardEntry:
        return cls(
            rank=int(d.get("rank", 0)),
            username=str(d.get("username", "")),
            net_worth=float(d.get("netWorth", 0.0)),
            is_current_user=bool(_get(d, "isCurrentUser", "isYou", default=False)),
        )


@dataclass(frozen=True)
class PerceptionState:
    """Full bot-optimized game state from `/api/sim/perception`.

    This is the single endpoint to call every block. It rolls up round, player,
    investments, events, fork analysis, dark web, staking, and leaderboard
    into one structured payload — replacing 5+ GETs.
    """

    active: bool
    round: Round | None
    player: Player | None
    investments: tuple[Investment, ...]
    events_active: tuple[Event, ...]
    events_upcoming: tuple[Event, ...]
    events_recent: tuple[Event, ...]
    fork: ForkAnalysis | None
    powers: tuple[PowerInfo, ...]
    targets: tuple[Target, ...]
    staking: StakingInfo | None
    leaderboard: tuple[LeaderboardEntry, ...]
    meta: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def _from_json(cls, d: dict[str, Any]) -> PerceptionState:
        active = bool(d.get("active", False))
        if not active:
            return cls(
                active=False,
                round=None,
                player=None,
                investments=(),
                events_active=(),
                events_upcoming=(),
                events_recent=(),
                fork=None,
                powers=(),
                targets=(),
                staking=None,
                leaderboard=(),
                meta=d.get("meta") or {},
                raw=d,
            )

        # Trainer's tournament `:id/perception` endpoint returns the
        # play-page "full state" shape (top-level `state`, `forkConfig`,
        # `events.past`, no `darkWeb`) instead of the solo perception
        # shape (`player`, `fork`, `events.recent`, `darkWeb`). Detect and
        # rewrite so the same PerceptionState dataclass works in both
        # contexts — bots get a uniform parser as the README promises.
        normalized = _normalize_tournament_perception(d) if _looks_like_tournament_shape(d) else d

        events = normalized.get("events") or {}
        dark_web = normalized.get("darkWeb") or {}
        return cls(
            active=True,
            round=Round._from_json(normalized.get("round") or {}),
            player=Player._from_json(normalized.get("player") or {}),
            investments=tuple(
                Investment._from_json(i) for i in normalized.get("investments") or ()
            ),
            events_active=tuple(Event._from_json(e) for e in events.get("active") or ()),
            events_upcoming=tuple(Event._from_json(e) for e in events.get("upcoming") or ()),
            events_recent=tuple(Event._from_json(e) for e in events.get("recent") or ()),
            fork=ForkAnalysis._from_json(normalized.get("fork") or {}),
            powers=tuple(PowerInfo._from_json(p) for p in dark_web.get("powers") or ()),
            targets=tuple(Target._from_json(t) for t in dark_web.get("targets") or ()),
            staking=StakingInfo._from_json(normalized.get("staking") or {}),
            leaderboard=tuple(
                LeaderboardEntry._from_json(e) for e in normalized.get("leaderboard") or ()
            ),
            meta=normalized.get("meta") or {},
            raw=d,
        )


# ─── Tournament-shape compatibility ───────────────────────────────────────


def _looks_like_tournament_shape(d: dict[str, Any]) -> bool:
    """True if `d` looks like the tournament full-state payload.

    The solo `/api/sim/perception` response has a top-level `player` block.
    The tournament `/api/trainer/tournament/{id}/perception` response uses `state` at
    the top level and `forkConfig` instead of `fork`. We sniff on absence of
    `player` + presence of `state` to disambiguate without committing to a
    schema version field.
    """
    return (
        d.get("player") is None
        and isinstance(d.get("state"), dict)
        and isinstance(d.get("forkConfig"), dict)
    )


def _normalize_tournament_investment(inv: Any) -> Any:
    """Map one trainer investment row onto the solo perception field names.

    The tournament full state names these differently from `/api/sim/perception`:
    `count` (owned), `isUnlocked` (unlocked), `canUnlock` (canAffordUnlock) and
    `currentEarnings` (totalEarnings). It also sends no per-unit figure at all,
    only `baseEarnings` plus an `earningsMultiplier` that it OMITS when no event
    applies, so the per-unit value is rebuilt the way solo computes it.

    Left unmapped, `Investment._from_json` found none of those keys and read every
    tournament line as not owned, not unlocked, not unlockable and earning
    nothing, so a bot filtering `unlocked and can_afford` never bought anything.

    A key the row already carries under its solo name always wins, so a trainer
    that one day adopts those names is not mapped a second time.
    """
    if not isinstance(inv, dict):
        return inv
    out = dict(inv)
    for solo, alias in (
        ("owned", "count"),
        ("unlocked", "isUnlocked"),
        ("canAffordUnlock", "canUnlock"),
        ("totalEarnings", "currentEarnings"),
    ):
        if solo not in out and alias in out:
            out[solo] = out[alias]
    if "earningsPerUnit" not in out and out.get("baseEarnings") is not None:
        mult = float(out.get("earningsMultiplier") or 1.0)
        out["earningsPerUnit"] = float(out["baseEarnings"]) * mult
    return out


def _normalize_tournament_perception(d: dict[str, Any]) -> dict[str, Any]:
    """Project the tournament full-state payload into a
    perception-shaped dict so `PerceptionState._from_json` can parse both
    surfaces uniformly. Fields the trainer doesn't include (e.g. `darkWeb`)
    are left empty; bots that need them should call
    `power_info(...)` / `cards(...)` on the dedicated endpoints or read
    `state.raw` for the unmapped payload.
    """
    state = d.get("state") or {}
    fork_cfg = d.get("forkConfig") or {}
    path_system = d.get("pathSystem") or {}
    events = d.get("events") or {}
    round_meta = d.get("round") or {}

    selected_bonuses = path_system.get("selectedBonuses") or []
    path_bonus_ids = [
        b["id"] if isinstance(b, dict) and "id" in b else b
        for b in selected_bonuses
    ]

    forks = state.get("forks") or {}
    fork_number = int(forks.get("number", 0))
    fork_bonus = float(forks.get("bonus", 0.0))

    player = {
        "cash": state.get("cash"),
        "stakedValue": state.get("stakedValue"),
        "investmentValue": state.get("investmentValue"),
        "netWorth": state.get("netWorth"),
        "earningsPerBlock": state.get("earningsPerBlock"),
        # userRank is top-level on the tournament response; total_players
        # comes from entryCount on the round block.
        "rank": d.get("userRank"),
        "totalPlayers": round_meta.get("entryCount"),
        "forkNumber": fork_number,
        "forkBonus": fork_bonus,
        "forkBonusPercent": fork_cfg.get("currentBonusPercent"),
        "path": path_system.get("currentPath") if path_system.get("hasSelectedPath") else None,
        "pathBonuses": path_bonus_ids,
        "tickets": d.get("tickets", 0),
        "isBlackedOut": state.get("isBlackedOut", False),
        "blackoutEndsIn": state.get("blackoutEndsIn", 0),
        "hasImmunity": state.get("hasImmunity", False),
        "immunityEndsIn": state.get("immunityEndsIn", 0),
        "autoStakeEnabled": d.get("autoStake", False),
        "shortCircuit": d.get("shortCircuit"),
    }

    fork = {
        "canFork": fork_cfg.get("canFork", False),
        "projectedBonus": fork_cfg.get("projectedBonusGain", 0.0),
        "projectedBonusPercent": fork_cfg.get("projectedBonusGainPercent", 0.0),
        "formula": fork_cfg.get("formula", ""),
        # No "first fork" prompt is exposed in the tournament full state; infer from
        # path-selection status. This mirrors the solo perception field.
        "needsPathSelection": (not path_system.get("hasSelectedPath")) and fork_number == 0,
        # The tournament full state doesn't preview the next bonus draft.
        # Bots can call `client.fork(tournament_id=...)` with no args to
        # get the prompt response instead.
        "bonusDraftPreview": None,
        "cooldownBlocksRemaining": fork_cfg.get("cooldownBlocksRemaining", 0),
    }

    staking = {
        "baseRate": round_meta.get("stakingRate", 0.0),
        # No "effective rate" in the tournament full state, so fall back to the
        # base rate so bots see a non-zero number. Path/event multipliers
        # are not surfaced in this DTO; read `playerEffects` for them.
        "effectiveRate": round_meta.get("stakingRate", 0.0),
        "lockoutBlocksRemaining": state.get("stakingLockoutRemaining", 0),
        "canStake": state.get("stakingLockoutRemaining", 0) == 0
        and (state.get("cash") or 0) > 0,
        "canUnstake": state.get("stakingLockoutRemaining", 0) == 0
        and (state.get("stakedValue") or 0) > 0,
        "hasUnlimitedStaking": state.get("hasUnlimitedStaking", False),
        "unlimitedStakingEndsIn": state.get("unlimitedStakingEndsIn", 0),
    }

    # Tournament round metadata uses `blockLength` (seconds); the perception
    # `Round` dataclass keys it as `blockLengthSeconds`. Also synthesize
    # `blocksRemaining` if absent.
    block_length = round_meta.get("blockLength") or round_meta.get("blockLengthSeconds") or 15
    current_block = round_meta.get("currentBlock", 0)
    total_blocks = round_meta.get("totalBlocks", 0)
    round_norm = {
        **round_meta,
        "blockLengthSeconds": block_length,
        "blocksRemaining": round_meta.get(
            "blocksRemaining", max(0, total_blocks - current_block)
        ),
    }

    return {
        **d,
        "round": round_norm,
        "investments": [_normalize_tournament_investment(i) for i in d.get("investments") or ()],
        "player": player,
        "fork": fork,
        "staking": staking,
        "events": {
            "active": events.get("active") or (),
            "upcoming": events.get("upcoming") or (),
            # Trainer uses `past`; solo perception uses `recent`. Map both.
            "recent": events.get("recent") or events.get("past") or (),
        },
    }


@dataclass(frozen=True)
class SimEvent:
    """A single message from the SSE stream.

    `type` is one of: connected, ping, block:advance, event:start, event:end,
    round:end, power:received. Unknown types are passed through untouched.
    `data` is the parsed JSON payload of the event.
    """

    type: str
    data: dict[str, Any]
