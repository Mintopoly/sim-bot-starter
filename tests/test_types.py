"""Tests for type parsing - graceful handling of missing/extra fields."""

from __future__ import annotations

from simbot.types import (
    DarkWebPower,
    Investment,
    Path,
    PerceptionState,
    Player,
    Round,
)


def test_round_handles_missing_blocks_remaining() -> None:
    r = Round._from_json({"id": "r", "number": 1, "currentBlock": 100, "totalBlocks": 1000})
    assert r.blocks_remaining == 900


def test_round_handles_completely_missing_data() -> None:
    r = Round._from_json({})
    assert r.id == ""
    assert r.current_block == 0
    assert r.total_blocks == 0
    assert r.blocks_remaining == 0


def test_player_unknown_path_is_none() -> None:
    p = Player._from_json({"path": "INVALID"})
    assert p.path is None


def test_player_valid_path_is_enum() -> None:
    p = Player._from_json({"path": "STAKING"})
    assert p.path == Path.STAKING


def test_investment_handles_null_unlock_cost() -> None:
    i = Investment._from_json({"id": 0, "unlockCost": None})
    assert i.unlock_cost is None


def test_perception_inactive() -> None:
    state = PerceptionState._from_json({"active": False})
    assert not state.active
    assert state.round is None
    assert state.player is None
    assert state.investments == ()


def test_perception_extra_fields_dont_break() -> None:
    payload = {
        "active": True,
        "round": {"id": "r1", "number": 1, "currentBlock": 0, "totalBlocks": 10},
        "player": {"cash": 100.0, "rank": 1},
        "investments": [],
        "events": {"active": [], "upcoming": [], "recent": []},
        "fork": {"canFork": False, "projectedBonus": 0.0, "projectedBonusPercent": 0.0},
        "darkWeb": {"powers": [], "targets": []},
        "staking": {},
        "leaderboard": [],
        "meta": {},
        "future_field_we_dont_know_about": "still works",
    }
    state = PerceptionState._from_json(payload)
    assert state.active
    assert state.player is not None
    assert state.player.cash == 100.0


def test_dark_web_power_enum_round_trip() -> None:
    # Wire values are camelCase strings, matching the server emit format.
    assert DarkWebPower("blackout") == DarkWebPower.BLACKOUT
    assert DarkWebPower("shortCircuit") == DarkWebPower.SHORT_CIRCUIT
    assert DarkWebPower.IMMUNITY.value == "immunity"
    assert DarkWebPower.UNLIMITED_STAKING.value == "unlimitedStaking"


