"""Tests for the tournament surface added in Phase B.

Verifies the bot client routes to the right URLs for solo vs tournament
contexts and that sim-only methods reject `tournament_id`.
"""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from simbot import Client

# ─── Routing: dual-mode methods route to /api/sim vs /api/trainer/tournament ──


@respx.mock
def test_perception_routes_to_sim_when_no_tournament_id(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/sim/perception").mock(
        return_value=Response(200, json={"active": False})
    )
    client.perception()
    assert route.called


@respx.mock
def test_perception_routes_to_trainer_when_tournament_id(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/trainer/tournament/rnd_abc/perception").mock(
        return_value=Response(200, json={"active": False})
    )
    client.perception(tournament_id="rnd_abc")
    assert route.called


@respx.mock
def test_full_state_routes_solo_and_tournament(client: Client, base_url: str) -> None:
    solo = respx.get(f"{base_url}/api/sim").mock(return_value=Response(200, json={"k": "solo"}))
    tour = respx.get(f"{base_url}/api/trainer/tournament/rnd_xyz").mock(
        return_value=Response(200, json={"k": "tournament"})
    )
    assert client.full_state()["k"] == "solo"
    assert client.full_state(tournament_id="rnd_xyz")["k"] == "tournament"
    assert solo.called and tour.called


@respx.mock
def test_events_routes_to_tournament_path(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/trainer/tournament/rnd_abc/events").mock(
        return_value=Response(200, json={"active": [], "upcoming": [], "past": []})
    )
    client.events(tournament_id="rnd_abc")
    assert route.called


@respx.mock
def test_paths_and_leaderboard_route_to_tournament(client: Client, base_url: str) -> None:
    paths = respx.get(f"{base_url}/api/trainer/tournament/rnd_abc/paths").mock(
        return_value=Response(200, json={})
    )
    lb = respx.get(f"{base_url}/api/trainer/tournament/rnd_abc/leaderboard").mock(
        return_value=Response(200, json=[])
    )
    client.paths(tournament_id="rnd_abc")
    client.leaderboard(tournament_id="rnd_abc", limit=50)
    assert paths.called and lb.called


# ─── Action routing: tournament has separate /purchase and /unlock endpoints ──


@respx.mock
def test_purchase_buy_solo_uses_action_discriminator(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/sim/purchase").mock(
        return_value=Response(200, json={"success": True})
    )
    client.purchase(investment_id=2, action="buy", quantity=3)
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"action":"buy"' in body
    assert '"quantity":3' in body


@respx.mock
def test_purchase_buy_tournament_uses_purchase_endpoint(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/purchase").mock(
        return_value=Response(200, json={"success": True})
    )
    client.purchase(investment_id=2, quantity=3, tournament_id="rnd_abc")
    assert route.called
    # Tournament purchase body should NOT include action discriminator
    body = route.calls.last.request.read().decode()
    assert '"investmentId":2' in body
    assert '"quantity":3' in body
    assert '"action"' not in body


@respx.mock
def test_purchase_unlock_tournament_uses_unlock_endpoint(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/unlock").mock(
        return_value=Response(200, json={"success": True})
    )
    client.purchase(investment_id=4, action="unlock", tournament_id="rnd_abc")
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"investmentId":4' in body
    assert '"quantity"' not in body


@respx.mock
def test_stake_solo_vs_tournament(client: Client, base_url: str) -> None:
    solo = respx.post(f"{base_url}/api/sim/stake").mock(
        return_value=Response(200, json={"success": True})
    )
    tour = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/stake").mock(
        return_value=Response(200, json={"success": True})
    )
    client.stake(amount=100.0)
    client.stake(amount=200.0, tournament_id="rnd_abc")
    assert solo.called and tour.called

    solo_body = solo.calls.last.request.read().decode()
    tour_body = tour.calls.last.request.read().decode()
    # Solo includes action discriminator, tournament does not
    assert '"action":"stake"' in solo_body
    assert '"action"' not in tour_body


@respx.mock
def test_unstake_tournament_uses_separate_endpoint(client: Client, base_url: str) -> None:
    # Solo unstake hits /api/sim/stake with action=unstake
    solo = respx.post(f"{base_url}/api/sim/stake").mock(
        return_value=Response(200, json={"success": True})
    )
    # Tournament unstake hits /api/trainer/tournament/{id}/unstake (separate URL)
    tour = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/unstake").mock(
        return_value=Response(200, json={"success": True})
    )
    client.unstake(amount=50.0)
    client.unstake(amount=75.0, tournament_id="rnd_abc")
    assert solo.called and tour.called

    solo_body = solo.calls.last.request.read().decode()
    assert '"action":"unstake"' in solo_body


