import asyncio
import base64
import threading
import time
from pathlib import Path

import pytest

from hermes_discord_botrooms.bot_mode.attachments import stage_data_attachment
from hermes_discord_botrooms.bot_mode.config import BotRoomConfig, BotRoomMember
from hermes_discord_botrooms.bot_mode.models import RoomAttachment
from hermes_discord_botrooms.bot_mode.profile_worker import (
    JsonRpcWorker,
    ProfileWorkerExecutor,
    _room_session_source,
)


def test_data_attachment_accepts_wrapped_base64_and_sanitizes_the_name(tmp_path: Path):
    encoded = base64.b64encode(b"hello room").decode("ascii")
    wrapped = f"{encoded[:4]}\n{encoded[4:]}"
    attachment = stage_data_attachment(
        tmp_path,
        data_url=f"data:text/plain;base64,{wrapped}",
        name="../unsafe?.txt",
        kind="file",
    )
    path = Path(attachment.path)
    assert path.parent == (tmp_path / "plugin-data" / "hermes-discord-botrooms" / "attachments")
    assert path.read_bytes() == b"hello room"
    assert attachment.name == "unsafe_.txt"


@pytest.mark.asyncio
async def test_worker_routes_each_attachment_kind_to_its_native_rpc(tmp_path: Path):
    executor = ProfileWorkerExecutor(tmp_path)
    calls = []

    async def fake_call(_worker, method, params, timeout=60):
        calls.append((method, params["path"], timeout))
        return {"ref_text": "@file:notes.txt"} if method == "file.attach" else {}

    executor._call = fake_call
    refs = await executor._attach(
        object(),
        "runtime",
        [
            RoomAttachment("/tmp/a.png", "a.png", "image", "image/png"),
            RoomAttachment("/tmp/a.pdf", "a.pdf", "pdf", "application/pdf"),
            RoomAttachment("/tmp/a.txt", "a.txt", "file", "text/plain"),
        ],
    )
    assert calls == [
        ("image.attach", "/tmp/a.png", 60),
        ("pdf.attach", "/tmp/a.pdf", 180),
        ("file.attach", "/tmp/a.txt", 60),
    ]
    assert refs == ["@file:notes.txt"]


@pytest.mark.asyncio
async def test_group_session_title_collision_does_not_adopt_an_unrelated_session(
    tmp_path: Path,
):
    (tmp_path / "profiles" / "coder").mkdir(parents=True)
    executor = ProfileWorkerExecutor(tmp_path)
    worker = object()
    executor._workers["coder"] = worker
    room = BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="desktop",
        channel_id="",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )
    member = room.members[1]
    calls = []

    async def fake_call(_worker, method, params, timeout=60):
        calls.append((method, params))
        if method == "session.list":
            return {
                "sessions": [
                    {
                        "id": "private-session",
                        "resolved_id": "private-session",
                        "title": "Group: agents",
                        "source": "discord",
                    }
                ]
            }
        if method == "session.create":
            return {"session_id": "runtime", "stored_session_id": "stored"}
        raise AssertionError(method)

    executor._call = fake_call
    _worker, runtime, stored, _result = await executor.ensure_session(room, member)

    assert (runtime, stored) == ("runtime", "stored")
    assert [method for method, _params in calls] == ["session.list", "session.create"]
    assert calls[-1][1]["source"] == _room_session_source(room, member)
    assert calls[-1][1]["event_subscriptions"] == ["tool_lifecycle"]
    assert "Agent Inbox" in calls[-1][1]["system_prompt_append"]


@pytest.mark.asyncio
async def test_resumed_room_session_reapplies_room_only_instructions(tmp_path: Path):
    (tmp_path / "profiles" / "coder").mkdir(parents=True)
    executor = ProfileWorkerExecutor(tmp_path)
    worker = object()
    executor._workers["coder"] = worker
    room = BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="desktop",
        channel_id="",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )
    member = room.members[1]
    calls = []

    async def fake_call(_worker, method, params, timeout=60):
        calls.append((method, params))
        if method == "session.list":
            return {
                "sessions": [
                    {
                        "id": "stored",
                        "resolved_id": "stored",
                        "title": "Group: agents",
                        "source": _room_session_source(room, member),
                    }
                ]
            }
        if method == "session.resume":
            return {"session_id": "runtime", "resumed": "stored"}
        raise AssertionError(method)

    executor._call = fake_call
    _worker, runtime, stored, _result = await executor.ensure_session(room, member)

    assert (runtime, stored) == ("runtime", "stored")
    assert [method for method, _params in calls] == ["session.list", "session.resume"]
    assert calls[-1][1]["event_subscriptions"] == ["tool_lifecycle"]
    assert "Agent Inbox" in calls[-1][1]["system_prompt_append"]


