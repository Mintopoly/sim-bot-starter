"""Children of Titan - Sim API Python client and reference bots.

Build a bot that competes on a real multiplayer leaderboard.

    from simbot import Client

    client = Client(api_key="sim_...")
    state = client.perception()
    print(state.player.cash, state.player.rank)
"""

from simbot.client import Client
from simbot.exceptions import (
    APIError,
    AuthError,
    BadRequestError,
    NoActiveRoundError,
    RateLimitError,
    SimBotError,
    SubscriptionRequiredError,
)
from simbot.types import (
    DarkWebPower,
    EconomyPreset,
    Event,
    ForkAnalysis,
    Investment,
    LeaderboardEntry,
    Path,
    PerceptionState,
    Player,
    PowerInfo,
    Round,
    SimEvent,
    StakingInfo,
    Target,
)

__version__ = "0.1.0"

__all__ = [
    "Client",
    # Exceptions
    "APIError",
    "AuthError",
    "BadRequestError",
    "NoActiveRoundError",
    "RateLimitError",
    "SimBotError",
    "SubscriptionRequiredError",
    # Types
    "DarkWebPower",
    "EconomyPreset",
    "Event",
    "ForkAnalysis",
    "Investment",
    "LeaderboardEntry",
    "Path",
    "PerceptionState",
    "Player",
    "PowerInfo",
    "Round",
    "SimEvent",
    "StakingInfo",
    "Target",
]
