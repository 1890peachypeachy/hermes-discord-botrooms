"""Operator and agent-friendly CLI for Hermes Discord Bot Rooms."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .bot_mode.config import (
    PLUGIN_KEY,
    bot_room_from_mapping,
    current_profile_name,
    profile_home,
)
from .bot_mode.service import get_bot_room_service
from .compat import check_compatibility
from .configuration import (
    available_profiles,
    configured_rooms,
    hermes_root,
    parse_profiles,
    profile_has_discord_token,
    read_config,
    with_room,
    without_room,
    write_config,
)

_ROOM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CREDENTIAL_URL_RE = re.compile(r"((?:https?|ssh)://)[^/\s@]+@", re.IGNORECASE)


def register_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="botrooms_action")

    setup = sub.add_parser("setup", aliases=["add"], help="Create or update a room")
    setup.add_argument("--room-id", default="")
    setup.add_argument("--display-name", default="")
    setup.add_argument("--profiles", default="")
    setup.add_argument("--controller", default="")
    setup.add_argument("--guild-id", default="")
    setup.add_argument("--channel-id", default="")
    setup.add_argument("--plugin-source", default="")
    setup.add_argument("--plugin-ref", default="")
    setup.add_argument("--skip-profile-install", action="store_true")
    restart = setup.add_mutually_exclusive_group()
    restart.add_argument(
        "--restart",
        action="store_true",
        dest="restart",
        help="Restart selected gateways after setup (default)",
    )
    restart.add_argument(
        "--no-restart",
        action="store_false",
        dest="restart",
        help="Leave selected gateways stopped or stale; intended for development",
    )
    setup.set_defaults(restart=True)
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument("--yes", action="store_true")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--json", action="store_true", dest="json_output")

    doctor = sub.add_parser("doctor", help="Check compatibility and room health")
    doctor.add_argument("--pre-install", action="store_true")
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--json", action="store_true", dest="json_output")

    listing = sub.add_parser("list", aliases=["ls"], help="List configured rooms")
    listing.add_argument("--json", action="store_true", dest="json_output")

    status = sub.add_parser("status", help="Show persisted room run state")
    status.add_argument("room_id", nargs="?", default="")
    status.add_argument("--json", action="store_true", dest="json_output")

    remove = sub.add_parser("remove", help="Remove one room configuration")
    remove.add_argument("room_id")
    remove.add_argument("--yes", action="store_true")
    remove.add_argument("--dry-run", action="store_true")
    remove.add_argument("--json", action="store_true", dest="json_output")

    uninstall = sub.add_parser("uninstall", help="Remove every room and profile copy")
    uninstall.add_argument("--purge-state", action="store_true")
    uninstall.add_argument("--restart", action="store_true")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--json", action="store_true", dest="json_output")

    parser.set_defaults(func=botrooms_command)


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    title = payload.get("summary") or payload.get("status") or "Bot Rooms"
    print(title)
    for item in payload.get("details") or []:
        print(f"  {item}")
    for problem in payload.get("problems") or []:
        print(f"  ERROR: {problem}")


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _confirm(message: str) -> bool:
    return input(f"{message} [y/N]: ").strip().lower() in {"y", "yes"}


def _profile_metadata(root: Path, profile: str) -> tuple[str, str]:
    path = profile_home(root, profile) / "plugins" / ".install-metadata.json"
    if not path.exists():
        return "", ""
    try:
        row = (json.loads(path.read_text(encoding="utf-8")) or {}).get(PLUGIN_KEY) or {}
        return str(row.get("source") or ""), str(row.get("revision") or "")
    except Exception:
        return "", ""


def _source_metadata(root: Path, profiles: list[str]) -> tuple[str, str]:
    """Find this plugin's pinned origin without assuming which profile owns it."""

    candidates = [current_profile_name(root), *profiles, "default", *available_profiles(root)]
    for profile in dict.fromkeys(candidates):
        source, revision = _profile_metadata(root, profile)
        if source and revision:
            return source.strip(), revision.strip()
    return "", ""


def _profile_plugin_enabled(root: Path, profile: str) -> bool:
    try:
        config = read_config(profile_home(root, profile))
        plugins = config.get("plugins") or {}
        if not isinstance(plugins, dict):
            return False
        enabled = plugins.get("enabled") or []
        disabled = plugins.get("disabled") or []
        return PLUGIN_KEY in enabled and PLUGIN_KEY not in disabled
    except Exception:
        return False


def _redact_text(value: str) -> str:
    return _CREDENTIAL_URL_RE.sub(r"\1<credentials-redacted>@", str(value))


