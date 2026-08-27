from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import yaml

from hermes_discord_botrooms import cli
from hermes_discord_botrooms.bot_mode.config import PLUGIN_KEY
from hermes_discord_botrooms.compat import CompatibilityReport


def _setup_args(**overrides):
    values = {
        "room_id": "team-room",
        "display_name": "",
        "profiles": "planner,builder",
        "controller": "planner",
        "guild_id": "111",
        "channel_id": "222",
        "plugin_source": "example/hermes-discord-botrooms",
        "plugin_ref": "a" * 40,
        "skip_profile_install": False,
        "restart": True,
        "non_interactive": True,
        "yes": True,
        "dry_run": True,
        "json_output": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _make_profiles(root: Path, *names: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / "profiles" / name).mkdir(parents=True)


def test_setup_restarts_selected_gateways_by_default():
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)

    assert parser.parse_args(["setup"]).restart is True
    assert parser.parse_args(["setup", "--no-restart"]).restart is False


def _write_install(root: Path, profile: str, source: str, revision: str) -> None:
    home = root if profile == "default" else root / "profiles" / profile
    plugin = home / "plugins" / PLUGIN_KEY
    plugin.mkdir(parents=True, exist_ok=True)
    (home / "plugins" / ".install-metadata.json").write_text(
        json.dumps({PLUGIN_KEY: {"source": source, "revision": revision}}),
        encoding="utf-8",
    )
    config = (
        yaml.safe_load((home / "config.yaml").read_text())
        if (home / "config.yaml").exists()
        else {}
    )
    config.setdefault("plugins", {})["enabled"] = [PLUGIN_KEY]
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_setup_dry_run_is_non_mutating_and_uses_arbitrary_profiles(tmp_path: Path, monkeypatch):
    _make_profiles(tmp_path, "planner", "builder", "reviewer")
    captured = {}
    monkeypatch.setattr(cli, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "profile_has_discord_token", lambda *_args: True)
    monkeypatch.setattr(cli, "_legacy_mirror", lambda config, _room: config)
    monkeypatch.setattr(
        cli,
        "_run_commands",
        lambda _commands: (_ for _ in ()).throw(AssertionError("dry run executed a command")),
    )
    monkeypatch.setattr(cli, "_emit", lambda payload, **_kwargs: captured.update(payload))

    assert cli._setup(_setup_args()) == 0

    assert not (tmp_path / "config.yaml").exists()
    assert captured["status"] == "dry-run"
    assert [member["profile"] for member in captured["room"]["members"]] == [
        "planner",
        "builder",
    ]
    assert all("reviewer" not in command for command in captured["install_commands"])
    assert captured["restart_commands"] == [
        "hermes --profile planner gateway restart",
        "hermes --profile builder gateway restart",
    ]


def test_source_metadata_can_come_from_a_named_profile(tmp_path: Path):
    _make_profiles(tmp_path, "planner")
    _write_install(tmp_path, "planner", "example/private-repo", "b" * 40)

    assert cli._source_metadata(tmp_path, ["planner"]) == (
        "example/private-repo",
        "b" * 40,
    )


def test_source_metadata_prefers_the_current_administrative_profile(tmp_path: Path, monkeypatch):
    _make_profiles(tmp_path, "admin", "planner", "builder")
    _write_install(tmp_path, "admin", "example/admin-source", "e" * 40)
    monkeypatch.setattr(cli, "current_profile_name", lambda _root: "admin")

    assert cli._source_metadata(tmp_path, ["planner", "builder"]) == (
        "example/admin-source",
        "e" * 40,
    )


def test_matching_but_disabled_profile_is_reenabled(tmp_path: Path):
    _make_profiles(tmp_path, "planner")
    source = "example/private-repo"
    revision = "b" * 40
    _write_install(tmp_path, "planner", source, revision)
    config_path = tmp_path / "profiles" / "planner" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["plugins"]["enabled"] = []
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert cli._install_commands(tmp_path, ["planner"], source, revision) == [
        [
            "hermes",
            "--profile",
            "planner",
            "plugins",
            "enable",
            PLUGIN_KEY,
            "--no-allow-tool-override",
        ]
    ]


def test_credential_bearing_plugin_source_is_rejected_without_echo(
    tmp_path: Path, monkeypatch, capsys
):
    _make_profiles(tmp_path, "planner", "builder")
    monkeypatch.setattr(cli, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "profile_has_discord_token", lambda *_args: True)
    monkeypatch.setattr(cli, "_legacy_mirror", lambda config, _room: config)
    args = _setup_args(plugin_source="https://account:private-value@example.invalid/repo.git")

    assert cli.botrooms_command(argparse.Namespace(**vars(args), botrooms_action="setup")) == 1

    output = capsys.readouterr().out
    assert "private-value" not in output
    assert "credential-bearing plugin URLs are not allowed" in output


