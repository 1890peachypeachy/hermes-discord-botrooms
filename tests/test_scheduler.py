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
    assert store.session_id("agents", "researcher") == "session-researcher"
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
