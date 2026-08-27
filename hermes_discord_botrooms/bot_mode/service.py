"""Process-neutral service facade for Bot Mode room adapters and RPC."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root

from .config import (
    BotRoomConfig,
    bot_room_from_mapping,
    headless_room_engine_enabled,
    load_bot_room_registry,
)
from .models import RoomAttachment, SubmittedRun
from .profile_worker import ProfileWorkerExecutor
from .scheduler import RoomRunEngine
from .store import RoomStore

logger = logging.getLogger(__name__)
Subscriber = Callable[[dict[str, Any]], Awaitable[None]]


class BotRoomService:
    """Own one background scheduler loop while exposing async-safe methods."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        executor: Any | None = None,
    ):
        self.root = Path(root or get_default_hermes_root())
        self._started_at = time.time()
        self.store = RoomStore(self.root)
        self.executor = executor or ProfileWorkerExecutor(self.root)
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="hermes-bot-rooms",
            daemon=True,
        )
        self._thread.start()
        self._loop_ready.wait(timeout=5)
        self._engine = RoomRunEngine(self.store, self.executor, event_sink=self._publish)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._subscribers: dict[str, tuple[asyncio.AbstractEventLoop, Subscriber]] = {}
        self._subscriber_lock = threading.Lock()
        self._recovery_started = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    @property
    def event_loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def enabled(self) -> bool:
        return headless_room_engine_enabled(self.root)

    def _submit_coro(self, coroutine: Awaitable[Any]) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def _await(self, coroutine: Awaitable[Any]) -> Any:
        return await asyncio.wrap_future(self._submit_coro(coroutine))

    def _registry(self) -> dict[str, BotRoomConfig]:
        # Discord routing is config-owned. Filtering stale persisted Discord
        # descriptors makes deleting/disabling a YAML room take effect
        # immediately, while Desktop-created rooms remain durable in SQLite.
        stored = {
            room_id: room
            for room_id, room in self.store.room_configs().items()
            if room.platform != "discord"
        }
        configured = load_bot_room_registry(self.root)
        return {**stored, **configured}

    def rooms(self) -> dict[str, BotRoomConfig]:
        return self._registry()

    def room(self, room_id: str) -> BotRoomConfig:
        room = self._registry().get(room_id)
        if room is None:
            raise KeyError(f"Bot Mode room {room_id!r} does not exist")
        return room

    def subscribe(
        self,
        callback: Subscriber,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> str:
        target_loop = loop or asyncio.get_running_loop()
        token = uuid.uuid4().hex
        with self._subscriber_lock:
            self._subscribers[token] = (target_loop, callback)
            start_recovery = not self._recovery_started
            self._recovery_started = True
        if start_recovery:
            self._submit_coro(self._recover_runs())
        return token

    def unsubscribe(self, token: str) -> None:
        with self._subscriber_lock:
            self._subscribers.pop(token, None)

    async def _publish(self, event: dict[str, Any]) -> None:
        with self._subscriber_lock:
            subscribers = list(self._subscribers.values())
        for target_loop, callback in subscribers:
            try:
                if target_loop is self._loop:
                    await callback(event)
                elif target_loop.is_running():
                    await asyncio.wrap_future(
                        asyncio.run_coroutine_threadsafe(callback(event), target_loop)
                    )
            except Exception:
                logger.exception("Bot Mode room subscriber failed")

    async def submit(
        self,
        *,
        room_id: str,
        thread_id: str,
        event_uid: str,
        text: str,
        author_id: str,
        author_name: str,
        attachments: Sequence[RoomAttachment] = (),
        platform_message_id: str = "",
        guild_id: str = "",
        channel_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SubmittedRun:
        return await self._await(
            self._submit(
                room_id=room_id,
                thread_id=thread_id,
                event_uid=event_uid,
                text=text,
                author_id=author_id,
                author_name=author_name,
                attachments=attachments,
                platform_message_id=platform_message_id,
                guild_id=guild_id,
                channel_id=channel_id,
                metadata=metadata,
            )
        )

    def submit_sync(self, **kwargs: Any) -> SubmittedRun:
        return self._submit_coro(self._submit(**kwargs)).result(timeout=30)

    async def _submit(self, **kwargs: Any) -> SubmittedRun:
        room = self.room(str(kwargs.pop("room_id")))
        submitted = await asyncio.to_thread(self.store.submit_user_event, room, **kwargs)
        if submitted.created:
            if submitted.superseded_run_id:
                await self._interrupt_superseded_run(room, submitted.superseded_run_id)
            task = asyncio.create_task(
                self._engine.run(room, submitted.thread_id, submitted.run_id)
            )
            self._tasks[submitted.run_id] = task
            task.add_done_callback(
                lambda _task, run_id=submitted.run_id: self._tasks.pop(run_id, None)
            )
        return submitted

    async def _recover_runs(self) -> None:
        try:
            registry = self._registry()
            rows = await asyncio.to_thread(
                self.store.recoverable_runs,
                tuple(registry),
                before=self._started_at,
            )
            for row in rows:
                run_id = str(row["run_id"])
                room = registry.get(str(row["room_id"]))
                if room is None or run_id in self._tasks:
                    continue
                task = asyncio.create_task(
                    self._engine.run(
                        room,
                        str(row["thread_id"]),
                        run_id,
                        recovering=True,
                    )
                )
                self._tasks[run_id] = task
                task.add_done_callback(
                    lambda _task, recovered_id=run_id: self._tasks.pop(recovered_id, None)
                )
        except Exception:
            logger.exception("Bot Mode startup run recovery failed")

    async def _interrupt_superseded_run(self, room: BotRoomConfig, run_id: str) -> None:
        run = self.store.run_row(run_id)
        pending = self.store.pending_prompt(room.room_id)
        old_task = self._tasks.get(run_id)
        try:
            # Stop and join the old scheduler first. ProfileWorkerExecutor's
            # cancellation bridges join every native worker RPC/event waiter,
            # so nothing from the superseded turn can race after this await.
            if old_task is not None and not old_task.done():
                old_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await old_task
            if pending is not None and pending.run_id == run_id:
                member = room.member(pending.member_key)
                if member is not None:
                    await self.executor.interrupt(member.profile, pending.runtime_session_id)
            elif run is not None:
                member = room.member(str(run.get("current_member") or ""))
                interrupt_current = getattr(self.executor, "interrupt_room_member", None)
                if member is not None and interrupt_current is not None:
                    await interrupt_current(room.room_id, member)
        except Exception:
            logger.exception(
                "Bot Mode superseded-turn interrupt failed room=%s run=%s",
                room.room_id,
                run_id,
            )
        finally:
            self.store.clear_run_prompts(run_id)

    async def stop(self, room_id: str, thread_id: str = "") -> dict[str, Any]:
        return await self._await(self._stop(room_id, thread_id))

    def stop_sync(self, room_id: str, thread_id: str = "") -> dict[str, Any]:
        return self._submit_coro(self._stop(room_id, thread_id)).result(timeout=30)

    async def _stop(self, room_id: str, thread_id: str) -> dict[str, Any]:
        room = self.room(room_id)
        run = await asyncio.to_thread(self.store.request_stop, room_id, thread_id)
        if not run:
            return {"stopped": False, "reason": "no active run"}
        pending = self.store.pending_prompt(room_id)
        if pending and pending.run_id == run["run_id"]:
            member = room.member(pending.member_key)
            if member is not None:
                try:
                    await self.executor.interrupt(member.profile, pending.runtime_session_id)
                except Exception:
                    logger.exception("Bot Mode interrupt failed")
        else:
            member = room.member(str(run.get("current_member") or ""))
            interrupt_current = getattr(self.executor, "interrupt_room_member", None)
            if member is not None and interrupt_current is not None:
                try:
                    await interrupt_current(room_id, member)
                except Exception:
                    logger.exception("Bot Mode active-turn interrupt failed")
        return {"stopped": True, "run_id": run["run_id"]}

    async def respond(self, room_id: str, member_key: str, value: str) -> dict[str, Any]:
        return await self._await(self._respond(room_id, member_key, value))

    def respond_sync(self, room_id: str, member_key: str, value: str) -> dict[str, Any]:
        return self._submit_coro(self._respond(room_id, member_key, value)).result(timeout=30)

    async def _respond(self, room_id: str, member_key: str, value: str) -> dict[str, Any]:
        pending = self.store.pending_prompt(room_id, member_key)
        if pending is None:
            return {"resolved": False, "reason": "no pending prompt"}
        resolved = await self.executor.respond(pending, value)
        if resolved:
            self.store.clear_pending_prompt(room_id, member_key)
            self.store.update_run(pending.run_id, "running", current_member=member_key)
        return {"resolved": bool(resolved), "run_id": pending.run_id}

    def status(self, room_id: str, thread_id: str = "") -> dict[str, Any]:
        self.room(room_id)
        return self.store.status(room_id, thread_id)

    def create_or_update(self, payload: dict[str, Any]) -> BotRoomConfig:
        room = bot_room_from_mapping(payload)
        self.store.sync_room(room)
        return room

    def delete(self, room_id: str) -> bool:
        configured = load_bot_room_registry(self.root)
        if room_id in configured:
            raise ValueError(
                f"room {room_id!r} is defined in config.yaml and must be removed there"
            )
        return self.store.delete_room(room_id)

    def legacy_import(self, rooms: Sequence[dict[str, Any]]) -> dict[str, int]:
        imported = 0
        skipped = 0
        for row in rooms:
            try:
                payload = dict(row)
                payload.setdefault("platform", "desktop")
                payload.setdefault("channel_id", "")
                self.create_or_update(payload)
                imported += 1
            except (TypeError, ValueError):
                skipped += 1
        return {"imported": imported, "skipped": skipped}

    def close(self) -> None:
        future = self._submit_coro(self._close())
        try:
            future.result(timeout=5)
        except Exception:
            logger.debug("Bot Mode service close timed out", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

    async def _close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self.executor, "close", None)
        if close:
            await asyncio.to_thread(close)


_services: dict[str, BotRoomService] = {}
_services_lock = threading.Lock()


def get_bot_room_service(root: Path | None = None) -> BotRoomService:
    install_root = Path(root or get_default_hermes_root()).resolve(strict=False)
    key = str(install_root)
    with _services_lock:
        service = _services.get(key)
        if service is None:
            service = BotRoomService(install_root)
            _services[key] = service
        return service
