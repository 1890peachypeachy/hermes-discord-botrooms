"""Discord REST egress for canonical room events.

Each member's own profile token is resolved only at send time.  The room
database stores profile identity and delivery receipts, never bot tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .config import BotRoomConfig, BotRoomMember, profile_home
from .models import RoomEvent
from .store import RoomStore

logger = logging.getLogger(__name__)
DISCORD_API = "https://discord.com/api/v10"
MAX_CHUNK_CHARS = 1900
MAX_CHUNKS = 8
TYPING_REFRESH_SECONDS = 8.0


class DiscordRequestError(RuntimeError):
    """A failed Discord request, classified by duplicate-delivery risk."""

    def __init__(self, message: str, *, ambiguous: bool):
        super().__init__(message)
        self.ambiguous = bool(ambiguous)


class AmbiguousDiscordDeliveryError(RuntimeError):
    """A Discord POST may have succeeded, so retrying could duplicate it."""


def _profile_token(root: Path, profile: str) -> str:
    from agent.secret_scope import build_profile_secret_scope

    scope = build_profile_secret_scope(profile_home(root, profile))
    return str(scope.get("DISCORD_BOT_TOKEN") or "").strip()


def _profile_proxy(root: Path, profile: str) -> str | None:
    from agent.secret_scope import build_profile_secret_scope
    from gateway.platforms.base import normalize_proxy_url, resolve_proxy_url

    scope = build_profile_secret_scope(profile_home(root, profile))
    configured = str(scope.get("DISCORD_PROXY") or "").strip()
    if configured:
        return normalize_proxy_url(configured)
    return resolve_proxy_url(target_hosts={"discord.com"})


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
        notice = "\n\n[Response truncated at the Discord room delivery limit.]"
        chunks[-1] = chunks[-1][: (MAX_CHUNK_CHARS - len(notice))].rstrip() + notice
    return chunks


class DiscordRoomTransport:
    def __init__(self, root: Path, store: RoomStore):
        self.root = Path(root)
        self.store = store
        self._user_ids: dict[str, str] = {}
        self._typing_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        payload: dict[str, Any] | None = None,
        proxy_url: str | None = None,
    ) -> dict[str, Any]:
        import aiohttp
        from gateway.platforms.base import proxy_kwargs_for_aiohttp

        headers = {"Authorization": f"Bot {token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        url = f"{DISCORD_API}{path}"
        last_error = ""
        ambiguous = False
        for attempt in range(4):
            try:
                session_kwargs, request_kwargs = proxy_kwargs_for_aiohttp(proxy_url)
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30), **session_kwargs
                ) as session:
                    async with session.request(
                        method,
                        url,
                        headers=headers,
                        json=payload,
                        **request_kwargs,
                    ) as response:
                        raw = await response.text()
                        data = json.loads(raw) if raw else {}
                        if response.status in {200, 201, 204}:
                            return data if isinstance(data, dict) else {}
                        if response.status == 429:
                            retry_after = float(
                                (data if isinstance(data, dict) else {}).get("retry_after", 1)
                            )
                            await asyncio.sleep(min(max(retry_after, 0.25), 30))
                            continue
                        last_error = f"Discord API {response.status}: {raw[:500]}"
                        if response.status == 408 or response.status >= 500:
                            # A gateway/server timeout or failure does not prove
                            # that Discord did not commit the message.
                            ambiguous = True
                        if response.status < 500:
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # The request may have reached Discord even if its response did
                # not reach us. A later explicit HTTP rejection cannot prove
                # that an earlier timed-out attempt was not accepted.
                ambiguous = True
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 3:
                await asyncio.sleep(min(2**attempt, 5))
        raise DiscordRequestError(last_error or "Discord API request failed", ambiguous=ambiguous)

    async def bot_user_id(self, member: BotRoomMember) -> str:
        if member.discord_bot_user_id:
            return member.discord_bot_user_id
        if member.profile in self._user_ids:
            return self._user_ids[member.profile]
        token = _profile_token(self.root, member.profile)
        if not token:
            return ""
        data = await self._request(
            "GET",
            "/users/@me",
            token,
            proxy_url=_profile_proxy(self.root, member.profile),
        )
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

    async def _typing_loop(self, profile: str, thread_id: str) -> None:
        key = (profile, thread_id)
        task = asyncio.current_task()
        try:
            try:
                token = _profile_token(self.root, profile)
            except Exception as exc:
                logger.debug(
                    "Discord Bot Mode typing setup failed profile=%s thread=%s: %s",
                    profile,
                    thread_id,
                    exc,
                )
                return
            if not token:
                logger.warning(
                    "Discord Bot Mode typing unavailable: profile %s has no bot token",
                    profile,
                )
                return
            try:
                proxy_url = _profile_proxy(self.root, profile)
            except Exception as exc:
                logger.debug(
                    "Discord Bot Mode typing proxy setup failed profile=%s thread=%s: %s",
                    profile,
                    thread_id,
                    exc,
                )
                return
            while True:
                try:
                    await self._request(
                        "POST",
                        f"/channels/{thread_id}/typing",
                        token,
                        proxy_url=proxy_url,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Typing is presentation only. A REST outage or missing
                    # permission must never fail, delay, or duplicate a turn.
                    logger.debug(
                        "Discord Bot Mode typing failed profile=%s thread=%s: %s",
                        profile,
                        thread_id,
                        exc,
                    )
                await asyncio.sleep(TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise
        finally:
            if self._typing_tasks.get(key) is task:
                self._typing_tasks.pop(key, None)

    async def start_typing(self, *, profile: str, thread_id: str) -> None:
        """Show the working member's own Discord identity as typing."""

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
        thread_id: str,
        text: str,
        nonce_seed: str = "",
        event_id: int = 0,
    ) -> str:
        token = _profile_token(self.root, profile)
        if not token:
            raise RuntimeError(f"profile {profile!r} has no DISCORD_BOT_TOKEN configured")
        message_id = ""
        proxy_url = _profile_proxy(self.root, profile)
        for index, chunk in enumerate(_chunks(text)):
            nonce = (
                hashlib.blake2s(f"{nonce_seed}:{index}".encode("utf-8"), digest_size=12).hexdigest()
                if nonce_seed
                else ""
            )
            if event_id:
                delivered = self.store.delivered_chunk_message_id(
                    event_id,
                    platform="discord",
                    destination=thread_id,
                    chunk_index=index,
                )
                if delivered:
                    message_id = delivered
                    continue
                if (
                    self.store.delivery_chunk_status(
                        event_id,
                        platform="discord",
                        destination=thread_id,
                        chunk_index=index,
                    )
                    == "unknown"
                ):
                    raise AmbiguousDiscordDeliveryError(
                        "Discord room chunk has an ambiguous prior delivery; it was not reposted"
                    )
            payload: dict[str, Any] = {
                "content": chunk,
                "allowed_mentions": {
                    "parse": [],
                    "users": [],
                    "roles": [],
                    "replied_user": False,
                },
            }
            if nonce:
                payload.update({"nonce": nonce, "enforce_nonce": True})
            if event_id:
                # Write ahead of the HTTP request. If Discord accepts the POST
                # but its response is lost, a later process must not repost the
                # chunk after Discord's nonce-deduplication window expires.
                self.store.begin_delivery_chunk(
                    event_id,
                    platform="discord",
                    destination=thread_id,
                    chunk_index=index,
                    nonce=nonce,
                )
            try:
                data = await self._request(
                    "POST",
                    f"/channels/{thread_id}/messages",
                    token,
                    payload=payload,
                    proxy_url=proxy_url,
                )
                chunk_message_id = str(data.get("id") or "")
                if not chunk_message_id:
                    raise RuntimeError("Discord accepted a room chunk without a message ID")
            except DiscordRequestError as exc:
                if event_id and not exc.ambiguous:
                    # Discord returned an explicit non-success response for
                    # every attempt, so no message was created. Keep this
                    # retryable for a later token/permission correction.
                    self.store.fail_delivery_chunk_definitively(
                        event_id,
                        platform="discord",
                        destination=thread_id,
                        chunk_index=index,
                        nonce=nonce,
                        error=str(exc),
                    )
                    raise
                raise AmbiguousDiscordDeliveryError(str(exc)) from exc
            except AmbiguousDiscordDeliveryError:
                raise
            except Exception as exc:
                # Keep the write-ahead row ambiguous: the request may have
                # reached Discord even though no response reached us.
                raise AmbiguousDiscordDeliveryError(str(exc)) from exc
            message_id = chunk_message_id
            if event_id:
                self.store.record_delivery_chunk(
                    event_id,
                    platform="discord",
                    destination=thread_id,
                    chunk_index=index,
                    nonce=nonce,
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
            event.id, platform="discord", destination=destination
        )
        if delivered and not delivered.startswith("ambiguous:"):
            return delivered
        try:
            message_id = await self.send_text(
                profile=member.profile,
                thread_id=destination,
                text=event.text,
                nonce_seed=event.event_uid,
                event_id=event.id,
            )
        except AmbiguousDiscordDeliveryError as exc:
            # Event-level suppression is deliberately understood by older Bot
            # Mode builds that predate chunk receipts. This makes an executable
            # rollback fail closed too: it will see a delivered receipt and
            # will not repost a possibly accepted Discord message.
            self.store.record_delivery(
                event.id,
                platform="discord",
                destination=destination,
                status="delivered",
                platform_message_id=f"ambiguous:{event.event_uid}",
                error=str(exc),
            )
            raise
        except DiscordRequestError:
            # The per-chunk row is retryable, while the event-level sentinel
            # must remain to suppress chunk-unaware rollback builds.
            raise
        except Exception as exc:
            self.store.record_delivery(
                event.id,
                platform="discord",
                destination=destination,
                status="failed",
                error=str(exc),
            )
            raise
        self.store.record_delivery(
            event.id,
            platform="discord",
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
                platform="discord",
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
                        "Discord Bot Mode recovery failed room=%s event=%s",
                        room.room_id,
                        event.id,
                    )
        return {"delivered": delivered, "failed": failed}
