"""Unit tests for Mattermost room resolution and registry acceptance."""

import pytest

from hermes_discord_botrooms.bot_mode.config import (
    bot_room_from_mapping,
    room_for_mattermost_channel,
)


def _room(**overrides):
    raw = {
        "room_id": "test-room",
        "platform": "mattermost",
        "channel_id": "chan123",
        "controller_profile": "peachy",
        "members": ["peachy", "pandy", "ollie"],
    }
    raw.update(overrides)
    return bot_room_from_mapping(raw)


def test_mattermost_platform_accepted():
    room = _room()
    assert room.platform == "mattermost"
    assert room.channel_id == "chan123"
    assert room.controller_profile == "peachy"
    assert len(room.members) == 3


def test_mattermost_channel_required():
    with pytest.raises(Exception):
        bot_room_from_mapping(
            {"room_id": "r2", "platform": "mattermost", "members": ["peachy", "pandy"]}
        )


def test_resolution_by_channel_and_root():
    registry = {"test-room": _room()}
    assert room_for_mattermost_channel(registry, channel_id="chan123") is not None
    # A thread root inside the channel resolves to the same room.
    assert (
        room_for_mattermost_channel(
            registry, channel_id="other_chan", root_id="chan123"
        ).room_id
        == "test-room"
    )
    assert room_for_mattermost_channel(registry, channel_id="nope") is None


def test_discord_registry_still_works():
    room = bot_room_from_mapping(
        {
            "room_id": "dRoom",
            "platform": "discord",
            "channel_id": "dchan1",
            "controller_profile": "peachy",
            "members": ["peachy", "pandy"],
        }
    )
    assert room.platform == "discord"