def test_perception_parses_tournament_full_state_shape() -> None:
    """Tournament perception (/api/trainer/tournament/{id}/perception) returns
    the play-page full-state DTO (top-level `state`, `forkConfig`,
    `events.past`), not the solo perception shape. The parser must remap so bots get a
    uniform `state.player.cash` / `state.fork.can_fork` regardless of
    surface. Regression: pre-shim, state.player was None → tournament
    agents looped on "No player state yet" forever.
    """
    tournament_payload = {
        "success": True,
        "active": True,
        "round": {
            "id": "rnd_abc",
            "number": 5,
            "title": "Friday Bot Bash",
            "currentBlock": 2400,
            "totalBlocks": 60000,
            "blockLength": 15,
            "startTime": "2026-05-15T00:00:00Z",
            "stakingRate": 0.0001,
            "contributionGoal": 1_000_000,
            "currentContributions": 0,
            "contributionProgress": 0,
            "entryMode": "BOT_ONLY",
            "visibility": "PUBLIC",
            "disableCardBoosts": False,
            "disableDarkWeb": False,
            "disableFork": False,
            "prizePoolGems": 5000,
            "prizePoolGold": 0,
            "entryFeeGems": 0,
            "entryCount": 22,
            "minParticipants": 15,
        },
        "state": {
            "cash": 12345.67,
            "stakedValue": 100.0,
            "investmentValue": 5000.0,
            "netWorth": 17445.67,
            "earningsPerBlock": 12.5,
            "forks": {"number": 1, "bonus": 0.03},
            "isBlackedOut": False,
            "blackoutEndsIn": 0,
            "hasImmunity": True,
            "immunityEndsIn": 120,
            "hasUnlimitedStaking": False,
            "unlimitedStakingEndsIn": 0,
            "stakingLockoutRemaining": 0,
        },
        "investments": [
            {
                "id": 0,
                "name": "Lemonade",
                "tier": 1,
                "count": 5,
                "isUnlocked": True,
                "nextCost": 100.0,
                "canAfford": True,
                "currentEarnings": 12.5,
            }
        ],
        "forkConfig": {
            "canFork": True,
            "cooldownBlocksRemaining": 0,
            "projectedBonusGain": 0.05,
            "projectedBonusGainPercent": 5.0,
            "currentBonusPercent": 3.0,
            "formula": "netWorth / 5^1",
        },
        "pathSystem": {
            "hasSelectedPath": True,
            "currentPath": "PRODUCTION",
            "selectedBonuses": [
                {"id": "PROD_EARNINGS_1", "name": "Production Efficiency"},
            ],
        },
        "leaderboard": [
            {"rank": 1, "userId": "u_other", "username": "alpha", "netWorth": 99999},
        ],
        "userRank": 7,
        "tickets": 4,
        "autoStake": False,
        "shortCircuit": None,
        "events": {
            "active": [{"id": "ev1", "title": "Boom", "type": "EVENT_BOOM"}],
            "upcoming": [],
            "past": [{"id": "ev0", "title": "Bust"}],
        },
    }
    state = PerceptionState._from_json(tournament_payload)
    assert state.active
    assert state.round is not None
    assert state.round.id == "rnd_abc"
    assert state.round.current_block == 2400
    assert state.round.blocks_remaining == 60000 - 2400
    assert state.round.block_length_seconds == 15

    assert state.player is not None
    assert state.player.cash == 12345.67
    assert state.player.net_worth == 17445.67
    assert state.player.rank == 7
    assert state.player.total_players == 22  # entryCount
    assert state.player.fork_number == 1
    assert state.player.fork_bonus == 0.03
    assert state.player.fork_bonus_percent == 3.0
    assert state.player.has_immunity is True
    assert state.player.is_blacked_out is False
    assert state.player.tickets == 4
    assert state.player.path == Path.PRODUCTION
    assert state.player.path_bonuses == ("PROD_EARNINGS_1",)
    assert state.player.auto_stake_enabled is False

    assert state.fork is not None
    assert state.fork.can_fork is True
    assert state.fork.projected_bonus_percent == 5.0
    assert state.fork.needs_path_selection is False  # path already selected
    assert state.fork.cooldown_blocks_remaining == 0

    assert state.staking is not None
    assert state.staking.can_stake is True
    assert state.staking.can_unstake is True
    assert state.staking.lockout_blocks_remaining == 0
    assert state.staking.has_unlimited_staking is False

    # events.past should map into events_recent
    assert len(state.events_active) == 1
    assert state.events_active[0].title == "Boom"
    assert len(state.events_recent) == 1
    assert state.events_recent[0].title == "Bust"

    assert len(state.investments) == 1
    assert state.investments[0].name == "Lemonade"

    # darkWeb is absent from the tournament full state, so powers/targets stay empty.
    assert state.powers == ()
    assert state.targets == ()

    # Raw payload is preserved on the dataclass for bots that want to read
    # tournament-only fields (pathSystem.effects, playerEffects, etc.).
    assert state.raw is tournament_payload


def test_perception_solo_shape_still_works() -> None:
    """The compatibility shim must NOT touch solo perception responses -
    only payloads that look unmistakably tournament-shaped get rewritten."""
    solo_payload = {
        "active": True,
        "round": {"id": "r", "number": 1, "currentBlock": 50, "totalBlocks": 100},
        "player": {"cash": 999.0, "rank": 3, "totalPlayers": 50},
        "investments": [],
        "events": {"active": [], "upcoming": [], "recent": []},
        "fork": {"canFork": False, "projectedBonus": 0.0},
        "darkWeb": {"powers": [{"id": "immunity", "cost": 1}], "targets": []},
        "staking": {"baseRate": 0.0001, "canStake": True},
        "leaderboard": [],
    }
    state = PerceptionState._from_json(solo_payload)
    assert state.player is not None
    assert state.player.cash == 999.0
    assert state.player.rank == 3
    # Solo darkWeb.powers must be preserved (the tournament shim would
    # have wiped this - make sure it doesn't trigger on solo payloads).
    assert len(state.powers) == 1
    assert state.powers[0].id == DarkWebPower.IMMUNITY


