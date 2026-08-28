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
        async def respond(self, room_id, member_key, value):
            events.append(("respond", room_id, member_key, value))
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
        ("respond", "team", "builder", "continue"),
    ]
    assert events[-1] == ("reply", "Response sent to builder.", True)