@pytest.mark.asyncio
async def test_recovery_listens_to_native_auto_continue_without_resubmitting(
    tmp_path: Path,
):
    executor = ProfileWorkerExecutor(tmp_path)
    room = BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="desktop",
        channel_id="",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )
    member = room.members[0]
    calls = []

    class AutoContinueWorker:
        def event_cursor(self, _runtime):
            raise AssertionError("recovery must listen from sequence zero")

        def wait_session_event(
            self,
            _runtime,
            *,
            accepted_types,
            deadline,
            after_seq,
            cancel_event=None,
        ):
            assert cancel_event is not None
            assert "message.complete" in accepted_types
            assert deadline > time.monotonic()
            assert after_seq == 0
            return {
                "params": {
                    "seq": 1,
                    "type": "message.complete",
                    "payload": {"text": "resumed result", "status": "complete"},
                }
            }

        def wake_event_waiters(self):
            pass

    worker = AutoContinueWorker()

    async def fake_ensure_session(_room, _member, _stored_hint=""):
        return worker, "runtime", "stored", {"auto_continue": {"attempt": 1}}

    async def fake_call(_worker, method, params, timeout=60):
        calls.append((method, params, timeout))
        return {}

    executor.ensure_session = fake_ensure_session
    executor._call = fake_call
    result = await executor.turn(
        room,
        member,
        "do not resubmit this prompt",
        [],
        run_id="run",
        thread_id="thread",
        recovering=True,
    )

    assert result.text == "resumed result"
    assert not any(method == "prompt.submit" for method, _params, _timeout in calls)


@pytest.mark.asyncio
async def test_cancelling_member_turn_joins_its_blocking_event_waiter(tmp_path: Path):
    class LiveProcess:
        @staticmethod
        def poll():
            return None

    class WaitingWorker(JsonRpcWorker):
        def __init__(self):
            super().__init__(tmp_path)
            self._process = LiveProcess()
            self.waiter_started = threading.Event()
            self.waiter_exited = threading.Event()

        def wait_session_event(self, *args, **kwargs):
            self.waiter_started.set()
            try:
                return super().wait_session_event(*args, **kwargs)
            finally:
                self.waiter_exited.set()

    executor = ProfileWorkerExecutor(tmp_path)
    worker = WaitingWorker()
    room = BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="desktop",
        channel_id="",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )

    async def fake_ensure_session(_room, _member, _stored_hint=""):
        return worker, "runtime", "stored", {}

    async def fake_call(_worker, method, _params, timeout=60):
        if method == "session.history":
            return {"messages": []}
        assert method == "prompt.submit"
        assert (
            executor._room_store.turn_attempt(submitted.run_id, room.members[0].key)["phase"]
            == "dispatched"
        )
        return {}

    executor.ensure_session = fake_ensure_session
    executor._call = fake_call
    submitted = executor._room_store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="desktop:cancel-waiter",
        text="first prompt",
        author_id="user",
        author_name="You",
    )
    task = asyncio.create_task(
        executor.turn(
            room,
            room.members[0],
            "first prompt",
            [],
            run_id=submitted.run_id,
            thread_id="thread",
        )
    )
    assert await asyncio.to_thread(worker.waiter_started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.waiter_exited.is_set()


@pytest.mark.asyncio
async def test_cancelling_prompt_rpc_joins_then_interrupts_native_runtime(
    tmp_path: Path,
):
    executor = ProfileWorkerExecutor(tmp_path)

    class BlockingWorker:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = []

        def call(self, method, params, timeout):
            self.calls.append((method, params, timeout))
            if method == "prompt.submit":
                self.started.set()
                assert self.release.wait(2)
                return {"accepted": True}
            assert method == "session.interrupt"
            return {"interrupted": True}

    worker = BlockingWorker()
    task = asyncio.create_task(
        executor._call(
            worker,
            "prompt.submit",
            {"session_id": "runtime", "text": "old prompt"},
            60,
        )
    )
    assert await asyncio.to_thread(worker.started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    worker.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call[0] for call in worker.calls] == [
        "prompt.submit",
        "session.interrupt",
    ]


@pytest.mark.asyncio
async def test_cancelling_resume_interrupts_returned_runtime_not_stored_key(
    tmp_path: Path,
):
    executor = ProfileWorkerExecutor(tmp_path)

    class BlockingWorker:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = []

        def call(self, method, params, timeout):
            self.calls.append((method, params, timeout))
            if method == "session.resume":
                self.started.set()
                assert self.release.wait(2)
                return {"session_id": "runtime", "resumed": "stored"}
            assert method == "session.interrupt"
            assert params["session_id"] == "runtime"
            return {"interrupted": True}

    worker = BlockingWorker()
    task = asyncio.create_task(
        executor._call(
            worker,
            "session.resume",
            {"session_id": "stored", "omit_messages": True},
            90,
        )
    )
    assert await asyncio.to_thread(worker.started.wait, 2)
    task.cancel()
    worker.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call[0] for call in worker.calls] == [
        "session.resume",
        "session.interrupt",
    ]


