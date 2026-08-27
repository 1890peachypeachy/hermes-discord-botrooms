"""Headless Bot Mode rooms shared by Desktop and messaging adapters.

The package intentionally has no Discord or renderer dependency.  Frontends
append user events and subscribe to canonical room events; the room engine
owns scheduling, persistent member sessions, watermarks, and stop rules.
"""

from .config import BotRoomConfig, BotRoomMember, load_bot_room_registry
from .service import BotRoomService, get_bot_room_service

__all__ = [
    "BotRoomConfig",
    "BotRoomMember",
    "BotRoomService",
    "get_bot_room_service",
    "load_bot_room_registry",
]
