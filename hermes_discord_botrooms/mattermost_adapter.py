"""Mattermost adapter extension for standalone Hermes Bot Rooms.

Port of ``adapter.py`` (Discord) onto the core Mattermost adapter. The
controller profile's gateway intercepts ``posted`` websocket events in the
configured room channel, starts/resumes room threads (a top-level post IS the
thread root in Mattermost), and routes member replies through
``MattermostRoomTransport`` using each member's own profile token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from plugins.platforms.mattermost import adapter as base

from .bot_mode.config import (
    current_profile_name,
    load_bot_room_registry,
    room_for_mattermost_channel,
)
from .bot_mode.mattermost_transport import MattermostRoomTransport
from .bot_mode.models import RoomAttachment
from .bot_mode.service import get_bot_room_service

logger = logging.getLogger(__name__)
_ROOM_UNAVAILABLE = object()
_ROOM_COMMANDS = {"/room-status", "/stop"}


class BotRoomsMattermostAdapter(base.MattermostAdapter):
    """Reserve configured Mattermost channels for the room engine."""

    def __init__(self, config):
        super().__init__(config)
        self._botrooms_service = None
        self._botrooms_transport = None
        self._botrooms_subscription: str | None = None
        self._botrooms_recovery_task: asyncio.Task | None = None
        self._botrooms_registry_cache: dict = {}
        self._room_disconnecting = False
        self._member_user_ids: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        connected = await super().connect(is_reconnect=is_reconnect)
        if connected:
            self._ensure_botrooms_subscription()
        return connected

    async def disconnect(self) -> None:
        self._room_disconnecting = True
        if self._botrooms_subscription:
            with contextlib.suppress(Exception):
                self._botrooms_service.unsubscribe(self._botrooms_subscription)
        self._botrooms_subscription = None
        if self._botrooms_recovery_task:
            self._botrooms_recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._botrooms_recovery_task
        self._botrooms_recovery_task = None
        await super().disconnect()

    def _ensure_botrooms_subscription(self) -> None:
        if self._botrooms_subscription:
            return
        try:
            from hermes_constants import get_default_hermes_root

            root = get_default_hermes_root()
            profile = current_profile_name(root)
            rooms = load_bot_room_registry(root)
            if not any(
                room.platform == "mattermost" and room.controller_profile == profile
                for room in rooms.values()
            ):
                return
            service = get_bot_room_service(root)
            self._botrooms_service = service
            self._botrooms_transport = MattermostRoomTransport(root, service.store)
            self._botrooms_subscription = service.subscribe(
                self._on_botrooms_event,
                loop=asyncio.get_running_loop(),
            )
            self._botrooms_recovery_task = asyncio.create_task(
                self._recover_botrooms_deliveries()
            )
        except Exception:
            logger.exception("[%s] Failed to initialize Bot Rooms", self.name)

    async def _recover_botrooms_deliveries(self) -> None:
        service = self._botrooms_service
        transport = self._botrooms_transport
        if service is None or transport is None:
            return
        try:
            profile = current_profile_name(service.root)
            for room in service.rooms().values():
                if room.platform == "mattermost" and room.controller_profile == profile:
                    await transport.recover_room(room)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] Bot Rooms delivery recovery failed", self.name)

    # ------------------------------------------------------------------
    # Room resolution and intake
    # ------------------------------------------------------------------

    def _room_for_post(self, post: dict[str, Any]) -> Any:
        channel_id = str(post.get("channel_id") or "")
        if not channel_id:
            return None
        root_id = str(post.get("root_id") or "")
        try:
            from hermes_constants import get_default_hermes_root

            root = get_default_hermes_root()
            registry = load_bot_room_registry(root)
            self._botrooms_registry_cache = registry
            return room_for_mattermost_channel(
                registry,
                channel_id=channel_id,
                root_id=root_id,
            )
        except Exception:
            logger.exception("[%s] Failed to load Bot Rooms registry", self.name)
            return room_for_mattermost_channel(
                self._botrooms_registry_cache,
                channel_id=channel_id,
                root_id=root_id,
            )

    def _cache_member_user_ids(self, room) -> None:
        """Best-effort mapping of member profiles -> Mattermost user IDs."""
        transport = self._botrooms_transport
        if transport is None:
            return
        for member in room.members:
            if member.profile in self._member_user_ids:
                continue
            try:
                user_id = transport._user_ids.get(member.profile) or ""
            except Exception:
                user_id = ""
            if user_id:
                self._member_user_ids[member.profile] = user_id

    async def _handle_ws_event(self, event: dict[str, Any]) -> None:
        """Intercept room-channel posts; everything else goes to the base."""
        try:
            if await self._maybe_handle_room_post(event):
                return
        except Exception:
            logger.exception("[mattermost] Bot Rooms intake failed; deferring to base")
        await super()._handle_ws_event(event)

    async def _maybe_handle_room_post(self, event: dict[str, Any]) -> bool:
        if str(event.get("event") or "") != "posted":
            return False
        data = event.get("data") or {}
        raw_post = data.get("post")
        if not raw_post:
            return False
        try:
            post = json.loads(raw_post)
        except (json.JSONDecodeError, TypeError):
            return False
        if post.get("type"):  # system posts
            return False

        # Own messages are skipped by the base handler before us, but the room
        # path must also ignore replies we deliver as *member* identities.
        if post.get("user_id") == self._bot_user_id:
            return False

        room = self._room_for_post(post)
        if room is None:
            return False  # ordinary channel: normal gateway path

        # Only the controller's gateway runs the room engine.
        from hermes_constants import get_default_hermes_root

        if current_profile_name(get_default_hermes_root()) != room.controller_profile:
            return True

        service = self._botrooms_service
        if service is None:
            return True  # room exists but engine unavailable: stay silent

        self._ensure_botrooms_subscription()
        transport = self._botrooms_transport
        if transport is not None:
            self._member_user_ids = {}
            self._cache_member_user_ids(room)
            if self._member_user_ids:
                # Swallow the echo of member replies delivered under their own
                # Mattermost identities so the room never re-ingests itself.
                if str(post.get("user_id") or "") in set(self._member_user_ids.values()):
                    return True

        post_id = str(post.get("id") or "")
        event_uid = f"mattermost:{post_id}"
        if service.store.event_by_uid(event_uid) is not None:
            return True

        channel_id = str(post.get("channel_id") or "")
        # A top-level post IS the thread root in Mattermost.
        thread_id = str(post.get("root_id") or post_id)
        raw = str(post.get("message") or "").strip()

        lowered = raw.lower()
        if lowered in _ROOM_COMMANDS:
            if lowered == "/stop":
                result = await service.stop(room.room_id, thread_id)
                text = (
                    "Stop requested."
                    if result.get("stopped")
                    else "No room run is active."
                )
            else:
                text = self._format_room_status(
                    service.status(room.room_id, thread_id)
                )
            await self._room_control_message(thread_id, text)
            return True

        author_id = str(post.get("user_id") or "")
        allowed_user_ids = (room.extra or {}).get("allowed_user_ids") or []
        if allowed_user_ids and author_id not in {str(u) for u in allowed_user_ids}:
            return True

        try:
            attachments = await self._cache_room_attachments(post)
        except ValueError as exc:
            await self._room_control_message(thread_id, f":warning: {exc}")
            return True
        if not raw and not attachments:
            return True

        if transport is not None:
            mention_map = await transport.mention_map(room)
            for _mm_user_id, member in mention_map.items():
                raw = re.sub(
                    rf"@[a-zA-Z0-9.\-_]*{re.escape(member.mention_handle)}",
                    f"@{member.mention_handle}",
                    raw,
                    flags=re.IGNORECASE,
                )

        sender_name = str(data.get("sender_name") or "").lstrip("@") or author_id
        await service.submit(
            room_id=room.room_id,
            thread_id=thread_id,
            event_uid=event_uid,
            text=raw,
            author_id=author_id,
            author_name=sender_name,
            attachments=attachments,
            platform_message_id=post_id,
            channel_id=room.channel_id,
            metadata={"platform": "mattermost"},
        )
        return True

    async def _cache_room_attachments(self, post: dict[str, Any]) -> tuple[RoomAttachment, ...]:
        """Download post files into the shared attachment cache."""
        attachments: list[RoomAttachment] = []
        file_ids = post.get("file_ids") or []
        if not file_ids:
            return tuple(attachments)
        for fid in file_ids:
            try:
                file_info = await self._api_get(f"files/{fid}/info")
                fname = str(file_info.get("name") or f"file_{fid}")
                mime = str(file_info.get("mime_type") or "application/octet-stream")
                dl_url = f"{self._base_url}/api/v4/files/{fid}"
                import aiohttp

                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as session:
                    async with session.get(
                        dl_url,
                        headers={"Authorization": f"Bearer {self._token}"},
                    ) as resp:
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status}")
                        file_data = await resp.read()
                from gateway.platforms.base import (
                    cache_document_from_bytes,
                    cache_image_from_bytes,
                )

                suffix = Path(fname).suffix or ""
                if mime.startswith("image/"):
                    local_path = cache_image_from_bytes(file_data, suffix or ".png")
                else:
                    local_path = cache_document_from_bytes(file_data, fname)
                attachments.append(
                    RoomAttachment(
                        path=str(local_path),
                        name=fname,
                        kind="image" if mime.startswith("image/") else "file",
                        mime_type=mime,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "[mattermost] Bot Rooms attachment %s could not be cached: %s",
                    fid,
                    exc,
                )
        return tuple(attachments)

    # ------------------------------------------------------------------
    # Outbound (service events -> Mattermost posts)
    # ------------------------------------------------------------------

    async def _room_control_message(self, thread_id: str, text: str) -> None:
        transport = self._botrooms_transport
        if transport is None or not text:
            return
        try:
            from hermes_constants import get_default_hermes_root

            root = get_default_hermes_root()
            registry = load_bot_room_registry(root)
            profile = current_profile_name(root)
            channel_id = ""
            for room in registry.values():
                if room.platform == "mattermost" and room.controller_profile == profile:
                    channel_id = room.channel_id
                    break
            if not channel_id:
                return
            await transport.send_text(
                profile=profile,
                channel_id=channel_id,
                thread_id=str(thread_id or ""),
                text=text,
            )
        except Exception:
            logger.exception("[mattermost] Failed to send Bot Rooms control message")

    async def _on_botrooms_event(self, envelope: dict[str, Any]) -> None:
        if self._room_disconnecting:
            return
        room_id = str(envelope.get("room_id") or "")
        service = self._botrooms_service
        if not room_id or service is None:
            return
        try:
            room = service.room(room_id)
        except KeyError:
            return
        if room.platform != "mattermost":
            return

        kind = str(envelope.get("kind") or "")
        thread_id = str(envelope.get("thread_id") or "")
        member = room.member(str(envelope.get("member_key") or ""))
        transport = self._botrooms_transport
        if kind == "member.started":
            if member is not None and transport is not None:
                await transport.start_typing(
                    profile=member.profile,
                    thread_id=thread_id,
                )
            return
        if kind == "message":
            event = envelope.get("event")
            if event is None or member is None or transport is None:
                return
            try:
                await transport.deliver_event(room, event, member)
            except Exception as exc:
                await self._room_control_message(
                    thread_id,
                    f":warning: {member.label}'s reply could not be delivered: {exc}",
                )
            finally:
                await transport.stop_typing(
                    profile=member.profile,
                    thread_id=thread_id,
                )
            return
        if kind in {"prompt", "member.passed", "member.failed"}:
            if member is not None and transport is not None:
                await transport.stop_typing(
                    profile=member.profile,
                    thread_id=thread_id,
                )
        if kind == "prompt":
            payload = envelope.get("payload") or {}
            prompt_kind = str(envelope.get("prompt_kind") or "")
            if prompt_kind == "approval":
                detail = str(
                    payload.get("command") or payload.get("description") or "a protected action"
                )
                text = (
                    f":lock: **{member.label if member else 'An agent'} needs approval** "
                    f"for `{detail}`."
                )
            else:
                question = str(
                    payload.get("question")
                    or payload.get("prompt")
                    or payload.get("message")
                    or "needs clarification"
                )
                text = (
                    f":question: **{member.label if member else 'An agent'} asks:** "
                    f"{question}"
                )
            await self._room_control_message(thread_id, text)
            return
        if kind == "member.failed":
            await self._room_control_message(
                thread_id,
                f":warning: **{envelope.get('member_name') or 'An agent'}** could not "
                f"complete its turn: {envelope.get('error') or 'unknown error'}. "
                "The room continued to the next available agent.",
            )
            return
        if kind in {"run.finished", "run.superseded"} and transport is not None:
            await transport.stop_thread_typing(thread_id)

    @staticmethod
    def _format_room_status(status: dict[str, Any]) -> str:
        if not status:
            return "No room run is active."
        run_id = str(status.get("run_id") or "")
        state = str(status.get("state") or "unknown")
        turn = status.get("turn") or {}
        lines = [f"Room run `{run_id}` is **{state}**."]
        if turn:
            lines.append(
                f"Turn {turn.get('index', '?')}: {turn.get('member', '?')} "
                f"({turn.get('state', '?')})"
            )
        return "\n".join(lines)


def build_adapter(config):
    return BotRoomsMattermostAdapter(config)


def register_platform(ctx) -> None:
    ctx.register_platform(
        name="mattermost",
        label="Mattermost (Bot Rooms)",
        adapter_factory=build_adapter,
        check_fn=base.check_mattermost_requirements,
        is_connected=base._is_connected,
        required_env=["MATTERMOST_URL", "MATTERMOST_TOKEN"],
        install_hint="Run `hermes setup` to install Mattermost support.",
        setup_fn=base.interactive_setup,
        apply_yaml_config_fn=base._apply_yaml_config,
        standalone_sender_fn=base._standalone_send,
        max_message_length=3900,
        emoji="💬",
        allow_update_command=True,
    )