@respx.mock
def test_fork_uses_bonusId_field_both_surfaces(client: Client, base_url: str) -> None:
    """Architecture decision Section 10: request body field names match across surfaces."""
    solo = respx.post(f"{base_url}/api/sim/fork").mock(
        return_value=Response(200, json={"success": True})
    )
    tour = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/fork").mock(
        return_value=Response(200, json={"success": True})
    )
    client.fork(path="PRODUCTION", bonus_id="PROD_EARNINGS_1")
    client.fork(path="STAKING", bonus_id="STAKE_RATE_1", tournament_id="rnd_abc")
    assert solo.called and tour.called

    solo_body = solo.calls.last.request.read().decode()
    tour_body = tour.calls.last.request.read().decode()
    assert '"bonusId":"PROD_EARNINGS_1"' in solo_body
    assert '"bonusId":"STAKE_RATE_1"' in tour_body


@respx.mock
def test_set_autostake_uses_enabled_field_both_surfaces(client: Client, base_url: str) -> None:
    """Architecture decision Section 10: request body field names match across surfaces."""
    solo = respx.post(f"{base_url}/api/sim/autostake").mock(
        return_value=Response(200, json={"success": True})
    )
    tour = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/autostake").mock(
        return_value=Response(200, json={"success": True})
    )
    client.set_autostake(True)
    client.set_autostake(False, tournament_id="rnd_abc")
    assert solo.called and tour.called

    solo_body = solo.calls.last.request.read().decode()
    tour_body = tour.calls.last.request.read().decode()
    assert '"enabled":true' in solo_body
    assert '"enabled":false' in tour_body


@respx.mock
def test_use_power_routes_correctly(client: Client, base_url: str) -> None:
    tour = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/power").mock(
        return_value=Response(200, json={"success": True})
    )
    client.use_power("BLACKOUT", target_user_id="usr_xyz", tournament_id="rnd_abc")
    assert tour.called
    body = tour.calls.last.request.read().decode()
    assert '"power":"BLACKOUT"' in body
    assert '"targetUserId":"usr_xyz"' in body


@respx.mock
def test_contribute_routes_correctly(client: Client, base_url: str) -> None:
    tour = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/contribute").mock(
        return_value=Response(200, json={"success": True})
    )
    client.contribute(amount=1000.0, tournament_id="rnd_abc")
    assert tour.called


@respx.mock
def test_dismiss_routes_correctly(client: Client, base_url: str) -> None:
    tour = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/dismiss").mock(
        return_value=Response(200, json={"success": True})
    )
    client.dismiss(tournament_id="rnd_abc")
    assert tour.called


# ─── Tournament-only endpoints ────────────────────────────────────────────


@respx.mock
def test_list_tournaments(client: Client, base_url: str) -> None:
    payload = [
        {"id": "rnd_a", "roundNumber": 1, "status": "PENDING", "entryMode": "BOT_ONLY"},
        {"id": "rnd_b", "roundNumber": 2, "status": "ACTIVE", "entryMode": "MIXED"},
    ]
    route = respx.get(f"{base_url}/api/trainer/tournament").mock(
        return_value=Response(200, json=payload)
    )
    result = client.list_tournaments()
    assert route.called
    assert len(result) == 2
    assert result[0]["entryMode"] == "BOT_ONLY"


@respx.mock
def test_list_tournaments_with_filters(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/trainer/tournament").mock(
        return_value=Response(200, json=[])
    )
    client.list_tournaments(entry_mode="BOT_ONLY", visibility="PUBLIC")
    assert route.called
    sent = route.calls.last.request
    assert "entryMode=BOT_ONLY" in str(sent.url)
    assert "visibility=PUBLIC" in str(sent.url)


@respx.mock
def test_tournament_state_alias(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/trainer/tournament/rnd_abc").mock(
        return_value=Response(200, json={"round": {"id": "rnd_abc"}})
    )
    state = client.tournament_state("rnd_abc")
    assert route.called
    assert state["round"]["id"] == "rnd_abc"


@respx.mock
def test_register_for_tournament(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/register").mock(
        return_value=Response(200, json={"success": True})
    )
    client.register_for_tournament(
        "rnd_abc", bot_name="GreedyBot", bot_author_url="https://github.com/me"
    )
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"botName":"GreedyBot"' in body
    assert '"botAuthorUrl":"https://github.com/me"' in body


@respx.mock
def test_register_for_tournament_with_invite_code(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/trainer/tournament/rnd_priv/register").mock(
        return_value=Response(200, json={"success": True})
    )
    client.register_for_tournament("rnd_priv", invite_code="abc123")
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"inviteCode":"abc123"' in body


# ─── Withdraw with refund ─────────────────────────────────────────────────