def _validated_plugin_source(value: str) -> str:
    source = str(value or "").strip()
    if _CREDENTIAL_URL_RE.search(source):
        raise ValueError(
            "credential-bearing plugin URLs are not allowed; use an SSH URL or Git credential helper"
        )
    return source


def _display_command(command: list[str]) -> str:
    return " ".join(_redact_text(part) for part in command)


async def _discord_live_profile_check(
    root: Path,
    profile: str,
    *,
    guild_id: str,
    channel_id: str,
) -> dict[str, Any]:
    """Validate one bot identity and read access without sending a message."""

    import aiohttp
    from gateway.platforms.base import proxy_kwargs_for_aiohttp

    from .bot_mode.discord_transport import DISCORD_API, _profile_proxy, _profile_token

    token = _profile_token(root, profile)
    result: dict[str, Any] = {
        "identity_verified": False,
        "channel_accessible": False,
    }
    if not token:
        result["error"] = "Discord token is not configured"
        return result

    try:
        proxy_url = _profile_proxy(root, profile)
        session_kwargs, request_kwargs = proxy_kwargs_for_aiohttp(proxy_url)
        headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "HermesDiscordBotRooms/0.1",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=headers, **session_kwargs
        ) as session:
            async with session.get(f"{DISCORD_API}/users/@me", **request_kwargs) as response:
                if response.status != 200:
                    result["error"] = f"Discord identity check returned HTTP {response.status}"
                    return result
                identity = await response.json(content_type=None)
                if not isinstance(identity, dict):
                    result["error"] = "Discord identity response was malformed"
                    return result
            result["identity_verified"] = True
            result["bot_user_id"] = str(identity.get("id") or "")
            result["bot_username"] = str(identity.get("username") or "")

            async with session.get(
                f"{DISCORD_API}/channels/{channel_id}", **request_kwargs
            ) as response:
                if response.status != 200:
                    result["error"] = f"Discord channel check returned HTTP {response.status}"
                    return result
                channel = await response.json(content_type=None)
                if not isinstance(channel, dict):
                    result["error"] = "Discord channel response was malformed"
                    return result
            returned_guild = str(channel.get("guild_id") or "")
            returned_channel = str(channel.get("id") or "")
            if returned_channel != channel_id or (
                guild_id and returned_guild and returned_guild != guild_id
            ):
                result["error"] = "Discord returned a different channel or server"
                return result
            result["channel_accessible"] = True
            result["channel_name"] = str(channel.get("name") or "")
            return result
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        result["error"] = f"Discord network check failed ({type(exc).__name__})"
        return result
    except Exception as exc:
        result["error"] = f"Discord live check failed ({type(exc).__name__})"
        return result


def _install_commands(
    root: Path,
    profiles: list[str],
    source: str,
    revision: str,
) -> list[list[str]]:
    commands: list[list[str]] = []
    for profile in profiles:
        installed_source, installed_revision = _profile_metadata(root, profile)
        plugin_path = profile_home(root, profile) / "plugins" / PLUGIN_KEY
        if installed_source == source and installed_revision == revision and plugin_path.is_dir():
            if _profile_plugin_enabled(root, profile):
                continue
            command = ["hermes"]
            if profile != "default":
                command.extend(["--profile", profile])
            command.extend(["plugins", "enable", PLUGIN_KEY, "--no-allow-tool-override"])
            commands.append(command)
            continue
        command = ["hermes"]
        if profile != "default":
            command.extend(["--profile", profile])
        command.extend(
            [
                "plugins",
                "install",
                source,
                "--enable",
                "--ref",
                revision,
                "--force",
            ]
        )
        commands.append(command)
    return commands


def _restart_commands(profiles: list[str]) -> list[list[str]]:
    result: list[list[str]] = []
    for profile in profiles:
        command = ["hermes"]
        if profile != "default":
            command.extend(["--profile", profile])
        command.extend(["gateway", "restart"])
        result.append(command)
    return result


def _run_commands(commands: list[list[str]]) -> list[str]:
    completed: list[str] = []
    for command in commands:
        result = subprocess.run(command, text=True, capture_output=True, timeout=180)
        if result.returncode != 0:
            detail = _redact_text((result.stderr or result.stdout or "command failed").strip())
            raise RuntimeError(f"{_display_command(command)}: {detail[-1200:]}")
        completed.append(_display_command(command))
    return completed