def _tournament_payload_with(investments: list[dict]) -> dict:
    """Smallest tournament full-state payload the shim recognises, carrying `investments`."""
    return {
        "success": True,
        "active": True,
        "round": {
            "id": "rnd_abc", "currentBlock": 10, "totalBlocks": 100,
            "blockLength": 15, "entryCount": 20,
        },
        "state": {
            "cash": 100.0, "stakedValue": 0.0, "investmentValue": 0.0, "netWorth": 100.0,
            "earningsPerBlock": 0.0, "forks": {"number": 0, "bonus": 0.0},
            "stakingLockoutRemaining": 0,
        },
        "investments": investments,
        "forkConfig": {"canFork": False, "cooldownBlocksRemaining": 0, "currentBonusPercent": 0.0},
        "pathSystem": {"hasSelectedPath": False, "selectedBonuses": []},
        "events": {"active": [], "upcoming": [], "past": []},
        "userRank": 1,
        "tickets": 0,
        "autoStake": False,
        "shortCircuit": None,
    }


def test_tournament_investments_map_to_the_solo_field_names() -> None:
    """The trainer names investment fields differently from solo perception
    (the tournament full state): `count` for owned,
    `isUnlocked` for unlocked, `canUnlock` for canAffordUnlock, and it sends
    `baseEarnings` + an optional `earningsMultiplier` instead of
    `earningsPerUnit`. Unmapped, every tournament investment parsed as not
    owned, not unlocked, not unlockable and earning nothing, so a bot filtering
    `unlocked and can_afford` never found a single thing to buy.

    The items below are the exact key set the trainer builds, one per case:
    an unlocked line under an earnings event, an unlocked line with no event
    (the trainer then OMITS the multipliers), and a locked line the player
    can afford to open.
    """
    boosted = {
        "id": 0, "name": "Mining Rig", "icon": "", "tier": 1, "count": 12, "isUnlocked": True,
        "unlockCost": None, "canUnlock": False, "baseCost": 1.0, "coefficient": 1.07,
        "baseEarnings": 0.5, "nextCost": 2.4, "baseCostNext": 2.0, "costMultiplier": 1.2,
        "canAfford": True, "currentEarnings": 9.0, "earningsMultiplier": 1.5,
    }
    plain = {
        "id": 1, "name": "Validator Node", "icon": "", "tier": 1, "count": 3, "isUnlocked": True,
        "unlockCost": None, "canUnlock": False, "baseCost": 3.0, "coefficient": 1.1,
        "baseEarnings": 2.0, "nextCost": 4.0, "baseCostNext": 4.0,
        "canAfford": False, "currentEarnings": 6.0,
    }
    locked = {
        "id": 2, "name": "Oracle", "icon": "", "tier": 2, "count": 0, "isUnlocked": False,
        "unlockCost": 50.0, "canUnlock": True, "baseCost": 10.0, "coefficient": 1.15,
        "baseEarnings": 8.0, "nextCost": 10.0, "baseCostNext": 10.0,
        "canAfford": False, "currentEarnings": 0.0,
    }
    state = PerceptionState._from_json(_tournament_payload_with([boosted, plain, locked]))
    rig, node, oracle = state.investments

    assert (rig.owned, rig.unlocked) == (12, True)
    assert (rig.can_afford, rig.can_afford_unlock) == (True, False)
    assert rig.earnings_per_unit == 0.75          # baseEarnings x earningsMultiplier
    assert rig.total_earnings == 9.0              # currentEarnings, already x count
    assert rig.earnings_multiplier == 1.5
    assert rig.cost_multiplier == 1.2
    assert rig.next_cost == 2.4

    assert (node.owned, node.unlocked, node.can_afford) == (3, True, False)
    assert node.earnings_per_unit == 2.0          # no multiplier sent means 1
    assert node.earnings_multiplier == 1.0
    assert node.cost_multiplier == 1.0

    assert (oracle.owned, oracle.unlocked, oracle.can_afford_unlock) == (0, False, True)
    assert oracle.unlock_cost == 50.0

    # What the example agents actually filter on.
    assert [i.name for i in state.investments if i.unlocked and i.can_afford] == ["Mining Rig"]
    unlockable = [i.name for i in state.investments if not i.unlocked and i.can_afford_unlock]
    assert unlockable == ["Oracle"]