@respx.mock
def test_withdraw_from_tournament(client: Client, base_url: str) -> None:
    route = respx.delete(f"{base_url}/api/trainer/tournament/rnd_abc/register").mock(
        return_value=Response(200, json={"success": True, "refunded": 100})
    )
    result = client.withdraw_from_tournament("rnd_abc")
    assert route.called
    assert result["refunded"] == 100


# ─── Create tournament ────────────────────────────────────────────────────


@respx.mock
def test_create_tournament(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/trainer/tournament").mock(
        return_value=Response(
            200,
            json={
                "id": "rnd_new",
                "roundNumber": 42,
                "title": "Friday Bot Bash",
                "entryMode": "BOT_ONLY",
            },
        )
    )
    result = client.create_tournament(
        title="Friday Bot Bash",
        entry_mode="BOT_ONLY",
        creation_fee_gems=2500,
        description="Weekly bots-only bracket",
        total_blocks=40320,
        min_participants=20,
        entry_fee_gems=500,
        prize_distribution_json='{"1": 0.5, "2": 0.3, "3": 0.2}',
        platform_fee_bps=1000,
    )
    assert route.called
    assert result["id"] == "rnd_new"
    body = route.calls.last.request.read().decode()
    assert '"title":"Friday Bot Bash"' in body
    assert '"entryMode":"BOT_ONLY"' in body
    assert '"creationFeeGems":2500' in body
    assert '"creationFeeGold":0' in body
    assert '"entryFeeGems":500' in body
    assert '"platformFeeBps":1000' in body


@respx.mock
def test_create_tournament_with_gold_layer(client: Client, base_url: str) -> None:
    """Gold layer: creator-supplied creationFeeGold is forwarded as a top-level body field."""
    route = respx.post(f"{base_url}/api/trainer/tournament").mock(
        return_value=Response(200, json={"id": "rnd_gold"})
    )
    client.create_tournament(
        title="Gem + Gold Mix",
        entry_mode="MIXED",
        creation_fee_gems=500,
        creation_fee_gold=10000,
    )
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"creationFeeGems":500' in body
    assert '"creationFeeGold":10000' in body


def test_create_tournament_validates_min_creation_fee(client: Client) -> None:
    with pytest.raises(ValueError, match="creation_fee_gems must be at least 250"):
        client.create_tournament(
            title="Cheap", entry_mode="MIXED", creation_fee_gems=100
        )


def test_create_tournament_validates_creation_fee_gold(client: Client) -> None:
    with pytest.raises(ValueError, match="creation_fee_gold must be"):
        client.create_tournament(
            title="x",
            entry_mode="MIXED",
            creation_fee_gems=500,
            creation_fee_gold=-1,
        )


def test_create_tournament_validates_entry_mode(client: Client) -> None:
    with pytest.raises(ValueError, match="entry_mode must be"):
        client.create_tournament(
            title="x", entry_mode="INVALID_MODE", creation_fee_gems=2000
        )


def test_create_tournament_validates_visibility(client: Client) -> None:
    with pytest.raises(ValueError, match="visibility must be"):
        client.create_tournament(
            title="x",
            entry_mode="MIXED",
            creation_fee_gems=2000,
            visibility="HIDDEN",
        )


def test_create_tournament_validates_platform_fee(client: Client) -> None:
    with pytest.raises(ValueError, match="platform_fee_bps must be 0..5000"):
        client.create_tournament(
            title="x",
            entry_mode="MIXED",
            creation_fee_gems=2000,
            platform_fee_bps=6000,
        )


# ─── My tournaments ────────────────────────────────────────────────────────


@respx.mock
def test_my_tournaments(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/trainer/tournament/mine").mock(
        return_value=Response(200, json=[{"id": "rnd_a"}, {"id": "rnd_b"}])
    )
    result = client.my_tournaments()
    assert route.called
    assert len(result) == 2


# ─── Invites (PRIVATE tournaments) ─────────────────────────────────────────


@respx.mock
def test_list_tournament_invites(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/trainer/tournament/rnd_priv/invites").mock(
        return_value=Response(
            200, json=[{"invitedUserId": "u1"}, {"invitedUserId": "u2"}]
        )
    )
    result = client.list_tournament_invites("rnd_priv")
    assert route.called
    assert len(result) == 2


@respx.mock
def test_invite_to_tournament(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/trainer/tournament/rnd_priv/invites").mock(
        return_value=Response(200, json={"invitedUserId": "usr_alice"})
    )
    client.invite_to_tournament("rnd_priv", invited_user_id="usr_alice")
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"invitedUserId":"usr_alice"' in body