def _legacy_mirror(config: dict[str, Any], room: dict[str, Any]) -> dict[str, Any]:
    """Mirror rooms only for the already-deployed in-core compatibility bridge."""

    if check_compatibility().mode != "legacy-bot-room-hooks":
        return config
    legacy = config.setdefault("bot_mode", {})
    legacy["room_engine"] = "headless"
    rows = [
        row
        for row in (legacy.get("rooms") or [])
        if isinstance(row, dict) and str(row.get("room_id") or "") != room["room_id"]
    ]
    rows.append(room)
    legacy["rooms"] = rows
    return config


def _remove_legacy_room(config: dict[str, Any], room_id: str) -> dict[str, Any]:
    """Remove only the matching room from the original in-core mirror."""

    legacy = config.get("bot_mode")
    if not isinstance(legacy, dict):
        return config
    rows = legacy.get("rooms")
    if not isinstance(rows, list):
        return config
    legacy["rooms"] = [
        row for row in rows if not isinstance(row, dict) or str(row.get("room_id") or "") != room_id
    ]
    return config


def _setup(args: argparse.Namespace) -> int:
    root = hermes_root()
    discovered = available_profiles(root)
    interactive = not args.non_interactive

    profiles = parse_profiles(args.profiles)
    if not profiles and interactive:
        print("Available Hermes profiles:")
        for name in discovered:
            credential = (
                "Discord configured"
                if profile_has_discord_token(root, name)
                else "no Discord token"
            )
            print(f"  - {name} ({credential})")
        profiles = parse_profiles(_prompt("Profiles (comma-separated)"))
    if not 2 <= len(profiles) <= 6:
        raise ValueError("choose between 2 and 6 distinct Hermes profiles")
    missing = [name for name in profiles if name not in discovered]
    if missing:
        raise ValueError("unknown Hermes profiles: " + ", ".join(missing))
    missing_tokens = [name for name in profiles if not profile_has_discord_token(root, name)]
    if missing_tokens:
        raise ValueError(
            "Discord is not configured for: "
            + ", ".join(missing_tokens)
            + ". Run `hermes --profile <name> setup` without pasting tokens into chat."
        )

    controller = str(args.controller or "").strip().lower()
    if not controller and interactive:
        controller = _prompt("Controller profile", profiles[0]).lower()
    if controller not in profiles:
        raise ValueError("controller must be one of the selected profiles")
    guild_id = str(args.guild_id or "").strip()
    channel_id = str(args.channel_id or "").strip()
    room_id = str(args.room_id or "").strip().lower()
    if interactive:
        room_id = room_id or _prompt("Room ID", "agents").lower()
        guild_id = guild_id or _prompt("Discord server ID")
        channel_id = channel_id or _prompt("Discord channel ID")
    if not _ROOM_ID_RE.fullmatch(room_id):
        raise ValueError("room ID must use lowercase letters, numbers, hyphens, or underscores")
    if not guild_id.isdigit() or not channel_id.isdigit():
        raise ValueError("guild and channel IDs must be numeric Discord IDs")

    room_row = {
        "room_id": room_id,
        "display_name": str(args.display_name or room_id).strip(),
        "platform": "discord",
        "guild_id": guild_id,
        "channel_id": channel_id,
        "controller_profile": controller,
        "members": [{"profile": profile} for profile in profiles],
        "enabled": True,
    }
    room = bot_room_from_mapping(room_row)
    source, revision = _source_metadata(root, profiles)
    source = _validated_plugin_source(args.plugin_source or source)
    revision = str(args.plugin_ref or revision).strip().lower()
    if not args.skip_profile_install:
        if not source:
            raise ValueError(
                "plugin source metadata is unavailable; supply --plugin-source or "
                "use --skip-profile-install for a development checkout"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("profile installation requires a full 40-character --plugin-ref")
    installs = (
        [] if args.skip_profile_install else _install_commands(root, profiles, source, revision)
    )
    restarts = _restart_commands(profiles) if args.restart else []
    updated = with_room(read_config(root), room)
    updated = _legacy_mirror(updated, room_row)
    preview = {
        "status": "dry-run" if args.dry_run else "ready",
        "summary": f"Bot Room {room_id!r} with {len(profiles)} profiles",
        "room": room_row,
        "config_path": str(root / "config.yaml"),
        "install_commands": [_display_command(command) for command in installs],
        "restart_commands": [_display_command(command) for command in restarts],
        "details": [
            f"profiles: {', '.join(profiles)}",
            f"controller: {controller}",
            f"channel: {channel_id}",
        ],
    }
    if args.dry_run:
        _emit(preview, json_output=args.json_output)
        return 0
    if interactive and not args.yes and not _confirm("Apply these changes?"):
        _emit({**preview, "status": "cancelled"}, json_output=args.json_output)
        return 1
    if not interactive and not args.yes:
        raise ValueError("non-interactive setup requires --yes or --dry-run")

    completed = _run_commands(installs)
    path = write_config(updated, root)
    completed.extend(_run_commands(restarts))
    _emit(
        {
            **preview,
            "status": "configured",
            "summary": f"Configured Bot Room {room_id!r}",
            "written": str(path),
            "completed_commands": completed,
        },
        json_output=args.json_output,
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    root = hermes_root()
    compatibility = check_compatibility()
    problems = list(compatibility.problems)
    checks: list[dict[str, Any]] = []
    rooms = [] if args.pre_install else configured_rooms(read_config(root))
    discovered = set(available_profiles(root))
    channel_claims: dict[str, list[str]] = {}
    for row in rooms:
        if (
            bool(row.get("enabled", True))
            and str(row.get("platform") or "").strip().lower() == "discord"
            and (channel_id := str(row.get("channel_id") or "").strip())
        ):
            channel_claims.setdefault(channel_id, []).append(str(row.get("room_id") or "<unnamed>"))
    for channel_id, room_ids in channel_claims.items():
        if len(room_ids) > 1:
            problems.append(
                f"Discord channel {channel_id!r} is claimed by multiple rooms: "
                + ", ".join(room_ids)
            )
    for row in rooms:
        try:
            room = bot_room_from_mapping(row)
        except Exception as exc:
            problems.append(f"invalid room configuration: {exc}")
            continue
        room_check = {"room_id": room.room_id, "profiles": []}
        revisions: set[tuple[str, str]] = set()
        bot_id_profiles: dict[str, list[str]] = {}
        for member in room.members:
            exists = member.profile in discovered
            has_token = exists and profile_has_discord_token(root, member.profile)
            plugin_path = profile_home(root, member.profile) / "plugins" / PLUGIN_KEY
            installed = plugin_path.is_dir()
            source, revision = _profile_metadata(root, member.profile)
            enabled = _profile_plugin_enabled(root, member.profile)
            item = {
                "profile": member.profile,
                "exists": exists,
                "discord_configured": has_token,
                "plugin_installed": installed,
                "plugin_enabled": enabled,
                "plugin_source": _redact_text(source),
                "plugin_revision": revision,
            }
            if args.live and exists and has_token:
                item["discord"] = asyncio.run(
                    _discord_live_profile_check(
                        root,
                        member.profile,
                        guild_id=room.guild_id,
                        channel_id=room.channel_id,
                    )
                )
            room_check["profiles"].append(item)
            if not exists:
                problems.append(f"room {room.room_id}: profile {member.profile!r} does not exist")
            elif not has_token:
                problems.append(
                    f"room {room.room_id}: profile {member.profile!r} has no Discord token"
                )
            if not installed:
                problems.append(
                    f"room {room.room_id}: plugin is not installed for {member.profile!r}"
                )
            elif not enabled:
                problems.append(
                    f"room {room.room_id}: plugin is not enabled for {member.profile!r}"
                )
            if source and revision:
                revisions.add((source, revision))
            elif installed:
                problems.append(
                    f"room {room.room_id}: plugin source metadata is missing for {member.profile!r}"
                )
            live = item.get("discord") or {}
            bot_user_id = str(live.get("bot_user_id") or "")
            if bot_user_id:
                bot_id_profiles.setdefault(bot_user_id, []).append(member.profile)
            if args.live and not live.get("identity_verified"):
                problems.append(
                    f"room {room.room_id}: Discord identity check failed for {member.profile!r}"
                )
            elif args.live and not live.get("channel_accessible"):
                problems.append(
                    f"room {room.room_id}: Discord channel is inaccessible to {member.profile!r}"
                )
        if len(revisions) > 1:
            problems.append(
                f"room {room.room_id}: selected profiles have different plugin sources or revisions"
            )
        for bot_user_id, profiles in bot_id_profiles.items():
            if len(profiles) > 1:
                problems.append(
                    f"room {room.room_id}: Discord bot user {bot_user_id} is shared by profiles "
                    + ", ".join(profiles)
                )
        checks.append(room_check)
    payload = {
        "status": "ok" if not problems else "error",
        "summary": "Bot Rooms doctor passed" if not problems else "Bot Rooms doctor found problems",
        "compatibility": compatibility.to_dict(),
        "root": str(root),
        "profiles": available_profiles(root),
        "rooms": checks,
        "problems": problems,
    }
    _emit(payload, json_output=args.json_output)
    return 0 if not problems else 1


def _list(args: argparse.Namespace) -> int:
    rows = configured_rooms(read_config())
    details = [
        f"{row.get('room_id')}: {', '.join(str(m.get('profile')) for m in row.get('members') or [])}"
        for row in rows
    ]
    _emit(
        {
            "status": "ok",
            "summary": f"{len(rows)} configured Bot Room(s)",
            "rooms": rows,
            "details": details,
        },
        json_output=args.json_output,
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    service = get_bot_room_service(hermes_root())
    rooms = service.rooms()
    selected = [args.room_id] if args.room_id else sorted(rooms)
    result = {}
    problems = []
    for room_id in selected:
        if room_id not in rooms:
            problems.append(f"unknown room {room_id!r}")
            continue
        result[room_id] = service.status(room_id)
    _emit(
        {
            "status": "ok" if not problems else "error",
            "summary": "Bot Rooms status",
            "rooms": result,
            "problems": problems,
        },
        json_output=args.json_output,
    )
    return 0 if not problems else 1


def _remove(args: argparse.Namespace) -> int:
    root = hermes_root()
    config = read_config(root)
    rows = configured_rooms(config)
    if not any(str(row.get("room_id") or "") == args.room_id for row in rows):
        raise ValueError(f"unknown room {args.room_id!r}")
    payload = {
        "status": "dry-run" if args.dry_run else "removed",
        "summary": f"Remove Bot Room {args.room_id!r}",
        "config_path": str(root / "config.yaml"),
    }
    if args.dry_run:
        _emit(payload, json_output=args.json_output)
        return 0
    if not args.yes and not _confirm(f"Remove room {args.room_id!r}?"):
        return 1
    updated = without_room(config, args.room_id)
    updated = _remove_legacy_room(updated, args.room_id)
    write_config(updated, root)
    _emit(payload, json_output=args.json_output)
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    root = hermes_root()
    config = read_config(root)
    rows = configured_rooms(config)
    configured_profiles = {
        str(member.get("profile") or "")
        for row in rows
        for member in (row.get("members") or [])
        if member.get("profile")
    }
    installed_profiles = {
        profile
        for profile in available_profiles(root)
        if (profile_home(root, profile) / "plugins" / PLUGIN_KEY).is_dir()
    }
    profiles = sorted(configured_profiles | installed_profiles)
    data_dir = root / "plugin-data" / PLUGIN_KEY
    payload = {
        "status": "dry-run" if args.dry_run else "uninstalled",
        "summary": "Uninstall Hermes Discord Bot Rooms",
        "profiles": profiles,
        "purge_state": bool(args.purge_state),
        "restart": bool(args.restart),
        "state_path": str(data_dir),
    }
    if args.dry_run:
        _emit(payload, json_output=args.json_output)
        return 0
    if not args.yes and not _confirm("Remove Bot Rooms from all configured profiles?"):
        return 1
    updated = config
    for row in rows:
        room_id = str(row.get("room_id") or "")
        updated = without_room(updated, room_id)
        updated = _remove_legacy_room(updated, room_id)
    write_config(updated, root)
    commands = []
    for profile in profiles:
        if not (profile_home(root, profile) / "plugins" / PLUGIN_KEY).is_dir():
            continue
        prefix = ["hermes"]
        if profile != "default":
            prefix.extend(["--profile", profile])
        if _profile_plugin_enabled(root, profile):
            commands.append([*prefix, "plugins", "disable", PLUGIN_KEY])
        commands.append([*prefix, "plugins", "remove", PLUGIN_KEY])
    completed = _run_commands(commands)
    if args.restart:
        completed.extend(_run_commands(_restart_commands(profiles)))
    if args.purge_state and data_dir.is_dir():
        shutil.rmtree(data_dir)
    _emit({**payload, "completed_commands": completed}, json_output=args.json_output)
    return 0


def botrooms_command(args: argparse.Namespace) -> int:
    action = getattr(args, "botrooms_action", None)
    try:
        if action in {"setup", "add"}:
            return _setup(args)
        if action == "doctor":
            return _doctor(args)
        if action in {"list", "ls"}:
            return _list(args)
        if action == "status":
            return _status(args)
        if action == "remove":
            return _remove(args)
        if action == "uninstall":
            return _uninstall(args)
        print("Usage: hermes botrooms {setup|doctor|list|status|remove|uninstall}")
        return 2
    except (ValueError, RuntimeError) as exc:
        payload = {"status": "error", "summary": "Bot Rooms command failed", "problems": [str(exc)]}
        _emit(payload, json_output=bool(getattr(args, "json_output", False)))
        return 1
