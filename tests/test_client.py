"""Client tests using respx (httpx mock layer)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from simbot import (
    AuthError,
    BadRequestError,
    Client,
    NoActiveRoundError,
    RateLimitError,
    SubscriptionRequiredError,
)


def test_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIM_API_KEY", raising=False)
    # Neutralize the .env auto-loader so a developer's local .env doesn't
    # silently provide the key and mask the "no key" error path.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_a, **_k: False)
    with pytest.raises(ValueError, match="API key required"):
        Client()


def test_client_validates_key_prefix() -> None:
    with pytest.raises(ValueError, match="must start with 'sim_'"):
        Client(api_key="bogus_key_1234")


def test_wrong_key_is_never_echoed_back() -> None:
    """A value that fails the prefix check is most often a DIFFERENT secret pasted
    by mistake (a Stripe or cloud key). The error ends up in logs, CI output and
    bug reports, so it must not repeat any part of what was passed."""
    wrong = "sk_live_" + "9" * 24
    with pytest.raises(ValueError) as exc_info:
        Client(api_key=wrong)
    message = str(exc_info.value)
    assert "sk_li" not in message
    assert wrong[:3] not in message


def test_client_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_API_KEY", "sim_" + "a" * 64)
    client = Client()
    client.close()


@respx.mock
def test_perception_active(client: Client, base_url: str) -> None:
    payload = {
        "active": True,
        "round": {
            "id": "rnd_1",
            "number": 5,
            "currentBlock": 4200,
            "totalBlocks": 120000,
            "blocksRemaining": 115800,
            "blockLengthSeconds": 15,
            "stakingRate": 0.001,
            "contributionGoal": 50000,
            "currentContributions": 6000,
            "contributionProgress": 12.0,
            "startTime": "2026-03-10T00:00:00.000Z",
        },
        "player": {
            "cash": 1523.45,
            "stakedValue": 500.0,
            "investmentValue": 3200.80,
            "netWorth": 5224.25,
            "earningsPerBlock": 12.34,
            "rank": 7,
            "totalPlayers": 142,
            "forkNumber": 2,
            "forkBonus": 0.045,
            "forkBonusPercent": 4.50,
            "path": "PRODUCTION",
            "pathBonuses": ["prod_efficiency_1"],
            "tickets": 15,
            "isBlackedOut": False,
            "blackoutEndsIn": 0,
            "hasImmunity": False,
            "immunityEndsIn": 0,
            "autoStakeEnabled": True,
            "shortCircuit": None,
        },
        "investments": [
            {
                "id": 0,
                "name": "Mining Rig",
                "tier": 1,
                "owned": 25,
                "unlocked": True,
                "nextCost": 342.50,
                "unlockCost": None,
                "canAfford": True,
                "canAffordUnlock": False,
                "earningsPerUnit": 0.85,
                "totalEarnings": 21.25,
                "costMultiplier": 1.0,
                "earningsMultiplier": 1.2,
                "coefficient": 1.15,
            }
        ],
        "events": {
            "active": [{"id": "e1", "title": "Mining Boom", "type": "EARNINGS_BOOST"}],
            "upcoming": [],
            "recent": [],
        },
        "fork": {
            "canFork": True,
            "projectedBonus": 0.021,
            "projectedBonusPercent": 2.09,
            "formula": "netWorth / 5^2",
            "needsPathSelection": False,
            "bonusDraftPreview": [],
        },
        "darkWeb": {"powers": [], "targets": []},
        "staking": {
            "baseRate": 0.001,
            "effectiveRate": 0.0012,
            "lockoutBlocksRemaining": 0,
            "canStake": True,
            "canUnstake": True,
            "hasUnlimitedStaking": False,
            "unlimitedStakingEndsIn": 0,
        },
        "leaderboard": [
            {"rank": 1, "username": "whale", "netWorth": 50000.0, "isCurrentUser": False}
        ],
        "meta": {"tick": 4200},
    }
    respx.get(f"{base_url}/api/sim/perception").mock(return_value=Response(200, json=payload))

    state = client.perception()
    assert state.active
    assert state.round is not None
    assert state.round.current_block == 4200
    assert state.player is not None
    assert state.player.cash == 1523.45
    assert state.player.rank == 7
    assert len(state.investments) == 1
    assert state.investments[0].name == "Mining Rig"
    assert state.investments[0].can_afford
    assert len(state.events_active) == 1
    assert state.fork is not None
    assert state.fork.can_fork
    assert state.staking is not None
    assert state.staking.can_stake


@respx.mock
def test_perception_inactive(client: Client, base_url: str) -> None:
    respx.get(f"{base_url}/api/sim/perception").mock(
        return_value=Response(200, json={"active": False, "message": "no round"})
    )
    state = client.perception()
    assert not state.active
    assert state.round is None
    assert state.player is None


@respx.mock
def test_purchase_validation(client: Client) -> None:
    with pytest.raises(ValueError, match="action must"):
        client.purchase(investment_id=0, action="invalid")
    with pytest.raises(ValueError, match="investment_id must"):
        client.purchase(investment_id=99)
    with pytest.raises(ValueError, match="quantity"):
        client.purchase(investment_id=0, quantity=0)


@respx.mock
def test_purchase_success(client: Client, base_url: str) -> None:
    respx.post(f"{base_url}/api/sim/purchase").mock(
        return_value=Response(200, json={"success": True, "newCount": 26})
    )
    result = client.purchase(investment_id=0, quantity=1)
    assert result["success"]
    assert result["newCount"] == 26


@respx.mock
def test_stake_validation(client: Client) -> None:
    with pytest.raises(ValueError, match="amount must be positive"):
        client.stake(amount=0)
    with pytest.raises(ValueError, match="amount must be positive"):
        client.unstake(amount=-1)


@respx.mock
def test_buy_keys_validation(client: Client) -> None:
    with pytest.raises(ValueError, match="quantity must be one of"):
        client.buy_keys(quantity=7)


@respx.mock
def test_auth_error_raises_typed(client: Client, base_url: str) -> None:
    respx.get(f"{base_url}/api/sim/perception").mock(
        return_value=Response(401, json={"error": "Unauthorized"})
    )
    with pytest.raises(AuthError) as exc_info:
        client.perception()
    assert exc_info.value.status_code == 401


@respx.mock
def test_subscription_required_raises_typed(client: Client, base_url: str) -> None:
    respx.get(f"{base_url}/api/sim/perception").mock(
        return_value=Response(403, json={"error": "Subscription required"})
    )
    with pytest.raises(SubscriptionRequiredError):
        client.perception()


@respx.mock
def test_no_active_round_raises_typed(client: Client, base_url: str) -> None:
    respx.post(f"{base_url}/api/sim/purchase").mock(
        return_value=Response(400, json={"error": "No active sim round"})
    )
    with pytest.raises(NoActiveRoundError):
        client.purchase(investment_id=0, quantity=1)


@respx.mock
def test_bad_request_raises_typed(client: Client, base_url: str) -> None:
    respx.post(f"{base_url}/api/sim/purchase").mock(
        return_value=Response(400, json={"error": "Not enough cash"})
    )
    with pytest.raises(BadRequestError) as exc_info:
        client.purchase(investment_id=0, quantity=1)
    assert "Not enough cash" in str(exc_info.value)


@respx.mock
def test_rate_limit_raises_typed_after_exhaustion(api_key: str, base_url: str) -> None:
    # Use a fresh client with retries=0 so the first 429 raises immediately
    client = Client(api_key=api_key, base_url=base_url, retries=0)
    respx.get(f"{base_url}/api/sim/perception").mock(
        return_value=Response(
            429,
            json={"error": "Rate limit exceeded"},
            headers={"Retry-After": "5"},
        )
    )
    with pytest.raises(RateLimitError) as exc_info:
        client.perception()
    assert exc_info.value.retry_after_seconds == 5.0


@respx.mock
def test_authorization_header_set(client: Client, base_url: str, api_key: str) -> None:
    route = respx.get(f"{base_url}/api/sim/perception").mock(
        return_value=Response(200, json={"active": False})
    )
    client.perception()
    sent = route.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {api_key}"


@respx.mock
def test_user_agent_set(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/sim/perception").mock(
        return_value=Response(200, json={"active": False})
    )
    client.perception()
    sent = route.calls.last.request
    assert sent.headers["user-agent"].startswith("simbot/")


# ── Error code surfacing ─────────────────────────────────────────────────


@respx.mock
def test_error_code_surfaced_on_bad_request(client: Client, base_url: str) -> None:
    respx.post(f"{base_url}/api/sim/purchase").mock(
        return_value=Response(
            400,
            json={"error": "Not enough cash", "code": "INSUFFICIENT_CASH"},
        )
    )
    with pytest.raises(BadRequestError) as exc_info:
        client.purchase(investment_id=0, quantity=1)
    assert exc_info.value.code == "INSUFFICIENT_CASH"
    assert "Not enough cash" in exc_info.value.message


@respx.mock
def test_error_code_falls_back_to_none_when_missing(client: Client, base_url: str) -> None:
    """Older API deployments may not yet return `code`. Client must not crash."""
    respx.post(f"{base_url}/api/sim/purchase").mock(
        return_value=Response(400, json={"error": "Some error"})
    )
    with pytest.raises(BadRequestError) as exc_info:
        client.purchase(investment_id=0, quantity=1)
    assert exc_info.value.code is None


@respx.mock
def test_no_active_round_detected_via_code(client: Client, base_url: str) -> None:
    """Prefer the explicit code over message-string sniffing."""
    respx.post(f"{base_url}/api/sim/purchase").mock(
        return_value=Response(
            400,
            json={"error": "Round wrapping up", "code": "NO_ACTIVE_ROUND"},
        )
    )
    with pytest.raises(NoActiveRoundError):
        client.purchase(investment_id=0, quantity=1)


@respx.mock
def test_rate_limit_error_includes_code(api_key: str, base_url: str) -> None:
    client = Client(api_key=api_key, base_url=base_url, retries=0)
    respx.get(f"{base_url}/api/sim/perception").mock(
        return_value=Response(
            429,
            json={"error": "Rate limit exceeded", "code": "RATE_LIMIT_EXCEEDED"},
            headers={"Retry-After": "5"},
        )
    )
    with pytest.raises(RateLimitError) as exc_info:
        client.perception()
    assert exc_info.value.code == "RATE_LIMIT_EXCEEDED"


def _record_dotenv_loads(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the loader with a recorder: the test sees WHICH file would be
    loaded, and nothing real (a developer's own .env) is ever read."""
    seen: list[object] = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda path=None, *_a, **_k: seen.append(path))
    return seen


