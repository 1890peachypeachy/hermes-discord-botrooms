"""Installation-scoped configuration for headless Bot Mode rooms."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml
from hermes_constants import get_default_hermes_root, get_hermes_home

MAX_ROOM_MEMBERS = 6
MIN_ROOM_MEMBERS = 2
PLUGIN_KEY = "hermes-discord-botrooms"


def profile_home(root: Path, profile: str) -> Path:
    """Return the on-disk home for a local Hermes profile."""

    return root if profile == "default" else root / "profiles" / profile


def current_profile_name(root: Path | None = None) -> str:
    """Resolve the profile owning the current process without display aliases."""

    install_root = (root or get_default_hermes_root()).resolve(strict=False)
    home = get_hermes_home().resolve(strict=False)
    if home == install_root:
        return "default"
    if home.parent == install_root / "profiles":
        return home.name
    return "default"


def _mention_forms(value: str) -> set[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return set()
    slug = "-".join(part for part in re.split(r"[^a-z0-9_-]+", raw) if part)
    collapsed = re.sub(r"[^a-z0-9_-]+", "", raw)
    return {form for form in (slug, collapsed) if form}


@dataclass(frozen=True)
class BotRoomMember:
    profile: str
    display_name: str = ""
    handle: str = ""
    discord_bot_user_id: str = ""
    connection_id: str = ""
    connection_label: str = ""

    @property
    def key(self) -> str:
        return f"{self.connection_id}::{self.profile}" if self.connection_id else self.profile

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        return "Hermes" if self.profile == "default" else self.profile

    @property
    def mention_handle(self) -> str:
        return self.handle or ("hermes" if self.profile == "default" else self.profile)

    @property
    def mention_forms(self) -> set[str]:
        forms = {
            self.profile.lower(),
            re.sub(r"[\s_-]+", "", self.profile.lower()),
            self.mention_handle.lower(),
            re.sub(r"[\s_.-]+", "", self.mention_handle.lower()),
        }
        forms.update(_mention_forms(self.display_name))
        if self.display_name:
            forms.add(self.display_name.split()[0].lower())
        return {form for form in forms if form}


@dataclass(frozen=True)
class BotRoomConfig:
    room_id: str
    display_name: str
    platform: str
    channel_id: str
    members: tuple[BotRoomMember, ...]
    controller_profile: str = "default"
    guild_id: str = ""
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def member(self, profile_or_key: str) -> BotRoomMember | None:
        return next(
            (member for member in self.members if profile_or_key in {member.profile, member.key}),
            None,
        )


class BotRoomConfigError(ValueError):
    """Raised when an enabled room cannot be routed safely."""


def _member_from_raw(raw: Any) -> BotRoomMember:
    if isinstance(raw, str):
        profile = raw.strip()
        data: dict[str, Any] = {}
    elif isinstance(raw, dict):
        data = raw
        profile = str(data.get("profile") or data.get("name") or "").strip()
    else:
        raise BotRoomConfigError("room members must be profile names or mappings")
    if not profile:
        raise BotRoomConfigError("room member profile is required")
    return BotRoomMember(
        profile=profile,
        display_name=str(data.get("display_name") or data.get("title") or "").strip(),
        handle=str(data.get("handle") or "").strip().lstrip("@"),
        discord_bot_user_id=str(data.get("discord_bot_user_id") or "").strip(),
        connection_id=str(data.get("connection_id") or "").strip(),
        connection_label=str(data.get("connection_label") or "").strip(),
    )


def _room_from_raw(raw: Any) -> BotRoomConfig:
    if not isinstance(raw, dict):
        raise BotRoomConfigError("each Bot Rooms entry must be a mapping")
    room_id = str(raw.get("room_id") or "").strip()
    platform = str(raw.get("platform") or "").strip().lower()
    channel_id = str(raw.get("channel_id") or "").strip()
    controller = str(raw.get("controller_profile") or "default").strip()
    if not room_id:
        raise BotRoomConfigError("room_id is required")
    if platform not in {"desktop", "discord"}:
        raise BotRoomConfigError(f"room {room_id!r}: platform must be desktop or discord")
    if platform == "discord" and not channel_id:
        raise BotRoomConfigError(f"room {room_id!r}: channel_id is required for Discord")
    members = tuple(_member_from_raw(item) for item in (raw.get("members") or []))
    if not MIN_ROOM_MEMBERS <= len(members) <= MAX_ROOM_MEMBERS:
        raise BotRoomConfigError(
            f"room {room_id!r}: expected {MIN_ROOM_MEMBERS}-{MAX_ROOM_MEMBERS} members"
        )
    keys = [member.key.lower() for member in members]
    if len(keys) != len(set(keys)):
        raise BotRoomConfigError(f"room {room_id!r}: duplicate members are not allowed")
    if controller not in {member.profile for member in members}:
        raise BotRoomConfigError(
            f"room {room_id!r}: controller_profile must be one of the room members"
        )
    known = {
        "room_id",
        "display_name",
        "platform",
        "guild_id",
        "channel_id",
        "controller_profile",
        "members",
        "enabled",
    }
    return BotRoomConfig(
        room_id=room_id,
        display_name=str(raw.get("display_name") or room_id).strip(),
        platform=platform,
        guild_id=str(raw.get("guild_id") or "").strip(),
        channel_id=channel_id,
        controller_profile=controller,
        members=members,
        enabled=bool(raw.get("enabled", True)),
        extra={key: value for key, value in raw.items() if key not in known},
    )


def bot_room_from_mapping(raw: Any) -> BotRoomConfig:
    """Validate one programmatic/RPC room definition."""

    return _room_from_raw(raw)


def _read_root_config(root: Path) -> dict[str, Any]:
    path = root / "config.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        parsed = yaml.safe_load(stream) or {}
    return parsed if isinstance(parsed, dict) else {}


def _plugin_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Return standalone-plugin settings, with a read-only legacy fallback.

    The fallback lets an existing built-in Bot Mode installation adopt this
    plugin without rewriting its room registry during the first restart.
    Fresh installations always write the namespaced plugin settings.
    """

    plugins = config.get("plugins")
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get(PLUGIN_KEY) if isinstance(entries, dict) else None
    settings = entry.get("settings") if isinstance(entry, dict) else None
    if isinstance(settings, dict) and settings.get("rooms") is not None:
        return settings
    legacy = config.get("bot_mode") or {}
    return legacy if isinstance(legacy, dict) else {}


