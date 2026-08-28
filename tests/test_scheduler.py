from collections import deque
from pathlib import Path

import pytest

from hermes_discord_botrooms.bot_mode.config import BotRoomConfig, BotRoomMember
from hermes_discord_botrooms.bot_mode.models import MemberTurnResult
from hermes_discord_botrooms.bot_mode.scheduler import RoomRunEngine
from hermes_discord_botrooms.bot_mode.store import RoomStore


class FakeExecutor:
    def __init__(self, replies):
        self.replies = {key: deque(value) for key, value in replies.items()}
        self.calls = []

    async def turn(
        self,
        room,
        member,
        prompt,
        attachments,
        *,
        run_id,
        thread_id,
        stored_session_id="",
        on_blocked=None,
        recovering=False,
    ):
        self.calls.append((member.profile, prompt, stored_session_id, recovering))
        reply = self.replies[member.profile].popleft()
        return MemberTurnResult(
            text=reply,
            stored_session_id=stored_session_id or f"session-{member.profile}",
        )


def _room() -> BotRoomConfig:
    return BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="desktop",
        channel_id="",
        controller_profile="researcher",
        members=(BotRoomMember("researcher"), BotRoomMember("coder")),
    )


async def _run(tmp_path: Path, text: str, replies):
    store = RoomStore(tmp_path)
    room = _room()
    submitted = store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="user:1",
        text=text,
        author_id="user",
        author_name="Daniel",
    )
    events = []

    async def sink(event):
        events.append(event)

    executor = FakeExecutor(replies)
    engine = RoomRunEngine(store, executor, event_sink=sink)
    await engine.run(room, "thread", submitted.run_id)
    return store, executor, events, submitted


@pytest.mark.asyncio
async def test_scheduler_runs_serially_hides_pass_and_reuses_member_sessions(tmp_path):
    store, executor, events, submitted = await _run(
        tmp_path,
        "Decide the architecture",
        {
            "researcher": ["Approach B is best.", "(pass)"],
            "coder": ["(pass)", "pass"],
        },
    )
    assert [call[0] for call in executor.calls] == ["researcher", "coder"]
    log = store.thread_events("agents", "thread")
    assert [event.text for event in log] == [
        "Decide the architecture",
        "Approach B is best.",
    ]
    assert store.session_id("agents", "thread", "researcher") == "session-researcher"
    assert store.run_row(submitted.run_id)["status"] == "settled"
    assert [event["kind"] for event in events].count("message") == 1


@pytest.mark.asyncio
async def test_agent_mention_pulls_teammate_into_next_round(tmp_path):
    store, executor, _events, _submitted = await _run(
        tmp_path,
        "@researcher investigate",
        {
            "researcher": ["I found the seam. @coder validate it.", "(pass)", "(pass)"],
            "coder": ["The seam is valid.", "(pass)"],
        },
    )
    assert [call[0] for call in executor.calls] == [
        "researcher",
        "coder",
        "researcher",
    ]
    assert [event.author_id for event in store.thread_events("agents", "thread")] == [
        "user",
        "researcher",
        "coder",
    ]


@pytest.mark.asyncio
async def test_explicit_member_mention_limits_the_first_round(tmp_path):
    _store, executor, _events, _submitted = await _run(
        tmp_path,
        "@coder check this",
        {"researcher": [], "coder": ["(pass)"]},
    )
    assert [call[0] for call in executor.calls] == ["coder"]


@pytest.mark.asyncio
async def test_round_exhaustion_is_reported_as_capped_not_settled(tmp_path):
    store, _executor, _events, submitted = await _run(
        tmp_path,
        "keep discussing",
        {
            "researcher": [f"research {index}" for index in range(10)],
            "coder": [f"code {index}" for index in range(10)],
        },
    )
    assert store.run_row(submitted.run_id)["status"] == "capped"


@pytest.mark.asyncio
async def test_empty_terminal_sentinel_becomes_a_friendly_room_error(tmp_path):
    store, _executor, _events, _submitted = await _run(
        tmp_path,
        "answer this",
        {
            "researcher": ["(empty)", "(pass)"],
            "coder": ["(pass)", "(pass)"],
        },
    )
    assert store.thread_events("agents", "thread")[1].text.startswith(
        "⚠️ The model returned no response"
    )


