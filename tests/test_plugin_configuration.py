from pathlib import Path

import pytest
import yaml

from hermes_discord_botrooms.bot_mode.config import bot_room_from_mapping
from hermes_discord_botrooms.configuration import (
    available_profiles,
    configured_rooms,
    parse_profiles,
    read_config,
    with_room,
    without_room,
    write_config,
)


def _room():
    return bot_room_from_mapping(
        {
            "room_id": "review-room",
            "display_name": "Review Room",
            "platform": "discord",
            "guild_id": "100",
            "channel_id": "200",
            "controller_profile": "researcher",
            "members": ["researcher", "coder", "reviewer"],
        }
    )


def test_profiles_are_discovered_without_assuming_names(tmp_path: Path):
    (tmp_path / "profiles" / "researcher").mkdir(parents=True)
    (tmp_path / "profiles" / "coder").mkdir()
    (tmp_path / "profiles" / ".staging").mkdir()

    assert available_profiles(tmp_path) == ["default", "coder", "researcher"]
    assert parse_profiles("researcher, coder,reviewer,researcher") == [
        "researcher",
        "coder",
        "reviewer",
    ]


def test_room_config_is_namespaced_and_round_trips(tmp_path: Path):
    original = {"agent": {"model": "unchanged"}}
    updated = with_room(original, _room())

    assert original == {"agent": {"model": "unchanged"}}
    assert updated["agent"] == original["agent"]
    assert configured_rooms(updated)[0]["controller_profile"] == "researcher"
    assert "bot_mode" not in updated

    write_config(updated, tmp_path)
    loaded = read_config(tmp_path)
    assert configured_rooms(loaded) == configured_rooms(updated)

    removed = without_room(loaded, "review-room")
    assert configured_rooms(removed) == []


def test_two_rooms_cannot_claim_the_same_discord_channel():
    first = with_room({}, _room())
    second = {
        "room_id": "other-room",
        "display_name": "Other Room",
        "platform": "discord",
        "guild_id": "100",
        "channel_id": "200",
        "controller_profile": "researcher",
        "members": ["researcher", "coder"],
    }

    with pytest.raises(ValueError, match="already claimed"):
        with_room(first, second)


def test_atomic_writer_preserves_a_recoverable_backup(tmp_path: Path):
    old = {"discord": {"allowed_channels": ["123"]}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(old), encoding="utf-8")

    write_config(with_room(old, _room()), tmp_path)

    backup = yaml.safe_load((tmp_path / "config.yaml.botrooms-backup").read_text(encoding="utf-8"))
    assert backup == old
    assert configured_rooms(read_config(tmp_path))[0]["room_id"] == "review-room"


def test_room_state_uses_durable_plugin_data_directory(tmp_path: Path):
    from hermes_discord_botrooms.bot_mode.store import RoomStore

    store = RoomStore(tmp_path)
    assert store.path == (tmp_path / "plugin-data" / "hermes-discord-botrooms" / "rooms.db")