def headless_room_engine_enabled(root: Path | None = None) -> bool:
    config = _read_root_config(Path(root or get_default_hermes_root()))
    settings = _plugin_settings(config)
    if "enabled" in settings:
        return bool(settings.get("enabled"))
    # Compatibility with the original in-core implementation.
    mode = str(settings.get("room_engine") or "headless").strip().lower()
    return mode == "headless"


def load_bot_room_registry(root: Path | None = None) -> dict[str, BotRoomConfig]:
    """Load the shared room registry from the installation root config.

    Named profiles deliberately do not read their own copy here.  A single
    installation registry is what lets every profile gateway reserve the same
    Discord channels and prevents ordinary gateway replies from racing rooms.
    """

    install_root = Path(root or get_default_hermes_root())
    if not headless_room_engine_enabled(install_root):
        return {}
    raw = _plugin_settings(_read_root_config(install_root))
    room_rows: Iterable[Any] = raw.get("rooms") or [] if isinstance(raw, dict) else []
    registry: dict[str, BotRoomConfig] = {}
    discord_channels: dict[str, str] = {}
    for row in room_rows:
        room = _room_from_raw(row)
        if not room.enabled:
            continue
        if room.room_id in registry:
            raise BotRoomConfigError(f"duplicate room_id {room.room_id!r}")
        if room.platform == "discord":
            claimed_by = discord_channels.get(room.channel_id)
            if claimed_by is not None:
                raise BotRoomConfigError(
                    f"Discord channel {room.channel_id!r} is claimed by both "
                    f"{claimed_by!r} and {room.room_id!r}"
                )
            discord_channels[room.channel_id] = room.room_id
        registry[room.room_id] = room
    return registry


def discord_channel_is_configured(
    root: Path,
    *,
    channel_id: str,
    parent_channel_id: str = "",
    guild_id: str = "",
) -> bool:
    """Detect a raw Discord-room reservation even when validation fails.

    This deliberately checks only routing fields.  A bad roster must disable
    Bot Mode with an operator-visible error, not expose the channel to the
    ordinary single-profile gateway path and produce duplicate responses.
    """

    config = _read_root_config(Path(root))
    settings = _plugin_settings(config)
    mode = (
        str(settings.get("room_engine") or "headless").strip().lower()
        if isinstance(settings, dict)
        else "headless"
    )
    if settings.get("enabled") is False or mode != "headless":
        return False
    rows = settings.get("rooms") or [] if isinstance(settings, dict) else []
    candidates = {str(channel_id or ""), str(parent_channel_id or "")}
    for row in rows:
        if not isinstance(row, dict) or not bool(row.get("enabled", True)):
            continue
        if str(row.get("platform") or "").strip().lower() != "discord":
            continue
        if str(row.get("channel_id") or "").strip() not in candidates:
            continue
        configured_guild = str(row.get("guild_id") or "").strip()
        if configured_guild and configured_guild != str(guild_id or ""):
            continue
        return True
    return False


def room_for_discord_channel(
    registry: dict[str, BotRoomConfig],
    *,
    channel_id: str,
    parent_channel_id: str = "",
    guild_id: str = "",
) -> BotRoomConfig | None:
    """Resolve a configured parent channel or one of its real threads."""

    candidates = {str(channel_id or ""), str(parent_channel_id or "")}
    for room in registry.values():
        if room.platform != "discord" or room.channel_id not in candidates:
            continue
        if room.guild_id and room.guild_id != str(guild_id or ""):
            continue
        return room
    return None
