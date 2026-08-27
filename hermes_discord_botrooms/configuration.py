"""Safe, installation-scoped configuration for the Bot Rooms CLI."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import yaml

from .bot_mode.config import (
    PLUGIN_KEY,
    BotRoomConfig,
    bot_room_from_mapping,
    profile_home,
)


def hermes_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        raw = os.environ.get("HERMES_HOME", "").strip()
        home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
        return home.parent.parent if home.parent.name == "profiles" else home


def available_profiles(root: Path | None = None) -> list[str]:
    base = Path(root or hermes_root())
    profiles = ["default"] if base.is_dir() else []
    named = base / "profiles"
    if named.is_dir():
        profiles.extend(
            child.name
            for child in sorted(named.iterdir())
            if child.is_dir() and not child.name.startswith(".")
        )
    return profiles


def profile_has_discord_token(root: Path, profile: str) -> bool:
    try:
        from agent.secret_scope import build_profile_secret_scope

        scope = build_profile_secret_scope(profile_home(root, profile))
        return bool(str(scope.get("DISCORD_BOT_TOKEN") or "").strip())
    except Exception:
        return False


def read_config(root: Path | None = None) -> dict[str, Any]:
    path = Path(root or hermes_root()) / "config.yaml"
    if not path.exists():
        return {}
    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return parsed


def configured_rooms(config: dict[str, Any]) -> list[dict[str, Any]]:
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return []
    entries = plugins.get("entries")
    if not isinstance(entries, dict):
        return []
    entry = entries.get(PLUGIN_KEY)
    if not isinstance(entry, dict):
        return []
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        return []
    rows = settings.get("rooms") or []
    return [copy.deepcopy(row) for row in rows if isinstance(row, dict)]


def _room_mapping(room: BotRoomConfig) -> dict[str, Any]:
    row = asdict(room)
    row.pop("extra", None)
    row["members"] = [
        {key: value for key, value in asdict(member).items() if value} for member in room.members
    ]
    return {key: value for key, value in row.items() if value not in ("", None)}


def with_room(config: dict[str, Any], room: BotRoomConfig | dict[str, Any]) -> dict[str, Any]:
    validated = room if isinstance(room, BotRoomConfig) else bot_room_from_mapping(room)
    updated = copy.deepcopy(config)
    plugins = updated.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    entry = entries.setdefault(PLUGIN_KEY, {})
    settings = entry.setdefault("settings", {})
    settings["enabled"] = True
    rooms = [
        row
        for row in configured_rooms(updated)
        if str(row.get("room_id") or "") != validated.room_id
    ]
    if validated.enabled and validated.platform == "discord":
        conflict = next(
            (
                row
                for row in rooms
                if bool(row.get("enabled", True))
                and str(row.get("platform") or "").strip().lower() == "discord"
                and str(row.get("channel_id") or "").strip() == validated.channel_id
            ),
            None,
        )
        if conflict is not None:
            raise ValueError(
                f"Discord channel {validated.channel_id!r} is already claimed by "
                f"room {str(conflict.get('room_id') or '')!r}"
            )
    rooms.append(_room_mapping(validated))
    settings["rooms"] = rooms
    return updated


def without_room(config: dict[str, Any], room_id: str) -> dict[str, Any]:
    updated = copy.deepcopy(config)
    plugins = updated.get("plugins")
    if not isinstance(plugins, dict):
        return updated
    entries = plugins.get("entries")
    if not isinstance(entries, dict):
        return updated
    entry = entries.get(PLUGIN_KEY)
    if not isinstance(entry, dict):
        return updated
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        return updated
    settings["rooms"] = [
        row for row in configured_rooms(updated) if str(row.get("room_id") or "") != room_id
    ]
    return updated


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_config(config: dict[str, Any], root: Path | None = None) -> Path:
    base = Path(root or hermes_root())
    base.mkdir(parents=True, exist_ok=True)
    path = base / "config.yaml"
    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    fd, tmp_name = tempfile.mkstemp(prefix="config.yaml.botrooms-", dir=base)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            backup = base / "config.yaml.botrooms-backup"
            _atomic_write_bytes(backup, path.read_bytes())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def parse_profiles(value: str | Iterable[str]) -> list[str]:
    raw = value.split(",") if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in raw:
        name = str(item).strip().lower()
        if name and name not in result:
            result.append(name)
    return result


def room_to_json(room: BotRoomConfig) -> str:
    return json.dumps(_room_mapping(room), indent=2, sort_keys=True)