def test_legacy_room_mirror_is_removed_without_touching_other_rooms():
    config = {
        "bot_mode": {
            "rooms": [
                {"room_id": "team-room"},
                {"room_id": "keep-room"},
                "preserve-unknown-row",
            ]
        }
    }

    updated = cli._remove_legacy_room(config, "team-room")

    assert updated["bot_mode"]["rooms"] == [
        {"room_id": "keep-room"},
        "preserve-unknown-row",
    ]


def test_uninstall_uses_supported_disable_and_remove_commands(tmp_path: Path, monkeypatch):
    _make_profiles(tmp_path, "planner", "builder")
    room = {
        "room_id": "team-room",
        "display_name": "Team Room",
        "platform": "discord",
        "guild_id": "111",
        "channel_id": "222",
        "controller_profile": "planner",
        "members": [{"profile": "planner"}, {"profile": "builder"}],
        "enabled": True,
    }
    config = cli.with_room({}, room)
    config["bot_mode"] = {"rooms": [room, {**room, "room_id": "keep-room", "channel_id": "999"}]}
    cli.write_config(config, tmp_path)
    for profile in ("planner", "builder"):
        _write_install(tmp_path, profile, "example/private-repo", "c" * 40)
    commands = []
    monkeypatch.setattr(cli, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "_run_commands",
        lambda rows: commands.extend(rows) or [" ".join(row) for row in rows],
    )
    monkeypatch.setattr(cli, "_emit", lambda *_args, **_kwargs: None)

    args = argparse.Namespace(
        purge_state=False,
        restart=False,
        yes=True,
        dry_run=False,
        json_output=True,
    )
    assert cli._uninstall(args) == 0

    assert all("--yes" not in command for command in commands)
    assert commands == [
        ["hermes", "--profile", "builder", "plugins", "disable", PLUGIN_KEY],
        ["hermes", "--profile", "builder", "plugins", "remove", PLUGIN_KEY],
        ["hermes", "--profile", "planner", "plugins", "disable", PLUGIN_KEY],
        ["hermes", "--profile", "planner", "plugins", "remove", PLUGIN_KEY],
    ]
    assert cli.configured_rooms(cli.read_config(tmp_path)) == []
    assert cli.read_config(tmp_path)["bot_mode"]["rooms"] == [
        {**room, "room_id": "keep-room", "channel_id": "999"}
    ]


def test_live_probe_never_returns_token_or_exception_text(tmp_path: Path, monkeypatch):
    from hermes_discord_botrooms.bot_mode import discord_transport

    canary = "redaction-canary-value-that-must-stay-private"
    monkeypatch.setattr(discord_transport, "_profile_token", lambda *_args: canary)

    def fail_proxy(*_args):
        raise RuntimeError(f"proxy failed with {canary}")

    monkeypatch.setattr(discord_transport, "_profile_proxy", fail_proxy)

    result = asyncio.run(
        cli._discord_live_profile_check(
            tmp_path,
            "planner",
            guild_id="111",
            channel_id="222",
        )
    )

    assert canary not in json.dumps(result)
    assert result["error"] == "Discord live check failed (RuntimeError)"


def test_live_doctor_rejects_profiles_sharing_one_bot_identity(tmp_path: Path, monkeypatch):
    _make_profiles(tmp_path, "planner", "builder")
    room = {
        "room_id": "team-room",
        "display_name": "Team Room",
        "platform": "discord",
        "guild_id": "111",
        "channel_id": "222",
        "controller_profile": "planner",
        "members": [{"profile": "planner"}, {"profile": "builder"}],
        "enabled": True,
    }
    cli.write_config(cli.with_room({}, room), tmp_path)
    for profile in ("planner", "builder"):
        _write_install(tmp_path, profile, "example/private-repo", "d" * 40)

    async def same_identity(*_args, **_kwargs):
        return {
            "identity_verified": True,
            "channel_accessible": True,
            "bot_user_id": "shared-bot-id",
            "bot_username": "same-bot",
        }

    captured = {}
    monkeypatch.setattr(cli, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "profile_has_discord_token", lambda *_args: True)
    monkeypatch.setattr(cli, "_discord_live_profile_check", same_identity)
    monkeypatch.setattr(
        cli,
        "check_compatibility",
        lambda: CompatibilityReport(True, "generic-session-hooks", "test"),
    )
    monkeypatch.setattr(cli, "_emit", lambda payload, **_kwargs: captured.update(payload))

    result = cli._doctor(argparse.Namespace(pre_install=False, live=True, json_output=True))

    assert result == 1
    assert any("is shared by profiles" in problem for problem in captured["problems"])
