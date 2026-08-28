"""Mattermost REST egress for canonical room events.

Mattermost port of ``discord_transport.py``: same canonical room event flow,
same SQLite delivery ledger semantics (write-ahead chunks, ambiguous-delivery
fail-closed), same typing-task lifecycle. Only the wire protocol differs.

Each member's own profile token is resolved only at send time.  The room
database stores profile identity and delivery receipts, never tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from .config import BotRoomConfig, BotRoomMember, profile_home
from .models import RoomEvent
from .store import RoomStore

logger = logging.getLogger(__name__)
MAX_CHUNK_CHARS = 3900  # Mattermost post limit is 4000; keep headroom
MAX_CHUNKS = 8
TYPING_REFRESH_SECONDS = 4.0  # MM typing indicators decay faster than Discord's

PLATFORM = "mattermost"


class MattermostRequestError(RuntimeError):
    """A failed Mattermost request, classified by duplicate-delivery risk."""

    def __init__(self, message: str, *, ambiguous: bool):
        super().__init__(message)
        self.ambiguous = bool(ambiguous)


class AmbiguousMattermostDeliveryError(RuntimeError):
    """A Mattermost POST may have succeeded, so retrying could duplicate it."""


def _profile_credentials(root: Path, profile: str) -> tuple[str, str]:
    """Return (base_url, token) from the profile's own secret scope."""
    from agent.secret_scope import build_profile_secret_scope

    scope = build_profile_secret_scope(profile_home(root, profile))
    base_url = str(scope.get("MATTERMOST_URL") or "").strip().rstrip("/")
    token = str(scope.get("MATTERMOST_TOKEN") or "").strip()
    return base_url, token


def _chunks(text: str) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    chunks: list[str] = []
    while value and len(chunks) < MAX_CHUNKS:
        if len(value) <= MAX_CHUNK_CHARS:
            chunks.append(value)
            value = ""
            break
        split = value.rfind("\n", 0, MAX_CHUNK_CHARS)
        if split < MAX_CHUNK_CHARS // 2:
            split = value.rfind(" ", 0, MAX_CHUNK_CHARS)
        if split < MAX_CHUNK_CHARS // 2:
            split = MAX_CHUNK_CHARS
        chunks.append(value[:split].rstrip())
        value = value[split:].lstrip()
    if value:
        notice = "\n\n[Response truncated at the Mattermost room delivery limit.]"
        chunks[-1] = chunks[-1][: (MAX_CHUNK_CHARS - len(notice))].rstrip() + notice
    return chunks


