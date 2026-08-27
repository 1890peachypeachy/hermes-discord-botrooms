"""Discord adapter extension for standalone Hermes Bot Rooms."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from typing import Any

from plugins.platforms.discord import adapter as base

from .bot_mode.config import (
    current_profile_name,
    discord_channel_is_configured,
    load_bot_room_registry,
    room_for_discord_channel,
)
from .bot_mode.discord_transport import DiscordRoomTransport
from .bot_mode.models import RoomAttachment
from .bot_mode.service import get_bot_room_service

logger = logging.getLogger(__name__)
discord = base.discord
_ROOM_UNAVAILABLE = object()


class BotRoomsDiscordAdapter(base.DiscordAdapter):
    """Reserve configured channels and route them through the room engine."""

    def __init__(self, config):
        super().__init__(config)
        self._botrooms_service = None
        self._botrooms_transport = None
        self._botrooms_subscription: str | None = None
        self._botrooms_recovery_task: asyncio.Task | None = None
        self._botrooms_registry_cache = {}

    async def connect(self) -> bool:
        connected = await super().connect()
        if connected:
            self._ensure_botrooms_subscription()
        return connected

    def _room_for_discord_object(self, value: Any):
        channel_id = ""
        parent_id = ""
        guild_id = ""
        root = None
        try:
            from hermes_constants import get_default_hermes_root

            channel = getattr(value, "channel", None)
            channel_id = str(
                getattr(channel, "id", None) or getattr(value, "channel_id", None) or ""
            )
            if not channel_id:
                return None
            parent_id = self._get_parent_channel_id(channel) if channel is not None else ""
            guild = getattr(value, "guild", None)
            guild_id = str(getattr(guild, "id", None) or getattr(value, "guild_id", None) or "")
            root = get_default_hermes_root()
            registry = load_bot_room_registry(root)
            self._botrooms_registry_cache = registry
            return room_for_discord_channel(
                registry,
                channel_id=channel_id,
                parent_channel_id=str(parent_id or ""),
                guild_id=guild_id,
            )
        except Exception:
            logger.exception("[%s] Failed to load Bot Rooms registry", self.name)
            cached = room_for_discord_channel(
                self._botrooms_registry_cache,
                channel_id=channel_id,
                parent_channel_id=str(parent_id or ""),
                guild_id=guild_id,
            )
            if cached is not None:
                return _ROOM_UNAVAILABLE
            if root is not None:
                with contextlib.suppress(Exception):
                    if discord_channel_is_configured(
                        root,
                        channel_id=channel_id,
                        parent_channel_id=str(parent_id or ""),
                        guild_id=guild_id,
                    ):
                        return _ROOM_UNAVAILABLE
            return None

    def _ensure_botrooms_subscription(self) -> None:
        if self._botrooms_subscription:
            return
        try:
            from hermes_constants import get_default_hermes_root

            root = get_default_hermes_root()
            profile = current_profile_name(root)
            rooms = load_bot_room_registry(root)
            if not any(
                room.platform == "discord" and room.controller_profile == profile
                for room in rooms.values()
            ):
                return
            service = get_bot_room_service(root)
            self._botrooms_service = service
            self._botrooms_transport = DiscordRoomTransport(root, service.store)
            self._botrooms_subscription = service.subscribe(
                self._on_botrooms_event,
                loop=asyncio.get_running_loop(),
            )
            self._botrooms_recovery_task = asyncio.create_task(self._recover_botrooms_deliveries())
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
                if room.platform == "discord" and room.controller_profile == profile:
                    await transport.recover_room(room)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] Bot Rooms delivery recovery failed", self.name)

    async def _room_control_message(self, thread_id: str, text: str) -> None:
        if not self._client or not text:
            return
        try:
            channel = self._client.get_channel(int(thread_id))
            if channel is None:
                channel = await self._client.fetch_channel(int(thread_id))
            await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=False,
                    replied_user=False,
                ),
            )
        except Exception:
            logger.exception(
                "[%s] Failed to send Bot Rooms control message to %s",
                self.name,
                thread_id,
            )

    async def _on_botrooms_event(self, envelope: dict[str, Any]) -> None:
        if self._disconnecting:
            return
        room_id = str(envelope.get("room_id") or "")
        service = self._botrooms_service
        if not room_id or service is None:
            return
        try:
            room = service.room(room_id)
        except KeyError:
            return
        if room.platform != "discord":
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
                    f"⚠️ {member.label}'s reply could not be delivered: {exc}",
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
                    f"🔐 **{member.label if member else 'An agent'} needs approval** "
                    f"for `{detail}`. Use `/room-approval`."
                )
            else:
                question = str(
                    payload.get("question")
                    or payload.get("prompt")
                    or payload.get("message")
                    or "needs clarification"
                )
                text = (
                    f"❓ **{member.label if member else 'An agent'} asks:** "
                    f"{question}\nUse `/room-answer`."
                )
            await self._room_control_message(thread_id, text)
            return
        if kind == "member.failed":
            await self._room_control_message(
                thread_id,
                f"⚠️ **{envelope.get('member_name') or 'An agent'}** could not "
                f"complete its turn: {envelope.get('error') or 'unknown error'}. "
                "The room continued to the next available agent.",
            )
            return
        if kind in {"run.finished", "run.superseded"} and transport is not None:
            await transport.stop_thread_typing(thread_id)

    def _room_admission(self, message: Any, *, claim: bool) -> tuple[bool, bool]:
        message_id = str(getattr(message, "id", ""))
        if claim:
            if self._dedup.is_duplicate(message_id):
                return False, False
        elif self._dedup.contains(message_id):
            return False, False
        if message.author == self._client.user or getattr(message.author, "bot", False):
            return False, False
        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return False, False

        guild = getattr(message, "guild", None)
        is_dm = isinstance(message.channel, discord.DMChannel) or guild is None
        channel_ids = None
        if not is_dm:
            channel_ids = {str(message.channel.id)}
            parent_id = self._get_parent_channel_id(message.channel)
            if parent_id:
                channel_ids.add(parent_id)
        if not self._is_allowed_user(
            str(message.author.id),
            message.author,
            guild=guild,
            is_dm=is_dm,
            channel_ids=channel_ids,
        ):
            self._warn_if_fail_closed_default()
            return False, False
        return True, bool(getattr(self, "_allowed_role_ids", set()))

    async def _dispatch_discord_message(self, message: Any) -> bool:
        if not self._ready_event.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._ready_event.wait(), timeout=30.0)
        room = self._room_for_discord_object(message)
        if room is _ROOM_UNAVAILABLE:
            return False
        if room is None:
            return await super()._dispatch_discord_message(message)
        if current_profile_name() != room.controller_profile:
            return False
        admitted, role_authorized = self._room_admission(message, claim=True)
        if not admitted:
            return False
        return await self._handle_room_message(
            message,
            room,
            role_authorized=role_authorized,
        )

    async def _dispatch_recovered_message(self, message: Any) -> bool:
        room = self._room_for_discord_object(message)
        if room is _ROOM_UNAVAILABLE:
            return False
        if room is None:
            return await super()._dispatch_recovered_message(message)
        if current_profile_name() != room.controller_profile:
            return False
        admitted, role_authorized = self._room_admission(message, claim=False)
        if not admitted:
            return False
        return await self._handle_room_message(
            message,
            room,
            role_authorized=role_authorized,
        )

    def _room_channel_authorized(self, message: Any) -> bool:
        channel = getattr(message, "channel", None)
        parent_id = self._get_parent_channel_id(channel) if channel is not None else ""
        keys = self._discord_channel_keys(message, parent_id)
        allowed = self._get_allowed_channels()
        if allowed and "*" not in allowed and not (keys & allowed):
            return False
        ignored = self._get_ignored_channels()
        return not ("*" in ignored or bool(keys & ignored))

    async def _create_room_thread(self, message: Any) -> Any:
        thread_name = self._derive_auto_thread_name(message.content or "")
        last_error = None
        for attempt in range(2):
            try:
                thread = await message.create_thread(
                    name=thread_name,
                    auto_archive_duration=1440,
                )
                self._dedup.is_duplicate(str(thread.id))
                return thread
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.75)
        logger.warning(
            "[%s] Could not create a Discord thread for message %s: %s",
            self.name,
            getattr(message, "id", "?"),
            last_error,
        )
        return None

    async def _cache_room_attachments(self, message: Any) -> tuple[RoomAttachment, ...]:
        attachments: list[RoomAttachment] = []
        for item in list(getattr(message, "attachments", None) or []):
            mime = str(getattr(item, "content_type", None) or "application/octet-stream")
            name = str(getattr(item, "filename", None) or "attachment")
            extension = os.path.splitext(name)[1].lower()
            if mime.startswith("audio/") and self._is_discord_voice_message_attachment(item):
                raise ValueError("Voice messages are not supported in Bot Rooms yet.")
            max_bytes = self._discord_max_attachment_bytes()
            if max_bytes and getattr(item, "size", 0) > max_bytes:
                raise ValueError(f"Attachment {name!r} exceeds the configured size limit.")
            if mime.startswith("image/"):
                image_extension = (
                    extension if extension in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".jpg"
                )
                path = await self._cache_discord_image(item, image_extension)
                kind = "image"
            else:
                raw = await self._cache_discord_document(item, extension)
                path = base.cache_document_from_bytes(raw, name)
                kind = "pdf" if mime == "application/pdf" or extension == ".pdf" else "file"
            attachments.append(RoomAttachment(path=str(path), name=name, kind=kind, mime_type=mime))
        return tuple(attachments)

    async def _handle_room_message(
        self,
        message: Any,
        room: Any,
        *,
        role_authorized: bool = False,
    ) -> bool:
        del role_authorized
        if not self._room_channel_authorized(message):
            return False
        self._ensure_botrooms_subscription()
        if self._botrooms_service is None:
            await message.channel.send("⚠️ This Bot Room is unavailable; check the gateway logs.")
            return False

        thread = message.channel if isinstance(message.channel, discord.Thread) else None
        raw = str(getattr(message, "content", "") or "").strip()
        event_uid = f"discord:{message.id}"
        if self._botrooms_service.store.event_by_uid(event_uid) is not None:
            return False

        lowered = raw.lower()
        if lowered in {"/room-status", "/stop"}:
            if lowered == "/stop":
                result = await self._botrooms_service.stop(
                    room.room_id,
                    str(getattr(thread, "id", "") or ""),
                )
                text = "Stop requested." if result.get("stopped") else "No room run is active."
            else:
                text = self._format_room_status(
                    self._botrooms_service.status(
                        room.room_id,
                        str(getattr(thread, "id", "") or ""),
                    )
                )
            await message.channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=False,
                    replied_user=False,
                ),
            )
            return True

        if thread is None:
            thread = await self._create_room_thread(message)
            if thread is None:
                await message.channel.send(
                    "⚠️ Hermes could not create a Bot Room thread. Please retry."
                )
                return False

        try:
            attachments = await self._cache_room_attachments(message)
        except ValueError as exc:
            await self._room_control_message(str(thread.id), f"⚠️ {exc}")
            return False
        if not raw and not attachments:
            return False

        if self._botrooms_transport is not None:
            mention_map = await self._botrooms_transport.mention_map(room)
            for discord_id, member in mention_map.items():
                raw = re.sub(
                    rf"<@!?{re.escape(discord_id)}>",
                    f"@{member.mention_handle}",
                    raw,
                )
        guild = getattr(message, "guild", None)
        submitted = await self._botrooms_service.submit(
            room_id=room.room_id,
            thread_id=str(thread.id),
            event_uid=event_uid,
            text=raw,
            author_id=str(message.author.id),
            author_name=str(
                getattr(message.author, "display_name", None)
                or getattr(message.author, "name", None)
                or "User"
            ),
            attachments=attachments,
            platform_message_id=str(message.id),
            guild_id=str(getattr(guild, "id", "") or ""),
            channel_id=room.channel_id,
            metadata={"platform": "discord"},
        )
        return bool(submitted.created)

    @staticmethod
    def _format_room_status(status: dict[str, Any]) -> str:
        run = status.get("run") or {}
        lines = [
            f"**{status.get('display_name') or status.get('room_id')}** — "
            f"{run.get('status') or 'idle'}",
            f"Messages this run: {int(run.get('message_count') or 0)}",
        ]
        if run.get("current_member"):
            lines.append(f"Current agent: {run['current_member']}")
        pending = status.get("pending_prompts") or []
        if pending:
            lines.append(
                f"Waiting for you: {pending[0].get('member_key')} ({pending[0].get('kind')})"
            )
        return "\n".join(lines)

    async def _run_room_control(
        self,
        interaction: Any,
        action: str,
        *,
        agent: str = "",
        value: str = "",
    ) -> bool:
        room = self._room_for_discord_object(interaction)
        if room is _ROOM_UNAVAILABLE:
            await interaction.response.send_message(
                "This Bot Room is unavailable because its configuration is invalid.",
                ephemeral=True,
            )
            return True
        if room is None:
            return False
        if not await self._check_slash_authorization(interaction, f"/{action}"):
            return True
        if current_profile_name() != room.controller_profile:
            await interaction.response.send_message(
                f"This room is controlled by profile {room.controller_profile!r}.",
                ephemeral=True,
            )
            return True
        self._ensure_botrooms_subscription()
        service = self._botrooms_service
        if service is None:
            await interaction.response.send_message(
                "Bot Rooms are unavailable; check the gateway logs.",
                ephemeral=True,
            )
            return True
        channel = getattr(interaction, "channel", None)
        thread_id = (
            str(getattr(channel, "id", "") or "") if isinstance(channel, discord.Thread) else ""
        )
        if action == "stop":
            result = await service.stop(room.room_id, thread_id)
            text = "Stop requested." if result.get("stopped") else "No room run is active."
        elif action == "room-status":
            text = self._format_room_status(service.status(room.room_id, thread_id))
        else:
            needle = agent.strip().lstrip("@").lower()
            member = next(
                (
                    candidate
                    for candidate in room.members
                    if needle in candidate.mention_forms
                    or needle in {candidate.profile.lower(), candidate.key.lower()}
                ),
                None,
            )
            if member is None:
                text = f"No room agent matches {agent!r}."
            else:
                transport = self._botrooms_transport
                typing_started = bool(transport is not None and thread_id)
                if typing_started:
                    # Start before releasing the blocked RPC. A fast completion
                    # can then stop typing through its message event without a
                    # later stale restart racing behind it.
                    await transport.start_typing(
                        profile=member.profile,
                        thread_id=thread_id,
                    )
                try:
                    result = await service.respond(room.room_id, member.key, value)
                except Exception:
                    if typing_started:
                        await transport.stop_typing(
                            profile=member.profile,
                            thread_id=thread_id,
                        )
                    raise
                if typing_started and not result.get("resolved"):
                    await transport.stop_typing(
                        profile=member.profile,
                        thread_id=thread_id,
                    )
                text = (
                    f"Response sent to {member.label}."
                    if result.get("resolved")
                    else f"{member.label} has no pending room prompt."
                )
        await interaction.response.send_message(text, ephemeral=True)
        return True

    async def _run_simple_slash(
        self,
        interaction: Any,
        command_text: str,
        followup_msg: str | None = None,
    ) -> None:
        if command_text.strip() == "/stop" and await self._run_room_control(
            interaction,
            "stop",
        ):
            return
        await super()._run_simple_slash(interaction, command_text, followup_msg)

    def _register_slash_commands(self) -> None:
        super()._register_slash_commands()
        if not self._client:
            return
        tree = self._client.tree

        @tree.command(name="room-status", description="Show this Bot Room's status")
        async def room_status(interaction):
            if not await self._run_room_control(interaction, "room-status"):
                await interaction.response.send_message(
                    "This channel is not a configured Bot Room.",
                    ephemeral=True,
                )

        @tree.command(name="room-answer", description="Answer a Bot Room question")
        @discord.app_commands.describe(agent="Agent handle", answer="Answer to send")
        async def room_answer(interaction, agent: str, answer: str):
            if not await self._run_room_control(
                interaction,
                "room-answer",
                agent=agent,
                value=answer,
            ):
                await interaction.response.send_message(
                    "This channel is not a configured Bot Room.",
                    ephemeral=True,
                )

        @tree.command(name="room-approval", description="Resolve a Bot Room approval")
        @discord.app_commands.describe(agent="Agent handle", choice="Approval choice")
        @discord.app_commands.choices(
            choice=[
                discord.app_commands.Choice(name=name, value=name)
                for name in ("once", "session", "always", "deny")
            ]
        )
        async def room_approval(interaction, agent: str, choice: str):
            if not await self._run_room_control(
                interaction,
                "room-approval",
                agent=agent,
                value=choice,
            ):
                await interaction.response.send_message(
                    "This channel is not a configured Bot Room.",
                    ephemeral=True,
                )

    async def disconnect(self) -> None:
        self._disconnecting = True
        task = self._botrooms_recovery_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._botrooms_recovery_task = None
        if self._botrooms_service is not None and self._botrooms_subscription:
            self._botrooms_service.unsubscribe(self._botrooms_subscription)
        self._botrooms_subscription = None
        if self._botrooms_transport is not None:
            await self._botrooms_transport.stop_all_typing()
        await super().disconnect()


def build_adapter(config):
    """Avoid double-wrapping a Hermes build that already contains Bot Rooms."""

    if hasattr(base.DiscordAdapter, "_bot_room_for_discord_object"):
        return base.DiscordAdapter(config)
    return BotRoomsDiscordAdapter(config)


def register_platform(ctx) -> None:
    ctx.register_platform(
        name="discord",
        label="Discord",
        adapter_factory=build_adapter,
        check_fn=base.discord_deps_present,
        ensure_deps_fn=base.check_discord_requirements,
        is_connected=base._is_connected,
        required_env=["DISCORD_BOT_TOKEN"],
        install_hint="Run `hermes setup` to install Discord support.",
        setup_fn=base.interactive_setup,
        apply_yaml_config_fn=base._apply_yaml_config,
        allowed_users_env="DISCORD_ALLOWED_USERS",
        allow_all_env="DISCORD_ALLOW_ALL_USERS",
        cron_deliver_env_var="DISCORD_HOME_CHANNEL",
        standalone_sender_fn=base._standalone_send,
        max_message_length=2000,
        emoji="🤖",
        allow_update_command=True,
    )