@pytest.mark.asyncio
async def test_recovery_does_not_publish_assistant_history_without_terminal_ledger(
    tmp_path: Path,
):
    executor = ProfileWorkerExecutor(tmp_path)
    room = BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="desktop",
        channel_id="",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )
    member = room.members[0]
    submitted = executor._room_store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="desktop:completed-before-crash",
        text="do the work",
        author_id="user",
        author_name="You",
    )
    executor._room_store.save_turn_attempt(
        run_id=submitted.run_id,
        room_id=room.room_id,
        thread_id="thread",
        member_key=member.key,
        runtime_session_id="old-runtime",
        stored_session_id="stored",
        baseline_row_id=10,
    )
    executor._room_store.mark_turn_dispatched(submitted.run_id, member.key)
    calls = []

    async def fake_ensure_session(_room, _member, _stored_hint=""):
        assert _stored_hint == "stored"
        return object(), "runtime", "stored", {}

    async def fake_call(_worker, method, _params, timeout=60):
        calls.append(method)
        assert method == "session.history"
        return {
            "messages": [
                {"role": "user", "text": "durable prompt", "row_id": 11},
                {"role": "assistant", "text": "durable answer", "row_id": 12},
            ]
        }

    executor.ensure_session = fake_ensure_session
    executor._call = fake_call
    result = await executor.turn(
        room,
        member,
        "must not run again",
        [],
        run_id=submitted.run_id,
        thread_id="thread",
        recovering=True,
    )

    assert result.status == "error"
    assert "ambiguous" in result.error
    assert calls == ["session.history"]
    assert executor._room_store.turn_attempt(submitted.run_id, member.key)["phase"] == "dispatched"


@pytest.mark.asyncio
async def test_recovery_fails_closed_for_accepted_turn_without_durable_outcome(
    tmp_path: Path,
):
    executor = ProfileWorkerExecutor(tmp_path)
    room = BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="desktop",
        channel_id="",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )
    member = room.members[0]
    submitted = executor._room_store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="desktop:ambiguous-before-crash",
        text="do the work",
        author_id="user",
        author_name="You",
    )
    executor._room_store.save_turn_attempt(
        run_id=submitted.run_id,
        room_id=room.room_id,
        thread_id="thread",
        member_key=member.key,
        runtime_session_id="old-runtime",
        stored_session_id="stored",
        baseline_row_id=10,
    )
    executor._room_store.mark_turn_dispatched(submitted.run_id, member.key)

    async def fake_ensure_session(_room, _member, _stored_hint=""):
        assert _stored_hint == "stored"
        return object(), "runtime", "stored", {}

    async def fake_call(_worker, method, _params, timeout=60):
        assert method == "session.history"
        return {"messages": []}

    executor.ensure_session = fake_ensure_session
    executor._call = fake_call
    result = await executor.turn(
        room,
        member,
        "must not run again",
        [],
        run_id=submitted.run_id,
        thread_id="thread",
        recovering=True,
    )

    assert result.status == "error"
    assert "not replayed" in result.error


