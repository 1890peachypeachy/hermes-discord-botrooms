"""Bounded serial Bot Mode room scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .config import BotRoomConfig, BotRoomMember
from .models import MemberTurnResult, PendingPrompt, RoomAttachment
from .prompts import (
    HISTORY_LIMIT,
    MAX_MESSAGES,
    MAX_ROUNDS,
    build_turn_prompt,
    format_room_line,
    is_pass_text,
    resolve_responders,
    rotate_speakers,
    should_commit_turn,
)
from .store import RoomStore, room_run_lock

logger = logging.getLogger(__name__)


class MemberExecutor(Protocol):
    async def turn(
        self,
        room: BotRoomConfig,
        member: BotRoomMember,
        prompt: str,
        attachments: list[RoomAttachment],
        *,
        run_id: str,
        thread_id: str,
        stored_session_id: str = "",
        on_blocked: Callable[[PendingPrompt], Awaitable[None]] | None = None,
        recovering: bool = False,
    ) -> MemberTurnResult: ...


EventSink = Callable[[dict[str, Any]], Awaitable[None]]


async def _nothing(_event: dict[str, Any]) -> None:
    return None


class RoomRunEngine:
    def __init__(
        self,
        store: RoomStore,
        executor: MemberExecutor,
        *,
        event_sink: EventSink | None = None,
    ):
        self.store = store
        self.executor = executor
        self.event_sink = event_sink or _nothing
        self._process_locks: dict[str, asyncio.Lock] = {}

    async def _emit(self, kind: str, **payload: Any) -> None:
        try:
            await self.event_sink({"kind": kind, **payload})
        except Exception:
            logger.exception("Bot Mode event sink failed for %s", kind)

    async def _blocked(self, pending: PendingPrompt) -> None:
        self.store.set_pending_prompt(pending)
        self.store.update_run(pending.run_id, "blocked", current_member=pending.member_key)
        await self._emit(
            "prompt",
            room_id=pending.room_id,
            thread_id=pending.thread_id,
            run_id=pending.run_id,
            member_key=pending.member_key,
            prompt_kind=pending.kind,
            request_id=pending.request_id,
            payload=pending.payload,
        )

    async def _finish_run(
        self,
        *,
        room_id: str,
        thread_id: str,
        run_id: str,
        status: str,
        posted: int,
        error: str | None,
    ) -> None:
        row = self.store.run_row(run_id)
        if row and row["status"] == "superseded":
            status = "superseded"
        elif row and (row["status"] in {"stopping", "stopped"} or row["stop_requested"]):
            status = "stopped"
        self.store.update_run(run_id, status, error=error, finished=True)
        self.store.clear_run_turn_attempts(run_id)
        await self._emit(
            "run.finished",
            room_id=room_id,
            thread_id=thread_id,
            run_id=run_id,
            status=status,
            message_count=posted,
            error=error,
        )

    async def run(
        self,
        room: BotRoomConfig,
        thread_id: str,
        run_id: str,
        *,
        recovering: bool = False,
    ) -> None:
        process_lock = self._process_locks.setdefault(room.room_id, asyncio.Lock())
        async with process_lock:
            lock_cm = room_run_lock(self.store, room.room_id)
            await asyncio.to_thread(lock_cm.__enter__)
            try:
                await self._run_locked(room, thread_id, run_id, recovering=recovering)
            finally:
                await asyncio.to_thread(lock_cm.__exit__, None, None, None)

    async def _run_locked(
        self,
        room: BotRoomConfig,
        thread_id: str,
        run_id: str,
        *,
        recovering: bool = False,
    ) -> None:
        run = self.store.run_row(run_id)
        if not run or run["status"] not in {"queued", "running", "blocked"}:
            return
        if recovering:
            self.store.clear_run_prompts(run_id)
        dispatch_epoch = int(run["epoch"])
        self.store.update_run(run_id, "running")
        await self._emit(
            "run.started",
            room_id=room.room_id,
            thread_id=thread_id,
            run_id=run_id,
            epoch=dispatch_epoch,
        )
        posted = int(run.get("message_count") or 0)
        final_status = "settled"
        final_error: str | None = None
        preserve_for_restart = False
        try:
            for round_number in range(MAX_ROUNDS):
                if self.store.run_should_stop(run_id):
                    final_status = "stopped"
                    break
                room_log = self.store.thread_events(room.room_id, thread_id)
                responders = rotate_speakers(
                    resolve_responders(room_log, room.members), round_number
                )
                spoke_this_round = 0
                for member in responders:
                    if self.store.run_should_stop(run_id):
                        final_status = "stopped"
                        break
                    if posted >= MAX_MESSAGES:
                        final_status = "capped"
                        break
                    delta, _seen = self.store.delta_for_member(room.room_id, thread_id, member.key)
                    if not delta:
                        continue
                    anchor_id = delta[-1].id
                    if member.key in self.store.holds(room.room_id):
                        self.store.advance_watermark(room.room_id, thread_id, member.key, anchor_id)
                        await self._emit(
                            "member.held",
                            room_id=room.room_id,
                            thread_id=thread_id,
                            run_id=run_id,
                            member_key=member.key,
                            member_name=member.label,
                        )
                        continue
                    prompt = build_turn_prompt(
                        group_name=room.display_name,
                        members=room.members,
                        viewer=member,
                        delta_lines=(
                            format_room_line(entry, member) for entry in delta[-HISTORY_LIMIT:]
                        ),
                    )
                    attachments = [
                        attachment for entry in delta for attachment in entry.attachments
                    ]
                    self.store.update_run(run_id, "running", current_member=member.key)
                    await self._emit(
                        "member.started",
                        room_id=room.room_id,
                        thread_id=thread_id,
                        run_id=run_id,
                        member_key=member.key,
                        member_name=member.label,
                        round=round_number,
                    )
                    try:
                        result = await self.executor.turn(
                            room,
                            member,
                            prompt,
                            attachments,
                            run_id=run_id,
                            thread_id=thread_id,
                            stored_session_id=self.store.session_id(room.room_id, member.key),
                            on_blocked=self._blocked,
                            recovering=recovering,
                        )
                    except Exception as exc:
                        logger.exception(
                            "Bot Mode member turn failed room=%s member=%s",
                            room.room_id,
                            member.key,
                        )
                        result = MemberTurnResult(status="error", error=str(exc))
                    if result.stored_session_id:
                        self.store.save_session(
                            room.room_id,
                            member.key,
                            member.profile,
                            result.stored_session_id,
                        )
                    self.store.clear_pending_prompt(room.room_id, member.key)
                    current_epoch = self.store.room_epoch(room.room_id)
                    newer_user = self.store.newer_user_in_thread(room.room_id, thread_id, anchor_id)
                    if not should_commit_turn(dispatch_epoch, current_epoch, newer_user):
                        self.store.clear_turn_attempt(run_id, member.key)
                        final_status = "superseded"
                        await self._emit(
                            "run.superseded",
                            room_id=room.room_id,
                            thread_id=thread_id,
                            run_id=run_id,
                        )
                        return
                    self.store.advance_watermark(room.room_id, thread_id, member.key, anchor_id)
                    if result.status not in {"complete", "interrupted"}:
                        self.store.clear_turn_attempt(run_id, member.key)
                        await self._emit(
                            "member.failed",
                            room_id=room.room_id,
                            thread_id=thread_id,
                            run_id=run_id,
                            member_key=member.key,
                            member_name=member.label,
                            error=result.error or result.status,
                        )
                        continue
                    if is_pass_text(result.text):
                        self.store.clear_turn_attempt(run_id, member.key)
                        await self._emit(
                            "member.passed",
                            room_id=room.room_id,
                            thread_id=thread_id,
                            run_id=run_id,
                            member_key=member.key,
                            member_name=member.label,
                        )
                        continue
                    event = self.store.append_agent_event(
                        room_id=room.room_id,
                        thread_id=thread_id,
                        run_id=run_id,
                        member_key=member.key,
                        member_name=member.label,
                        text=result.text,
                        event_uid=f"agent:{run_id}:{round_number}:{member.key}",
                        metadata={"profile": member.profile},
                    )
                    self.store.advance_watermark(room.room_id, thread_id, member.key, event.id)
                    self.store.clear_turn_attempt(run_id, member.key)
                    posted += 1
                    spoke_this_round += 1
                    await self._emit(
                        "message",
                        room_id=room.room_id,
                        thread_id=thread_id,
                        run_id=run_id,
                        member_key=member.key,
                        member_profile=member.profile,
                        member_name=member.label,
                        event=event,
                    )
                if final_status in {"stopped", "capped"}:
                    break
                if spoke_this_round == 0:
                    final_status = "settled"
                    break
            else:
                final_status = "capped" if posted >= MAX_MESSAGES else "settled"
        except asyncio.CancelledError:
            row = self.store.run_row(run_id)
            preserve_for_restart = bool(
                row
                and row["status"] in {"queued", "running", "blocked"}
                and not row["stop_requested"]
            )
            raise
        except Exception as exc:
            final_status = "error"
            final_error = str(exc)
            logger.exception("Bot Mode room run failed room=%s", room.room_id)
        finally:
            if not preserve_for_restart:
                await self._finish_run(
                    room_id=room.room_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    status=final_status,
                    posted=posted,
                    error=final_error,
                )