@respx.mock
def test_revoke_tournament_invite(client: Client, base_url: str) -> None:
    route = respx.delete(
        f"{base_url}/api/trainer/tournament/rnd_priv/invites/usr_alice"
    ).mock(return_value=Response(200, json={"revokedAt": "2026-05-06T00:00:00Z"}))
    client.revoke_tournament_invite("rnd_priv", invited_user_id="usr_alice")
    assert route.called


# ─── Sim-only methods reject tournament_id ────────────────────────────────


def test_profile_rejects_tournament_id(client: Client) -> None:
    with pytest.raises(ValueError, match="no tournament equivalent"):
        client.profile(tournament_id="rnd_abc")


def test_history_rejects_tournament_id(client: Client) -> None:
    with pytest.raises(ValueError, match="no tournament equivalent"):
        client.history(tournament_id="rnd_abc")


def test_cards_rejects_tournament_id(client: Client) -> None:
    with pytest.raises(ValueError, match="no tournament equivalent"):
        client.cards(tournament_id="rnd_abc")


def test_autostake_status_rejects_tournament_id(client: Client) -> None:
    with pytest.raises(ValueError, match="no tournament equivalent"):
        client.autostake_status(tournament_id="rnd_abc")


def test_power_info_rejects_tournament_id(client: Client) -> None:
    with pytest.raises(ValueError, match="no tournament equivalent"):
        client.power_info(tournament_id="rnd_abc")


# ─── Buy-keys works on both surfaces (dark-web is bot-eligible) ──────────


@respx.mock
def test_buy_keys_info_solo(client: Client, base_url: str) -> None:
    route = respx.get(f"{base_url}/api/sim/buy-keys").mock(
        return_value=Response(
            200,
            json={"currentKeys": 4, "gemsBalance": 1000, "gemsPerKey": 3, "options": []},
        )
    )
    result = client.buy_keys_info()
    assert route.called
    assert result["gemsBalance"] == 1000
    assert result["currentKeys"] == 4


@respx.mock
def test_buy_keys_info_tournament(client: Client, base_url: str) -> None:
    """Tournament buy-keys reads from the trainer surface and returns the
    round-scoped key balance."""
    route = respx.get(f"{base_url}/api/trainer/tournament/rnd_abc/buy-keys").mock(
        return_value=Response(
            200,
            json={"currentKeys": 2, "gemsBalance": 500, "gemsPerKey": 3, "options": []},
        )
    )
    result = client.buy_keys_info(tournament_id="rnd_abc")
    assert route.called
    assert result["gemsBalance"] == 500


@respx.mock
def test_buy_keys_solo(client: Client, base_url: str) -> None:
    route = respx.post(f"{base_url}/api/sim/buy-keys").mock(
        return_value=Response(200, json={"success": True, "keysAdded": 10})
    )
    client.buy_keys(quantity=10)
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"quantity":10' in body


@respx.mock
def test_buy_keys_tournament(client: Client, base_url: str) -> None:
    """Bots can now buy keys in tournament rounds — required for dark-web
    PvP gameplay (the trainer's HUMAN-only gate was lifted)."""
    route = respx.post(f"{base_url}/api/trainer/tournament/rnd_abc/buy-keys").mock(
        return_value=Response(200, json={"success": True, "keysAdded": 25})
    )
    client.buy_keys(quantity=25, tournament_id="rnd_abc")
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"quantity":25' in body


def test_buy_keys_validates_quantity_with_tournament_id(client: Client) -> None:
    """Quantity validation fires before any HTTP call on the tournament surface too."""
    with pytest.raises(ValueError, match="quantity must be one of"):
        client.buy_keys(quantity=7, tournament_id="rnd_abc")


# ─── Gem balance (account-level economy) ─────────────────────────────────


@respx.mock
def test_gems_balance(client: Client, base_url: str) -> None:
    """gems_balance() hits /api/gems/balance and returns the user-level
    gem balance + transaction history. Bots need this to plan key
    purchases, tournament entry fees, and creation fees."""
    route = respx.get(f"{base_url}/api/gems/balance").mock(
        return_value=Response(
            200,
            json={
                "gems": 5000,
                "transactions": [
                    {
                        "id": "gt_1",
                        "amount": -30,
                        "balance": 5000,
                        "type": "DARK_WEB",
                        "description": "purchased 10 encryption keys",
                        "createdAt": "2026-05-15T12:00:00Z",
                    }
                ],
            },
        )
    )
    result = client.gems_balance()
    assert route.called
    assert result["gems"] == 5000
    assert len(result["transactions"]) == 1
    assert result["transactions"][0]["type"] == "DARK_WEB"


def test_stream_rejects_tournament_id(client: Client) -> None:
    with (
        pytest.raises(ValueError, match="no tournament equivalent"),
        client.stream(tournament_id="rnd_abc"),
    ):
        pass
