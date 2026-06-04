import pytest

from gateway.config import Platform
from gateway.profile_routing import (
    ProfileRoute,
    ProfileRouteConfig,
    normalize_profile_routes_config,
    resolve_profile_for_source,
)
from gateway.session import SessionSource


def source(chat_id="-1001", thread_id=None):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        thread_id=thread_id,
        chat_type="forum" if thread_id else "group",
        user_id="u1",
    )


def test_exact_thread_route_wins_over_chat_route():
    cfg = ProfileRouteConfig(
        default_profile="default",
        routes=[
            ProfileRoute(id="chat", platform="telegram", chat_id="-1001", profile="family"),
            ProfileRoute(id="topic", platform="telegram", chat_id="-1001", thread_id="63", profile="planning"),
        ],
    )

    assert resolve_profile_for_source(cfg, source(thread_id="63")) == "planning"
    assert resolve_profile_for_source(cfg, source(thread_id="99")) == "family"


def test_disabled_route_is_ignored_and_default_is_used():
    cfg = ProfileRouteConfig(
        default_profile="default",
        routes=[
            ProfileRoute(
                id="disabled",
                enabled=False,
                platform="telegram",
                chat_id="-1001",
                thread_id="63",
                profile="planning",
            )
        ],
    )

    assert resolve_profile_for_source(cfg, source(thread_id="63")) == "default"


def test_normalize_rejects_bad_profile_names():
    raw = {
        "default_profile": "default",
        "routes": [{"id": "bad", "platform": "telegram", "chat_id": "-1001", "profile": "../escape"}],
    }

    with pytest.raises(ValueError, match="Invalid profile name"):
        normalize_profile_routes_config(raw)


def test_normalize_round_trips_minimal_yaml():
    cfg = normalize_profile_routes_config(
        {
            "default_profile": "default",
            "routes": [
                {
                    "id": "telegram-main",
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "profile": "planning",
                    "label": "Planning",
                }
            ],
        }
    )

    assert cfg.default_profile == "default"
    assert cfg.routes[0].id == "telegram-main"
    assert cfg.routes[0].profile == "planning"
    assert cfg.routes[0].label == "Planning"