def test_tournament_investment_mapping_leaves_solo_names_alone() -> None:
    """A trainer that one day adopts the solo names must not be double-mapped:
    an explicit solo field always wins over its tournament alias."""
    item = {
        "id": 0, "name": "Mining Rig", "tier": 1,
        "owned": 7, "count": 99,
        "unlocked": True, "isUnlocked": False,
        "canAffordUnlock": False, "canUnlock": True,
        "earningsPerUnit": 4.0, "baseEarnings": 1.0, "earningsMultiplier": 2.0,
        "nextCost": 1.0, "canAfford": True,
    }
    (inv,) = PerceptionState._from_json(_tournament_payload_with([item])).investments
    assert (inv.owned, inv.unlocked) == (7, True)
    assert (inv.can_afford_unlock, inv.earnings_per_unit) == (False, 4.0)


_HEAVY_INDUSTRY = {
    "id": 2,
    "name": "Heavy Industry",
    "i18nKey": "heavyIndustry",
    "description": "Units cost far more and earn far more. The slowest start of the five.",
}


def test_round_economy_parses_on_the_solo_surface() -> None:
    """`round.economy` says which of the five coefficient presets the round
    drew. Solo perception carries it since 2026-09-29."""
    payload = {
        "active": True,
        "round": {"id": "r", "currentBlock": 1, "totalBlocks": 10, "economy": _HEAVY_INDUSTRY},
        "player": {"cash": 1.0},
    }
    state = PerceptionState._from_json(payload)
    assert state.round is not None
    assert state.round.economy is not None
    assert (state.round.economy.id, state.round.economy.name) == (2, "Heavy Industry")
    assert state.round.economy.i18n_key == "heavyIndustry"
    assert state.round.economy.description.startswith("Units cost far more")


def test_round_economy_parses_on_the_tournament_surface() -> None:
    """The trainer sends the same field in the same place, and the tournament
    shim must carry it through untouched."""
    payload = _tournament_payload_with([])
    payload["round"]["economy"] = _HEAVY_INDUSTRY
    state = PerceptionState._from_json(payload)
    assert state.round is not None
    assert state.round.economy is not None
    assert (state.round.economy.id, state.round.economy.name) == (2, "Heavy Industry")


def test_round_economy_is_none_when_the_server_does_not_send_it() -> None:
    """An older server omits the field; that must read as unknown, not crash."""
    state = PerceptionState._from_json(_tournament_payload_with([]))
    assert state.round is not None
    assert state.round.economy is None


def _inv(**over: object) -> Investment:
    base = {"id": 0, "name": "Mining Rig", "owned": 5, "unlocked": True,
            "nextCost": 1.0, "coefficient": 2.0, "canAfford": True}
    return Investment._from_json({**base, **over})


def test_units_affordable_walks_the_price_curve() -> None:
    """Each unit costs `coefficient` times the one before, starting at next_cost:
    1 + 2 + 4 = 7. One purchase of that many is what the server charges too."""
    assert _inv().units_affordable(7.0) == 3
    assert _inv().units_affordable(6.99) == 2
    assert _inv().units_affordable(0.5) == 0


def test_units_affordable_honours_a_price_cap_per_unit() -> None:
    """The AI Companion refuses a 'buy max' rule without a cap, because the cost
    curve is geometric and a large budget otherwise buys ever pricier units."""
    assert _inv().units_affordable(1_000.0, max_unit_price=3.0) == 2  # 1, 2 yes; 4 no


def test_units_affordable_stops_at_a_target_count() -> None:
    assert _inv().units_affordable(1_000.0, until_owned=7) == 2  # owns 5, target 7
    assert _inv().units_affordable(1_000.0, until_owned=5) == 0


def test_units_affordable_respects_the_limit_and_locked_lines() -> None:
    assert _inv(coefficient=1.0).units_affordable(1_000.0, limit=10) == 10
    assert _inv(unlocked=False).units_affordable(1_000.0) == 0


def test_cost_of_sums_the_curve() -> None:
    assert _inv().cost_of(3) == 7.0
    assert _inv().cost_of(0) == 0.0