class MattermostRoomTransport:
    def __init__(self, root: Path, store: RoomStore):
        self.root = Path(root)
        self.store = store
        self._user_ids: dict[str, str] = {}
        self._typing_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def _request(
        self,
        method: str,
        path: str,
        base_url: str,
        token: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import aiohttp

        headers = {"Authorization": f"Bearer {token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        url = f"{base_url}/api/v4{path}"
        last_error = ""
        ambiguous = False
        for attempt in range(4):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as session:
                    async with session.request(
                        method, url, headers=headers, json=payload
                    ) as response:
                        raw = await response.text()
                        try:
                            data = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            data = {}
                        if response.status in {200, 201, 204}:
                            return data if isinstance(data, dict) else {}
                        last_error = f"Mattermost API {response.status}: {raw[:500]}"
                        if response.status == 408 or response.status >= 500:
                            # A server timeout or failure does not prove that
                            # Mattermost did not commit the message.
                            ambiguous = True
                        if response.status < 500:
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # The request may have reached Mattermost even if its response
                # did not reach us.
                ambiguous = True
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 3:
                await asyncio.sleep(min(2**attempt, 5))
        raise MattermostRequestError(
            last_error or "Mattermost API request failed", ambiguous=ambiguous
        )

    async def bot_user_id(self, member: BotRoomMember) -> str:
        base_url, token = _profile_credentials(self.root, member.profile)
        if not token:
            return ""
        if member.profile in self._user_ids:
            return self._user_ids[member.profile]
        data = await self._request("GET", "/users/me", base_url, token)
        user_id = str(data.get("id") or "")
        if user_id:
            self._user_ids[member.profile] = user_id
        return user_id

    async def mention_map(self, room: BotRoomConfig) -> dict[str, BotRoomMember]:
        pairs = await asyncio.gather(
            *(self.bot_user_id(member) for member in room.members),
            return_exceptions=True,
        )
        mapping: dict[str, BotRoomMember] = {}
        for member, value in zip(room.members, pairs):
            if isinstance(value, str) and value:
                mapping[value] = member
        return mapping

    async def _typing_loop(self, profile: str, channel_id: str) -> None:
        key = (profile, channel_id)
        task = asyncio.current_task()
        try:
            try:
                base_url, token = _profile_credentials(self.root, profile)
            except Exception as exc:
                logger.debug(
                    "Mattermost Bot Rooms typing setup failed profile=%s channel=%s: %s",
                    profile, channel_id, exc,
                )
                return
            if not token:
                logger.warning(
                    "Mattermost Bot Rooms typing unavailable: profile %s has no token",
                    profile,
                )
                return
            while True:
                try:
                    await self._request(
                        "POST",
                        "/users/me/typing",
                        base_url,
                        token,
                        payload={"channel_id": channel_id, "parent_id": ""},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Typing is presentation only. An outage must never fail,
                    # delay, or duplicate a turn.
                    logger.debug(
                        "Mattermost Bot Rooms typing failed profile=%s channel=%s: %s",
                        profile, channel_id, exc,
                    )
                await asyncio.sleep(TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise
        finally:
            if self._typing_tasks.get(key) is task:
                self._typing_tasks.pop(key, None)

    async def start_typing(self, *, profile: str, thread_id: str) -> None:
        """Show the working member's own Mattermost identity as typing."""
        key = (str(profile), str(thread_id))
        existing = self._typing_tasks.get(key)
        if existing is not None and not existing.done():
            return
        self._typing_tasks[key] = asyncio.create_task(self._typing_loop(*key))
        # Let the first typing POST start before returning to the event sink.
        await asyncio.sleep(0)

    async def stop_typing(self, *, profile: str, thread_id: str) -> None:
        key = (str(profile), str(thread_id))
        task = self._typing_tasks.pop(key, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def stop_thread_typing(self, thread_id: str) -> None:
        tasks = [
            self.stop_typing(profile=profile, thread_id=destination)
            for profile, destination in tuple(self._typing_tasks)
            if destination == str(thread_id)
        ]
        if tasks:
            await asyncio.gather(*tasks)

    async def stop_all_typing(self) -> None:
        tasks = [
            self.stop_typing(profile=profile, thread_id=thread_id)
            for profile, thread_id in tuple(self._typing_tasks)
        ]
        if tasks:
            await asyncio.gather(*tasks)

    async def send_text(
        self,
        *,
        profile: str,
        channel_id: str,
        thread_id: str,
        text: str,
        nonce_seed: str = "",
        event_id: int = 0,
    ) -> str:
        """Post text to a Mattermost room.

        ``thread_id`` is the room thread's root post ID (Mattermost threads are
        addressed as channel_id + root_id).  An empty ``thread_id`` posts a
        top-level channel message.
        """

        base_url, token = _profile_credentials(self.root, profile)
        if not token:
            raise RuntimeError(
                f"profile {profile!r} has no MATTERMOST_TOKEN configured"
            )
        message_id = ""
        for index, chunk in enumerate(_chunks(text)):
            if event_id:
                delivered = self.store.delivered_chunk_message_id(
                    event_id,
                    platform=PLATFORM,
                    destination=thread_id or channel_id,
                    chunk_index=index,
                )
                if delivered:
                    message_id = delivered
                    continue
                if (
                    self.store.delivery_chunk_status(
                        event_id,
                        platform=PLATFORM,
                        destination=thread_id or channel_id,
                        chunk_index=index,
                    )
                    == "unknown"
                ):
                    raise AmbiguousMattermostDeliveryError(
                        "Mattermost room chunk has an ambiguous prior delivery; "
                        "it was not reposted"
                    )
            payload: dict[str, Any] = {
                "channel_id": channel_id,
                "message": chunk,
            }
            if thread_id:
                payload["root_id"] = thread_id
            if event_id:
                # Write ahead of the HTTP request. If Mattermost accepts the
                # POST but its response is lost, a later process must not
                # repost the chunk.
                self.store.begin_delivery_chunk(
                    event_id,
                    platform=PLATFORM,
                    destination=thread_id or channel_id,
                    chunk_index=index,
                    nonce="",
                )
            try:
                data = await self._request(
                    "POST", "/posts", base_url, token, payload=payload
                )
                chunk_message_id = str(data.get("id") or "")
                if not chunk_message_id:
                    raise RuntimeError(
                        "Mattermost accepted a room chunk without a post ID"
                    )
            except MattermostRequestError as exc:
                if event_id and not exc.ambiguous:
                    self.store.fail_delivery_chunk_definitively(
                        event_id,
                        platform=PLATFORM,
                        destination=thread_id or channel_id,
                        chunk_index=index,
                        nonce="",
                        error=str(exc),
                    )
                    raise
                raise AmbiguousMattermostDeliveryError(str(exc)) from exc
            except AmbiguousMattermostDeliveryError:
                raise
            except Exception as exc:
                raise AmbiguousMattermostDeliveryError(str(exc)) from exc
            message_id = chunk_message_id
            if event_id:
                self.store.record_delivery_chunk(
                    event_id,
                    platform=PLATFORM,
                    destination=thread_id or channel_id,
                    chunk_index=index,
                    nonce="",
                    status="delivered",
                    platform_message_id=message_id,
                )
        return message_id

    async def deliver_event(
        self,
        room: BotRoomConfig,
        event: RoomEvent,
        member: BotRoomMember,
    ) -> str:
        destination = event.thread_id
        delivered = self.store.delivered_message_id(
            event.id, platform=PLATFORM, destination=destination
        )
        if delivered and not delivered.startswith("ambiguous:"):
            return delivered
        try:
            message_id = await self.send_text(
                profile=member.profile,
                channel_id=room.channel_id,
                thread_id=destination,
                text=event.text,
                nonce_seed=event.event_uid,
                event_id=event.id,
            )
        except AmbiguousMattermostDeliveryError as exc:
            # Event-level suppression is deliberately understood by older Bot
            # Mode builds that predate chunk receipts.
            self.store.record_delivery(
                event.id,
                platform=PLATFORM,
                destination=destination,
                status="delivered",
                platform_message_id=f"ambiguous:{event.event_uid}",
                error=str(exc),
            )
            raise
        except MattermostRequestError:
            raise
        except Exception as exc:
            self.store.record_delivery(
                event.id,
                platform=PLATFORM,
                destination=destination,
                status="failed",
                error=str(exc),
            )
            raise
        self.store.record_delivery(
            event.id,
            platform=PLATFORM,
            destination=destination,
            status="delivered",
            platform_message_id=message_id,
        )
        return message_id

    async def recover_room(self, room: BotRoomConfig) -> dict[str, int]:
        delivered = failed = 0
        after_id = 0
        while True:
            events = self.store.undelivered_member_events(
                room.room_id,
                platform=PLATFORM,
                limit=100,
                after_id=after_id,
            )
            if not events:
                break
            for event in events:
                after_id = max(after_id, event.id)
                member = room.member(event.author_id)
                if member is None:
                    failed += 1
                    continue
                try:
                    await self.deliver_event(room, event, member)
                    delivered += 1
                except Exception:
                    failed += 1
                    logger.exception(
                        "Mattermost Bot Rooms recovery failed room=%s event=%s",
                        room.room_id,
                        event.id,
                    )
        return {"delivered": delivered, "failed": failed}
