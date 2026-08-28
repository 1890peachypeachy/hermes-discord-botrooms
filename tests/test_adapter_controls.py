from __future__ import annotations

from types import SimpleNamespace

import pytest

import hermes_discord_botrooms.adapter as adapter_module
from hermes_discord_botrooms.adapter import BotRoomsDiscordAdapter
from hermes_discord_botrooms.bot_mode.config import (
    BotRoomConfig,
    BotRoomMember,
)


@pytest.mark.asyncio
async def test_connect_forwards_gateway_reconnect_flag(monkeypatch):
    calls = []

    async def connect(_self, *, is_reconnect=False):
        calls.append(is_reconnect)
        return True

    monkeypatch.setattr(adapter_module.base.DiscordAdapter, "connect", connect)
    instance = object.__new__(BotRoomsDiscordAdapter)
    instance._ensure_botrooms_subscription = lambda: calls.append("subscribed")

    assert await instance.connect(is_reconnect=True) is True
    assert calls == [True, "subscribed"]


@pytest.mark.asyncio
async def test_room_answer_restarts_typing_before_releasing_agent(monkeypatch):
    events = []
    room = BotRoomConfig(
        room_id="team",
        display_name="Team",
        platform="discord",
        guild_id="111",
        channel_id="222",
        controller_profile="planner",
        members=(BotRoomMember("planner"), BotRoomMember("builder")),
    )

    class FakeThread:
        id = 333

    class FakeTransport:
        async def start_typing(self, *, profile, thread_id):
            events.append(("typing", profile, thread_id))

        async def stop_typing(self, *, profile, thread_id):
            events.append(("stop", profile, thread_id))

    class FakeService:
        async def respond(self, room_id, thread_id, member_key, value):
            events.append(("respond", room_id, thread_id, member_key, value))
            return {"resolved": True}

    class FakeResponse:
        async def send_message(self, text, *, ephemeral):
            events.append(("reply", text, ephemeral))

    instance = object.__new__(BotRoomsDiscordAdapter)
    instance._botrooms_service = FakeService()
    instance._botrooms_transport = FakeTransport()
    instance._room_for_discord_object = lambda _interaction: room
    instance._ensure_botrooms_subscription = lambda: None

    async def allowed(_interaction, _command):
        return True

    instance._check_slash_authorization = allowed
    monkeypatch.setattr(adapter_module, "current_profile_name", lambda: "planner")
    monkeypatch.setattr(adapter_module.discord, "Thread", FakeThread)
    interaction = SimpleNamespace(channel=FakeThread(), response=FakeResponse())

    handled = await instance._run_room_control(
        interaction,
        "room-answer",
        agent="builder",
        value="continue",
    )

    assert handled is True
    assert events[:2] == [
        ("typing", "builder", "333"),
        ("respond", "team", "333", "builder", "continue"),
    ]
    assert events[-1] == ("reply", "Response sent to builder.", True)


@pytest.mark.asyncio
async def test_parent_channel_refuses_mutating_room_controls(monkeypatch):
    events = []
    room = BotRoomConfig(
        room_id="team",
        display_name="Team",
        platform="discord",
        channel_id="222",
        controller_profile="planner",
        members=(BotRoomMember("planner"), BotRoomMember("builder")),
    )

    class ParentChannel:
        id = 222

    class FakeResponse:
        async def send_message(self, text, *, ephemeral):
            events.append((text, ephemeral))

    class FakeService:
        async def respond(self, *_args):
            raise AssertionError("a parent-channel control must not resolve a prompt")

    instance = object.__new__(BotRoomsDiscordAdapter)
    instance._botrooms_service = FakeService()
    instance._botrooms_transport = None
    instance._room_for_discord_object = lambda _interaction: room
    instance._ensure_botrooms_subscription = lambda: None

    async def allowed(_interaction, _command):
        return True

    instance._check_slash_authorization = allowed
    monkeypatch.setattr(adapter_module, "current_profile_name", lambda: "planner")
    interaction = SimpleNamespace(channel=ParentChannel(), response=FakeResponse())

    assert await instance._run_room_control(
        interaction,
        "room-answer",
        agent="builder",
        value="continue",
    )
    assert events == [
        ("Use `/room-answer` inside the Bot Room thread you want to control.", True)
    ]


def test_parent_room_status_summarizes_active_threads():
    text = BotRoomsDiscordAdapter._format_room_status(
        {
            "room_id": "team",
            "display_name": "Team",
            "aggregate": True,
            "active_threads": [
                {
                    "thread_id": "333",
                    "status": "running",
                    "current_member": "builder",
                },
                {"thread_id": "444", "status": "blocked", "current_member": "planner"},
            ],
        }
    )

    assert "2 active threads" in text
    assert "<#333> — running (builder)" in text
    assert "<#444> — blocked (planner)" in text


@pytest.mark.asyncio
async def test_each_parent_message_creates_a_distinct_room_thread(monkeypatch):
    submitted = []
    created = []
    room = BotRoomConfig(
        room_id="team",
        display_name="Team",
        platform="discord",
        channel_id="222",
        controller_profile="planner",
        members=(BotRoomMember("planner"), BotRoomMember("builder")),
    )

    class FakeThread:
        def __init__(self, thread_id):
            self.id = thread_id

    class ParentChannel:
        id = 222

    class Store:
        @staticmethod
        def event_by_uid(_event_uid):
            return None

    class Service:
        store = Store()

        async def submit(self, **kwargs):
            submitted.append(kwargs)
            return SimpleNamespace(created=True)

    async def create_thread(message):
        thread = FakeThread(f"thread-{message.id}")
        created.append(thread.id)
        return thread

    async def cache_attachments(_message):
        return ()

    instance = object.__new__(BotRoomsDiscordAdapter)
    instance._botrooms_service = Service()
    instance._botrooms_transport = None
    instance._ensure_botrooms_subscription = lambda: None
    instance._room_channel_authorized = lambda _message: True
    instance._create_room_thread = create_thread
    instance._cache_room_attachments = cache_attachments
    monkeypatch.setattr(adapter_module.discord, "Thread", FakeThread)

    author = SimpleNamespace(id="user", display_name="Daniel")
    guild = SimpleNamespace(id="guild")
    for message_id in ("one", "two"):
        message = SimpleNamespace(
            id=message_id,
            content=f"prompt {message_id}",
            channel=ParentChannel(),
            author=author,
            guild=guild,
        )
        assert await instance._handle_room_message(message, room)

    assert created == ["thread-one", "thread-two"]
    assert [item["thread_id"] for item in submitted] == ["thread-one", "thread-two"]

    follow_up = SimpleNamespace(
        id="follow-up",
        content="continue",
        channel=FakeThread("thread-one"),
        author=author,
        guild=guild,
    )
    assert await instance._handle_room_message(follow_up, room)
    assert created == ["thread-one", "thread-two"]
    assert submitted[-1]["thread_id"] == "thread-one"