def test_dotenv_is_read_from_where_the_bot_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The README says: copy .env.example to .env in your project. Searching
    from the kit's own install directory instead never finds that file once
    the kit is pip-installed outside the project, and the first step of the
    guide fails with 'API key required'."""
    (tmp_path / ".env").write_text("SIM_API_KEY=sim_" + "b" * 64 + "\n")
    monkeypatch.chdir(tmp_path)
    seen = _record_dotenv_loads(monkeypatch)
    Client(api_key="sim_" + "a" * 64).close()
    assert seen == [str(tmp_path / ".env")]


def test_dotenv_is_found_from_a_subdirectory_of_the_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("SIM_API_KEY=sim_" + "b" * 64 + "\n")
    sub = tmp_path / "bots"
    sub.mkdir()
    monkeypatch.chdir(sub)
    seen = _record_dotenv_loads(monkeypatch)
    Client(api_key="sim_" + "a" * 64).close()
    assert seen == [str(tmp_path / ".env")]


_DRAFT = {
    "success": False,
    "requiresBonusSelection": True,
    "availableBonuses": [{"id": "PROD_EARNINGS_1"}, {"id": "PROD_UNLOCK"}],
}


@respx.mock
def test_complete_fork_sends_the_path_again_with_the_bonus(client: Client, base_url: str) -> None:
    """On the first fork the server returns the draft for a path-only request
    but does not store the path, so the committing request must carry it too."""
    route = respx.post(f"{base_url}/api/sim/fork").mock(
        side_effect=[
            Response(200, json=_DRAFT),
            Response(200, json={"success": True, "fork": {"forkNumber": 1}}),
        ]
    )
    result = client.complete_fork(path="PRODUCTION")
    assert result["success"] is True
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies == [
        {"path": "PRODUCTION"},
        {"path": "PRODUCTION", "bonusId": "PROD_EARNINGS_1"},
    ]


@respx.mock
def test_complete_fork_lets_the_bot_choose_the_bonus(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/sim/fork").mock(
        side_effect=[Response(200, json=_DRAFT), Response(200, json={"success": True})]
    )
    client.complete_fork(path="PRODUCTION", choose=lambda options: options[-1]["id"])
    assert json.loads(route.calls[1].request.content)["bonusId"] == "PROD_UNLOCK"


@respx.mock
def test_complete_fork_needs_a_path_on_the_first_fork(client: Client, base_url: str) -> None:
    respx.post(f"{base_url}/api/sim/fork").mock(
        return_value=Response(200, json={"success": False, "requiresPathSelection": True})
    )
    with pytest.raises(ValueError, match="needs a path"):
        client.complete_fork()


@respx.mock
def test_complete_fork_targets_a_tournament(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/fork").mock(
        side_effect=[Response(200, json=_DRAFT), Response(200, json={"success": True})]
    )
    assert client.complete_fork(path="PRODUCTION", tournament_id="rnd_abc")["success"] is True
    assert route.call_count == 2