@pytest.mark.asyncio
async def test_recovery_never_replays_a_native_marker_that_was_not_auto_continued(
    tmp_path: Path,
):
    from tui_gateway.turn_marker import record_turn_start

    executor = ProfileWorkerExecutor(tmp_path)
    room = BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="desktop",
        channel_id="",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )
    member = room.members[0]
    submitted = executor._room_store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="desktop:disabled-native-recovery",
        text="do the work",
        author_id="user",
        author_name="You",
    )
    executor._room_store.save_turn_attempt(
        run_id=submitted.run_id,
        room_id=room.room_id,
        thread_id="thread",
        member_key=member.key,
        runtime_session_id="old-runtime",
        stored_session_id="stored",
        baseline_row_id=10,
    )
    executor._room_store.mark_turn_dispatched(submitted.run_id, member.key)
    record_turn_start(tmp_path, "stored", "dangerous prompt", attempts=3)

    async def fake_ensure_session(_room, _member, _stored_hint=""):
        assert _stored_hint == "stored"
        return object(), "runtime", "stored", {}

    async def fake_call(*_args, **_kwargs):
        raise AssertionError("marker-present recovery must not read or resubmit")

    executor.ensure_session = fake_ensure_session
    executor._call = fake_call
    result = await executor.turn(
        room,
        member,
        "must not run again",
        [],
        run_id=submitted.run_id,
        thread_id="thread",
        recovering=True,
    )

    assert result.status == "error"
    assert "manual recovery" in result.error


class _ScriptedTurnWorker:
    def __init__(self, script):
        self.script = list(script)

    @staticmethod
    def event_cursor(_runtime):
        return 0

    @staticmethod
    def wake_event_waiters():
        return None

    def wait_session_event(
        self,
        _runtime,
        *,
        accepted_types,
        deadline,
        after_seq,
        cancel_event,
    ):
        assert accepted_types
        if not self.script:
            time.sleep(max(0, deadline - time.monotonic()))
            return None
        delay, event_type, payload = self.script.pop(0)
        time.sleep(delay)
        if cancel_event.is_set() or time.monotonic() >= deadline:
            return None
        return {
            "params": {
                "seq": after_seq + 1,
                "type": event_type,
                "payload": payload,
            }
        }


async def _run_scripted_turn(tmp_path, monkeypatch, script, *, hard=0.3):
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.profile_worker.BASE_TURN_TIMEOUT_SECONDS", 0.03
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.profile_worker.HARD_TURN_TIMEOUT_SECONDS", hard
    )
    executor = ProfileWorkerExecutor(tmp_path)
    worker = _ScriptedTurnWorker(script)
    room = BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="discord",
        channel_id="channel",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )
    member = room.members[0]
    submitted = executor._room_store.submit_user_event(
        room,
        thread_id="thread",
        event_uid=f"discord:timeout-{time.monotonic_ns()}",
        text="do the work",
        author_id="user",
        author_name="You",
    )
    calls = []

    async def ensure_session(_room, _member, _stored_hint=""):
        return worker, "runtime", "stored", {}

    async def call(_worker, method, _params, timeout=60):
        calls.append(method)
        if method == "session.history":
            return {"messages": []}
        return {}

    executor.ensure_session = ensure_session
    executor._call = call
    result = await executor.turn(
        room,
        member,
        "do the work",
        [],
        run_id=submitted.run_id,
        thread_id="thread",
    )
    return result, calls


@pytest.mark.asyncio
async def test_parallel_active_tools_pause_idle_timeout_until_all_complete(
    tmp_path: Path, monkeypatch
):
    result, calls = await _run_scripted_turn(
        tmp_path,
        monkeypatch,
        [
            (0, "tool.start", {"tool_id": "a"}),
            (0, "tool.start", {"tool_id": "b"}),
            (0, "tool.complete", {"tool_id": "a"}),
            (0.06, "tool.complete", {"tool_id": "b"}),
            (0, "message.complete", {"text": "finished", "status": "complete"}),
        ],
    )

    assert result.status == "complete"
    assert result.text == "finished"
    assert "session.interrupt" not in calls


@pytest.mark.asyncio
async def test_idle_turn_is_interrupted_with_a_clear_idle_error(tmp_path: Path, monkeypatch):
    result, calls = await _run_scripted_turn(tmp_path, monkeypatch, [])

    assert result.status == "timeout"
    assert "without new model or tool activity" in result.error
    assert calls[-1] == "session.interrupt"


@pytest.mark.asyncio
async def test_active_tool_still_obeys_the_hard_turn_limit(tmp_path: Path, monkeypatch):
    result, calls = await _run_scripted_turn(
        tmp_path,
        monkeypatch,
        [(0, "tool.start", {"tool_id": "long-tool"})],
        hard=0.08,
    )

    assert result.status == "timeout"
    assert "hard limit" in result.error
    assert calls[-1] == "session.interrupt"