@pytest.mark.asyncio
async def test_stop_during_member_turn_drops_the_racing_reply_and_holds_members(tmp_path):
    store = RoomStore(tmp_path)
    room = _room()
    submitted = store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="user:stop-race",
        text="start",
        author_id="user",
        author_name="Daniel",
    )

    class StoppingExecutor:
        async def turn(self, _room, _member, _prompt, _attachments, **_kwargs):
            store.request_stop("agents", "thread", [member.key for member in room.members])
            return MemberTurnResult(text="too late")

    await RoomRunEngine(store, StoppingExecutor()).run(room, "thread", submitted.run_id)
    assert [event.text for event in store.thread_events("agents", "thread")] == ["start"]
    assert store.run_row(submitted.run_id)["status"] == "stopped"
    assert store.holds("agents", "thread") == {"researcher", "coder"}


@pytest.mark.asyncio
async def test_quiet_round_drives_an_unanswered_member_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.scheduler.resolve_responders",
        lambda _log, members: [members[0]],
    )
    store, executor, _events, submitted = await _run(
        tmp_path,
        "investigate",
        {
            "researcher": ["I need @coder to verify.", "(pass)"],
            "coder": ["Verified."],
        },
    )
    assert [call[0] for call in executor.calls] == ["researcher", "coder", "researcher"]
    assert [event.text for event in store.thread_events("agents", "thread")] == [
        "investigate",
        "I need @coder to verify.",
        "Verified.",
    ]
    assert store.run_row(submitted.run_id)["status"] == "settled"


@pytest.mark.asyncio
async def test_stop_at_final_commit_boundary_cannot_publish_the_reply(tmp_path, monkeypatch):
    store = RoomStore(tmp_path)
    room = _room()
    submitted = store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="user:atomic-stop",
        text="start",
        author_id="user",
        author_name="Daniel",
    )
    original_commit = store.commit_agent_event
    stopped = False

    def stop_then_commit(**kwargs):
        nonlocal stopped
        if not stopped:
            stopped = True
            store.request_stop("agents", "thread", [member.key for member in room.members])
        return original_commit(**kwargs)

    monkeypatch.setattr(store, "commit_agent_event", stop_then_commit)
    executor = FakeExecutor({"researcher": ["racing reply"], "coder": ["(pass)"]})
    events = []

    async def sink(event):
        events.append(event)

    await RoomRunEngine(store, executor, event_sink=sink).run(
        room, "thread", submitted.run_id
    )
    assert [event.text for event in store.thread_events("agents", "thread")] == ["start"]
    assert not any(event["kind"] == "message" for event in events)
    assert store.run_row(submitted.run_id)["status"] == "stopped"


@pytest.mark.asyncio
async def test_recovered_idempotent_event_is_not_counted_or_emitted_twice(tmp_path):
    store = RoomStore(tmp_path)
    room = _room()
    submitted = store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="user:idempotent-recovery",
        text="start",
        author_id="user",
        author_name="Daniel",
    )
    event_uid = f"agent:{submitted.run_id}:0:researcher"
    store.append_agent_event(
        room_id="agents",
        thread_id="thread",
        run_id=submitted.run_id,
        member_key="researcher",
        member_name="researcher",
        text="durable answer",
        event_uid=event_uid,
        metadata={"profile": "researcher"},
    )
    events = []

    async def sink(event):
        events.append(event)

    executor = FakeExecutor(
        {"researcher": ["durable answer", "(pass)"], "coder": ["(pass)", "(pass)"]}
    )
    await RoomRunEngine(store, executor, event_sink=sink).run(
        room, "thread", submitted.run_id, recovering=True
    )
    assert store.run_row(submitted.run_id)["message_count"] == 1
    assert not any(event["kind"] == "message" for event in events)
    finished = next(event for event in events if event["kind"] == "run.finished")
    assert finished["message_count"] == 1
