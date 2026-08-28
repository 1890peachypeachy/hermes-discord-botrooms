"""Persistent profile-scoped TUI JSON-RPC workers for room member turns."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from hermes_constants import get_default_hermes_root

from .config import BotRoomConfig, BotRoomMember, profile_home
from .models import MemberTurnResult, PendingPrompt, RoomAttachment
from .prompts import ROOM_PROTOCOL_VERSION, is_pass_text, room_system_instructions
from .store import RoomStore

logger = logging.getLogger(__name__)
BASE_TURN_TIMEOUT_SECONDS = 180
HARD_TURN_TIMEOUT_SECONDS = 20 * 60


def _room_session_source(
    room: BotRoomConfig, member: BotRoomMember, thread_id: str = ""
) -> str:
    identity = (
        f"{room.room_id}\0{thread_id}\0{member.key}"
        if room.platform == "discord"
        else f"{room.room_id}\0{member.key}"
    ).encode("utf-8")
    return (
        f"bot_room:v{ROOM_PROTOCOL_VERSION}:{hashlib.blake2s(identity, digest_size=10).hexdigest()}"
    )


def _history_row_id(message: dict[str, Any]) -> int:
    try:
        return max(0, int(message.get("row_id") or 0))
    except (TypeError, ValueError):
        return 0


def _history_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        return "".join(parts).strip()
    return str(message.get("text") or "").strip()


def _pick_turn_reply(messages: list[dict[str, Any]], baseline_row_id: int) -> str | None:
    """Prefer the newest substantive assistant answer over a trailing pass."""

    pass_text: str | None = None
    for message in reversed(messages):
        if _history_row_id(message) <= baseline_row_id or message.get("role") != "assistant":
            continue
        text = _history_text(message)
        if is_pass_text(text):
            if pass_text is None:
                pass_text = text
            continue
        return text
    return pass_text


class RpcError(RuntimeError):
    def __init__(self, message: str, *, code: int = 0, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class JsonRpcWorker:
    """Thread-safe newline JSON-RPC client around ``tui_gateway.entry``."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self._process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._waiters: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._waiters_lock = threading.Lock()
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=2048)
        self._events_cv = threading.Condition()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=80)
        self._ready = threading.Event()
        self._closed = False

    def _environment(self) -> dict[str, str]:
        from tools.environments.local import build_subprocess_env

        return build_subprocess_env(
            dict(os.environ),
            scrub_secrets=False,
            inherit_profile_home=False,
            extra={"HERMES_HOME": str(self.home), "HERMES_SESSION_SOURCE": "bot_room"},
        )

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        with self._start_lock:
            if self._process is not None and self._process.poll() is None:
                return
            self._closed = False
            self._ready.clear()
            self._process = subprocess.Popen(
                [sys.executable, "-m", "tui_gateway.entry"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._environment(),
                start_new_session=True,
            )
            threading.Thread(target=self._read_stdout, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()
        if not self._ready.wait(timeout=30):
            detail = "\n".join(self._stderr_tail)[-1000:]
            self.close()
            raise RpcError(f"profile worker did not become ready: {detail}")

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if frame.get("method") == "event":
                    params = frame.get("params") or {}
                    if params.get("type") == "gateway.ready":
                        self._ready.set()
                    with self._events_cv:
                        self._events.append(frame)
                        self._events_cv.notify_all()
                    continue
                request_id = str(frame.get("id") or "")
                with self._waiters_lock:
                    waiter = self._waiters.get(request_id)
                if waiter is not None:
                    waiter.put(frame)
        finally:
            self._ready.set()
            with self._events_cv:
                self._events_cv.notify_all()
            with self._waiters_lock:
                waiters = list(self._waiters.values())
            for waiter in waiters:
                waiter.put(
                    {
                        "error": {
                            "code": 5000,
                            "message": "profile worker exited",
                        }
                    }
                )

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr_tail.append(line.rstrip())

    def call(self, method: str, params: dict[str, Any], timeout: float = 60) -> Any:
        self.start()
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RpcError("profile worker is not running")
        request_id = uuid.uuid4().hex
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._waiters_lock:
            self._waiters[request_id] = waiter
        try:
            frame = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            with self._write_lock:
                process.stdin.write(json.dumps(frame, ensure_ascii=True) + "\n")
                process.stdin.flush()
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise RpcError(f"{method} timed out waiting for the profile worker") from exc
            if response.get("error"):
                error = response["error"]
                raise RpcError(
                    str(error.get("message") or f"{method} failed"),
                    code=int(error.get("code") or 0),
                    data=error.get("data"),
                )
            return response.get("result")
        finally:
            with self._waiters_lock:
                self._waiters.pop(request_id, None)

    def event_cursor(self, session_id: str) -> int:
        with self._events_cv:
            return max(
                (
                    int((frame.get("params") or {}).get("seq") or 0)
                    for frame in self._events
                    if (frame.get("params") or {}).get("session_id") == session_id
                ),
                default=0,
            )

    def wait_session_event(
        self,
        session_id: str,
        *,
        accepted_types: set[str],
        deadline: float,
        after_seq: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        """Wait for the next matching event, retaining unrelated frames."""

        with self._events_cv:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return None
                for frame in list(self._events):
                    params = frame.get("params") or {}
                    seq = int(params.get("seq") or 0)
                    if seq and seq <= after_seq:
                        continue
                    if params.get("session_id") != session_id:
                        continue
                    if params.get("type") not in accepted_types:
                        continue
                    with contextlib.suppress(ValueError):
                        self._events.remove(frame)
                    return frame
                process = self._process
                if process is None or process.poll() is not None:
                    raise RpcError("profile worker exited during a member turn")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._events_cv.wait(timeout=min(remaining, 1.0))

    def wake_event_waiters(self) -> None:
        """Wake blocked event scans so a cancelled turn can join its waiter."""

        with self._events_cv:
            self._events_cv.notify_all()

    def close(self) -> None:
        self._closed = True
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    process.kill()


BlockedCallback = Callable[[PendingPrompt], Awaitable[None]]


class ProfileWorkerExecutor:
    """One persistent TUI worker and one serialized turn lane per profile."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or get_default_hermes_root())
        self._room_store = RoomStore(self.root)
        self._workers: dict[str, JsonRpcWorker] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._runtime_sessions: dict[tuple[str, str, str], str] = {}
        self._stored_sessions: dict[tuple[str, str, str], str] = {}
        self._guard = threading.Lock()

    def _worker(self, profile: str) -> JsonRpcWorker:
        with self._guard:
            worker = self._workers.get(profile)
            if worker is None:
                home = profile_home(self.root, profile)
                if not home.is_dir():
                    raise RpcError(f"Hermes profile {profile!r} is not installed")
                worker = JsonRpcWorker(home)
                self._workers[profile] = worker
            return worker

    def _lock(self, profile: str) -> asyncio.Lock:
        lock = self._turn_locks.get(profile)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[profile] = lock
        return lock

    async def _call(
        self,
        worker: JsonRpcWorker,
        method: str,
        params: dict[str, Any],
        timeout: float = 60,
    ) -> Any:
        call_task = asyncio.create_task(asyncio.to_thread(worker.call, method, params, timeout))
        try:
            return await asyncio.shield(call_task)
        except asyncio.CancelledError:
            # A cancelled asyncio.to_thread does not cancel its OS thread.
            # Join the unique RPC waiter before releasing the profile lock so
            # no old prompt can be accepted after its successor starts.
            result = None
            with contextlib.suppress(Exception):
                result = await asyncio.shield(call_task)
            if method in {"session.create", "session.resume", "prompt.submit"}:
                returned_runtime = str(
                    (result or {}).get("session_id") if isinstance(result, dict) else ""
                )
                runtime = (
                    returned_runtime
                    if method in {"session.create", "session.resume"}
                    else str(params.get("session_id") or "")
                )
                if runtime:
                    interrupt_task = asyncio.create_task(
                        asyncio.to_thread(
                            worker.call,
                            "session.interrupt",
                            {"session_id": runtime},
                            30,
                        )
                    )
                    with contextlib.suppress(Exception):
                        await asyncio.shield(interrupt_task)
            raise

    @staticmethod
    async def _store_call(method: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a room-ledger operation without abandoning its DB thread."""

        task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
            raise

    async def ensure_session(
        self,
        room: BotRoomConfig,
        member: BotRoomMember,
        thread_id: str,
        stored_hint: str = "",
    ) -> tuple[JsonRpcWorker, str, str, dict[str, Any]]:
        worker = self._worker(member.profile)
        instance_thread_id = thread_id if room.platform == "discord" else ""
        key = (room.room_id, instance_thread_id, member.key)
        runtime = self._runtime_sessions.get(key, "")
        if runtime:
            try:
                await self._call(worker, "session.status", {"session_id": runtime}, 10)
                return (
                    worker,
                    runtime,
                    self._stored_sessions.get(key, stored_hint),
                    {},
                )
            except RpcError as exc:
                if exc.code not in {4001, 4007}:
                    raise
                self._runtime_sessions.pop(key, None)
        title = (
            f"Group: {room.room_id} / Discord {thread_id}"
            if room.platform == "discord"
            else f"Group: {room.room_id}"
        )
        session_source = _room_session_source(room, member, thread_id)
        target = stored_hint or self._stored_sessions.get(key, "")
        if not target:
            listed = await self._call(worker, "session.list", {"title": title}, 30)
            sessions = [
                row
                for row in ((listed or {}).get("sessions") or [])
                if str(row.get("source") or "") == session_source
            ]
            if sessions:
                row = sessions[0]
                target = str(row.get("resolved_id") or row.get("id") or row.get("session_id") or "")
        result: dict[str, Any]
        if target:
            try:
                result = await self._call(
                    worker,
                    "session.resume",
                    {
                        "session_id": target,
                        "source": "bot_room",
                        "omit_messages": True,
                        "system_prompt_append": room_system_instructions(),
                        "event_subscriptions": ["tool_lifecycle"],
                    },
                    90,
                )
            except RpcError as exc:
                if exc.code not in {4001, 4007}:
                    raise
                target = ""
        if not target:
            result = await self._call(
                worker,
                "session.create",
                {
                    "title": title,
                    "source": session_source,
                    "hidden": True,
                    "close_on_disconnect": False,
                    "system_prompt_append": room_system_instructions(),
                    "event_subscriptions": ["tool_lifecycle"],
                },
                30,
            )
        runtime = str(result.get("session_id") or "")
        stored = str(
            result.get("stored_session_id")
            or result.get("session_key")
            or result.get("resumed")
            or target
            or ""
        )
        if not runtime or not stored:
            raise RpcError("profile worker returned an incomplete group session identity")
        self._runtime_sessions[key] = runtime
        self._stored_sessions[key] = stored
        return worker, runtime, stored, result

    async def _attach(
        self,
        worker: JsonRpcWorker,
        runtime_session_id: str,
        attachments: list[RoomAttachment],
    ) -> list[str]:
        file_refs: list[str] = []
        for attachment in attachments:
            params = {
                "session_id": runtime_session_id,
                "path": attachment.path,
                "name": attachment.name,
            }
            if attachment.kind == "image":
                method = "image.attach"
            elif attachment.kind == "pdf":
                method = "pdf.attach"
            else:
                method = "file.attach"
            result = await self._call(worker, method, params, 180 if method == "pdf.attach" else 60)
            if method == "file.attach" and result.get("ref_text"):
                file_refs.append(str(result["ref_text"]))
        return file_refs

    async def _session_history(
        self, worker: JsonRpcWorker, runtime_session_id: str
    ) -> list[dict[str, Any]]:
        result = await self._call(
            worker,
            "session.history",
            {"session_id": runtime_session_id},
            30,
        )
        return [item for item in ((result or {}).get("messages") or []) if isinstance(item, dict)]

    async def _reconcile_recovered_attempt(
        self,
        *,
        worker: JsonRpcWorker,
        runtime: str,
        stored: str,
        member: BotRoomMember,
        run_id: str,
        attempt: dict[str, Any],
    ) -> MemberTurnResult | None:
        phase = str(attempt.get("phase") or "")
        if phase == "terminal":
            return MemberTurnResult(
                text=str(attempt.get("result_text") or ""),
                status=str(attempt.get("result_status") or "complete"),
                error=str(attempt.get("result_error") or ""),
                runtime_session_id=runtime,
                stored_session_id=stored,
            )

        from tui_gateway.turn_marker import read_turn_marker

        marker_home = profile_home(self.root, member.profile)
        marker = None
        for marker_key in dict.fromkeys(
            (
                stored,
                str(attempt.get("stored_session_id") or ""),
            )
        ):
            if marker_key:
                marker = await asyncio.to_thread(read_turn_marker, marker_home, marker_key)
                if marker is not None:
                    break
        if marker is not None:
            # A surviving marker with no native auto_continue descriptor means
            # recovery is disabled, stale, or exhausted. Never replay the raw
            # prompt and its possible tool side effects.
            return MemberTurnResult(
                status="error",
                error="interrupted native turn requires manual recovery",
                runtime_session_id=runtime,
                stored_session_id=stored,
            )

        history = await self._session_history(worker, runtime)
        baseline = int(attempt.get("baseline_row_id") or 0)
        new_messages = [item for item in history if _history_row_id(item) > baseline]
        if new_messages or phase == "dispatched":
            # History alone cannot prove that a turn finished: Hermes may
            # durably flush an assistant tool-call row while its tool loop is
            # still running. Only the terminal Bot Mode ledger above is
            # authoritative. Lose one uncertain reply instead of publishing
            # partial text or replaying tool side effects.
            return MemberTurnResult(
                status="error",
                error="native turn outcome is ambiguous; prompt was not replayed",
                runtime_session_id=runtime,
                stored_session_id=stored,
            )
        return None

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
        on_blocked: BlockedCallback | None = None,
        recovering: bool = False,
    ) -> MemberTurnResult:
        async with self._lock(member.profile):
            attempt = (
                await self._store_call(self._room_store.turn_attempt, run_id, member.key)
                if recovering
                else None
            )
            resume_hint = (
                str(attempt.get("stored_session_id") or "") if attempt is not None else ""
            ) or stored_session_id
            worker, runtime, stored, session_result = await self.ensure_session(
                room, member, thread_id, resume_hint
            )
            auto_continuing = bool(recovering and session_result.get("auto_continue"))
            baseline_row_id = int((attempt or {}).get("baseline_row_id") or 0)
            if recovering and attempt is not None and not auto_continuing:
                reconciled = await self._reconcile_recovered_attempt(
                    worker=worker,
                    runtime=runtime,
                    stored=stored,
                    member=member,
                    run_id=run_id,
                    attempt=attempt,
                )
                if reconciled is not None:
                    return reconciled
            if auto_continuing:
                # session.resume found the durable crash marker and restarted
                # the interrupted prompt itself. Listen from sequence zero so
                # a very fast completion emitted during resume is not skipped.
                cursor = 0
            else:
                history = await self._session_history(worker, runtime)
                baseline_row_id = max((_history_row_id(item) for item in history), default=0)
                await self._store_call(
                    self._room_store.save_turn_attempt,
                    run_id=run_id,
                    room_id=room.room_id,
                    thread_id=thread_id,
                    member_key=member.key,
                    runtime_session_id=runtime,
                    stored_session_id=stored,
                    baseline_row_id=baseline_row_id,
                )
                refs = await self._attach(worker, runtime, attachments)
                if refs:
                    prompt = f"{prompt}\n\nAttached files available to your tools: {' '.join(refs)}"
                cursor = worker.event_cursor(runtime)
                # Write ahead of native acceptance. A crash after this point
                # is ambiguous and must fail closed on recovery; writing the
                # phase after prompt.submit would leave a window where an
                # accepted tool-bearing turn still looked safe to replay.
                await self._store_call(self._room_store.mark_turn_dispatched, run_id, member.key)
                await self._call(
                    worker,
                    "prompt.submit",
                    {"session_id": runtime, "text": prompt},
                    60,
                )
            hard_deadline = time.monotonic() + HARD_TURN_TIMEOUT_SECONDS
            activity_deadline = time.monotonic() + BASE_TURN_TIMEOUT_SECONDS
            active_tools: set[str] = set()

            async def _interrupt_timed_out_turn() -> None:
                with contextlib.suppress(RpcError):
                    await self._call(worker, "session.interrupt", {"session_id": runtime}, 15)

            accepted = {
                "message.complete",
                "clarify.request",
                "approval.request",
                "error",
                "status.update",
                "message.start",
                "tool.start",
                "tool.complete",
            }
            while time.monotonic() < hard_deadline:
                cancel_wait = threading.Event()
                wait_task = asyncio.create_task(
                    asyncio.to_thread(
                        worker.wait_session_event,
                        runtime,
                        accepted_types=accepted,
                        deadline=(
                            hard_deadline if active_tools else min(activity_deadline, hard_deadline)
                        ),
                        after_seq=cursor,
                        cancel_event=cancel_wait,
                    )
                )
                try:
                    # Shield the thread bridge so cancellation can explicitly
                    # stop and join the underlying deque consumer. Abandoning
                    # it would let it steal the successor turn's terminal event.
                    event = await asyncio.shield(wait_task)
                except asyncio.CancelledError:
                    cancel_wait.set()
                    worker.wake_event_waiters()
                    with contextlib.suppress(Exception):
                        await asyncio.shield(wait_task)
                    raise
                if event is None:
                    hard_limit_hit = time.monotonic() >= hard_deadline
                    await _interrupt_timed_out_turn()
                    return MemberTurnResult(
                        status="timeout",
                        error=(
                            "member turn stopped after the "
                            f"{HARD_TURN_TIMEOUT_SECONDS / 60:g}-minute hard limit"
                            if hard_limit_hit
                            else (
                                "member turn stopped after "
                                f"{BASE_TURN_TIMEOUT_SECONDS:g} seconds without "
                                "new model or tool activity"
                            )
                        ),
                        runtime_session_id=runtime,
                        stored_session_id=stored,
                    )
                params = event.get("params") or {}
                cursor = max(cursor, int(params.get("seq") or 0))
                event_type = str(params.get("type") or "")
                payload = params.get("payload") or {}
                if event_type in {"clarify.request", "approval.request"}:
                    request_id = str(payload.get("request_id") or "")
                    pending = PendingPrompt(
                        room_id=room.room_id,
                        run_id=run_id,
                        thread_id=thread_id,
                        member_key=member.key,
                        kind="clarify" if event_type == "clarify.request" else "approval",
                        request_id=request_id,
                        payload=dict(payload),
                        runtime_session_id=runtime,
                    )
                    if on_blocked is None:
                        with contextlib.suppress(RpcError):
                            await self._call(
                                worker,
                                "approval.respond"
                                if pending.kind == "approval"
                                else "clarify.respond",
                                {
                                    "session_id": runtime,
                                    "request_id": request_id,
                                    **(
                                        {"choice": "deny"}
                                        if pending.kind == "approval"
                                        else {"answer": "Unable to ask the user in this client."}
                                    ),
                                },
                                15,
                            )
                    else:
                        await on_blocked(pending)
                    # A human-blocking prompt is legitimate work, not model
                    # silence. Give it the native hard cap; the first event
                    # after the answer restores the rolling base timeout.
                    activity_deadline = hard_deadline
                    continue
                if event_type == "message.complete":
                    text = str(payload.get("text") or "").strip()
                    status = str(payload.get("status") or "complete")
                    error = str(payload.get("error") or "")
                    try:
                        history = await self._session_history(worker, runtime)
                        selected = _pick_turn_reply(history, baseline_row_id)
                        if selected is not None:
                            text = selected
                    except Exception:
                        logger.warning(
                            "Bot Mode could not refresh terminal history; using event text",
                            exc_info=True,
                        )
                    await self._store_call(
                        self._room_store.save_turn_result,
                        run_id,
                        member.key,
                        text=text,
                        status=status,
                        error=error,
                    )
                    return MemberTurnResult(
                        text=text,
                        status=status,
                        error=error,
                        runtime_session_id=runtime,
                        stored_session_id=stored,
                    )
                if event_type == "error":
                    error = str(payload.get("message") or "member turn failed")
                    await self._store_call(
                        self._room_store.save_turn_result,
                        run_id,
                        member.key,
                        text="",
                        status="error",
                        error=error,
                    )
                    return MemberTurnResult(
                        status="error",
                        error=error,
                        runtime_session_id=runtime,
                        stored_session_id=stored,
                    )
                if event_type == "tool.start":
                    tool_id = str(payload.get("tool_id") or f"seq:{cursor}")
                    active_tools.add(tool_id)
                    activity_deadline = hard_deadline
                    continue
                if event_type == "tool.complete":
                    tool_id = str(payload.get("tool_id") or "")
                    if tool_id:
                        active_tools.discard(tool_id)
                activity_deadline = (
                    hard_deadline
                    if active_tools
                    else min(
                        hard_deadline,
                        time.monotonic() + BASE_TURN_TIMEOUT_SECONDS,
                    )
                )
            await _interrupt_timed_out_turn()
            return MemberTurnResult(
                status="timeout",
                error=(
                    "member turn stopped after the "
                    f"{HARD_TURN_TIMEOUT_SECONDS / 60:g}-minute hard limit"
                ),
                runtime_session_id=runtime,
                stored_session_id=stored,
            )

    async def respond(self, prompt: PendingPrompt, value: str) -> bool:
        member_profile = prompt.member_key.split("::")[-1]
        worker = self._worker(member_profile)
        if prompt.kind == "approval":
            result = await self._call(
                worker,
                "approval.respond",
                {
                    "session_id": prompt.runtime_session_id,
                    "request_id": prompt.request_id,
                    "choice": value,
                },
                30,
            )
        else:
            result = await self._call(
                worker,
                "clarify.respond",
                {
                    "session_id": prompt.runtime_session_id,
                    "request_id": prompt.request_id,
                    "answer": value,
                },
                30,
            )
        return bool((result or {}).get("resolved", True))

    async def interrupt(self, profile: str, runtime_session_id: str) -> None:
        worker = self._worker(profile)
        await self._call(worker, "session.interrupt", {"session_id": runtime_session_id}, 30)

    async def interrupt_room_member(
        self, room_id: str, thread_id: str, member: BotRoomMember
    ) -> bool:
        runtime = self._runtime_sessions.get((room_id, thread_id, member.key), "")
        if not runtime:
            runtime = self._runtime_sessions.get((room_id, "", member.key), "")
        if not runtime:
            return False
        await self.interrupt(member.profile, runtime)
        return True

    def close(self) -> None:
        for worker in list(self._workers.values()):
            worker.close()
        self._workers.clear()
