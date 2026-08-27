import asyncio
from pathlib import Path

import pytest

from hermes_discord_botrooms.bot_mode.config import BotRoomConfig, BotRoomMember
from hermes_discord_botrooms.bot_mode.discord_transport import (
    MAX_CHUNK_CHARS,
    MAX_CHUNKS,
    DiscordRequestError,
    DiscordRoomTransport,
    _chunks,
)
from hermes_discord_botrooms.bot_mode.models import RoomEvent


def test_discord_room_chunks_are_bounded_and_add_a_truncation_notice():
    chunks = _chunks("word " * 10000)
    assert len(chunks) == MAX_CHUNKS
    assert all(len(chunk) <= MAX_CHUNK_CHARS for chunk in chunks)
    assert "truncated" in chunks[-1].lower()


def test_empty_room_output_stays_silent():
    assert _chunks("  ") == []


@pytest.mark.asyncio
async def test_delivery_uses_stable_enforced_nonce_and_skips_a_receipt(monkeypatch):
    class Store:
        receipt = ""
        chunk_receipts = {}

        def delivered_message_id(self, *_args, **_kwargs):
            return self.receipt

        def record_delivery(self, *_args, **kwargs):
            if kwargs["status"] == "delivered":
                self.receipt = kwargs["platform_message_id"]

        def delivered_chunk_message_id(self, _event_id, *, platform, destination, chunk_index):
            return self.chunk_receipts.get((platform, destination, chunk_index), "")

        def delivery_chunk_status(self, _event_id, *, platform, destination, chunk_index):
            return ""

        def begin_delivery_chunk(self, *_args, **_kwargs):
            pass

        def fail_delivery_chunk_definitively(self, *_args, **_kwargs):
            pass

        def record_delivery_chunk(self, _event_id, **kwargs):
            if kwargs["status"] == "delivered":
                self.chunk_receipts[
                    (kwargs["platform"], kwargs["destination"], kwargs["chunk_index"])
                ] = kwargs["platform_message_id"]

    store = Store()
    transport = DiscordRoomTransport(Path("/tmp/root"), store)
    requests = []

    async def request(method, path, token, **kwargs):
        requests.append((method, path, token, kwargs))
        return {"id": "discord-message"}

    transport._request = request
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_token", lambda *_: "token"
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_proxy", lambda *_: None
    )
    member = BotRoomMember("coder")
    room = BotRoomConfig(
        "agents", "Agents", "discord", "parent", (member, BotRoomMember("default"))
    )
    event = RoomEvent(
        id=7,
        event_uid="agent:run:0:coder",
        room_id="agents",
        thread_id="thread",
        run_id="run",
        kind="message",
        author_kind="member",
        author_id="coder",
        author_name="Coder",
        text="hello",
    )

    assert await transport.deliver_event(room, event, member) == "discord-message"
    payload = requests[0][3]["payload"]
    assert payload["enforce_nonce"] is True
    assert len(payload["nonce"]) == 24
    assert await transport.deliver_event(room, event, member) == "discord-message"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_retry_never_reposts_an_ambiguous_discord_chunk(monkeypatch):
    class Store:
        def __init__(self):
            self.chunks = {}
            self.statuses = {}
            self.overall = ""

        def delivered_message_id(self, *_args, **_kwargs):
            return self.overall

        def record_delivery(self, *_args, **kwargs):
            if kwargs["status"] == "delivered":
                self.overall = kwargs["platform_message_id"]

        def delivered_chunk_message_id(self, _event_id, *, platform, destination, chunk_index):
            return self.chunks.get((platform, destination, chunk_index), "")

        def delivery_chunk_status(self, _event_id, *, platform, destination, chunk_index):
            return self.statuses.get((platform, destination, chunk_index), "")

        def begin_delivery_chunk(self, _event_id, **kwargs):
            key = (
                kwargs["platform"],
                kwargs["destination"],
                kwargs["chunk_index"],
            )
            self.statuses[key] = "unknown"
            self.overall = f"ambiguous:{kwargs['nonce']}"

        def fail_delivery_chunk_definitively(self, _event_id, **kwargs):
            key = (
                kwargs["platform"],
                kwargs["destination"],
                kwargs["chunk_index"],
            )
            self.statuses[key] = "failed"

        def record_delivery_chunk(self, _event_id, **kwargs):
            key = (
                kwargs["platform"],
                kwargs["destination"],
                kwargs["chunk_index"],
            )
            self.statuses[key] = kwargs["status"]
            if kwargs["status"] == "delivered":
                self.chunks[key] = kwargs["platform_message_id"]

    store = Store()
    transport = DiscordRoomTransport(Path("/tmp/root"), store)
    attempts = []

    async def request(_method, _path, _token, **_kwargs):
        attempts.append(len(attempts))
        if len(attempts) == 2:
            raise RuntimeError("second chunk failed")
        return {"id": f"message-{len(attempts)}"}

    transport._request = request
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_token", lambda *_: "token"
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_proxy", lambda *_: None
    )
    member = BotRoomMember("coder")
    room = BotRoomConfig(
        "agents", "Agents", "discord", "parent", (member, BotRoomMember("default"))
    )
    event = RoomEvent(
        id=8,
        event_uid="agent:run:0:coder",
        room_id="agents",
        thread_id="thread",
        run_id="run",
        kind="message",
        author_kind="member",
        author_id="coder",
        author_name="Coder",
        text="x" * (MAX_CHUNK_CHARS + 20),
    )

    with pytest.raises(RuntimeError, match="second chunk failed"):
        await transport.deliver_event(room, event, member)
    assert len(attempts) == 2
    with pytest.raises(RuntimeError, match="ambiguous prior delivery"):
        await transport.deliver_event(room, event, member)
    assert len(attempts) == 2
    assert store.overall.startswith("ambiguous:")


@pytest.mark.asyncio
async def test_explicit_discord_rejection_remains_retryable(monkeypatch):
    class Store:
        def __init__(self):
            self.status = ""

        def delivered_chunk_message_id(self, *_args, **_kwargs):
            return ""

        def delivery_chunk_status(self, *_args, **_kwargs):
            return self.status

        def begin_delivery_chunk(self, *_args, **_kwargs):
            self.status = "unknown"

        def fail_delivery_chunk_definitively(self, *_args, **_kwargs):
            self.status = "failed"

        def record_delivery_chunk(self, _event_id, **kwargs):
            self.status = kwargs["status"]

    store = Store()
    transport = DiscordRoomTransport(Path("/tmp/root"), store)
    attempts = 0

    async def request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DiscordRequestError("Discord API 403", ambiguous=False)
        return {"id": "message-after-permission-fix"}

    transport._request = request
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_token", lambda *_: "token"
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_proxy", lambda *_: None
    )

    with pytest.raises(DiscordRequestError, match="403"):
        await transport.send_text(
            profile="coder",
            thread_id="thread",
            text="hello",
            nonce_seed="event",
            event_id=9,
        )
    assert store.status == "failed"
    assert (
        await transport.send_text(
            profile="coder",
            thread_id="thread",
            text="hello",
            nonce_seed="event",
            event_id=9,
        )
        == "message-after-permission-fix"
    )


@pytest.mark.asyncio
async def test_delivery_recovery_pages_through_more_than_one_hundred_events():
    member = BotRoomMember("coder")
    room = BotRoomConfig(
        "agents", "Agents", "discord", "parent", (member, BotRoomMember("default"))
    )
    events = [
        RoomEvent(
            id=index,
            event_uid=f"agent:run:{index}:coder",
            room_id="agents",
            thread_id="thread",
            run_id="run",
            kind="message",
            author_kind="member",
            author_id="coder",
            author_name="Coder",
            text=f"message {index}",
        )
        for index in range(1, 206)
    ]

    class Store:
        def undelivered_member_events(self, _room_id, *, platform, limit, after_id):
            assert platform == "discord"
            return [event for event in events if event.id > after_id][:limit]

    transport = DiscordRoomTransport(Path("/tmp/root"), Store())
    delivered = []

    async def deliver_event(_room, event, _member):
        delivered.append(event.id)
        return f"discord-{event.id}"

    transport.deliver_event = deliver_event
    result = await transport.recover_room(room)
    assert result == {"delivered": 205, "failed": 0}
    assert delivered == list(range(1, 206))


@pytest.mark.asyncio
async def test_typing_uses_the_working_profiles_own_token_and_stops(monkeypatch):
    transport = DiscordRoomTransport(Path("/tmp/root"), object())
    requests = []

    async def request(method, path, token, **kwargs):
        requests.append((method, path, token, kwargs))
        return {}

    transport._request = request
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_token",
        lambda _root, profile: f"{profile}-token",
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_proxy", lambda *_: None
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport.TYPING_REFRESH_SECONDS", 0.01
    )

    await transport.start_typing(profile="coder", thread_id="thread")
    await asyncio.sleep(0.025)
    await transport.stop_typing(profile="coder", thread_id="thread")

    assert len(requests) >= 2
    assert all(item[:3] == ("POST", "/channels/thread/typing", "coder-token") for item in requests)
    count = len(requests)
    await asyncio.sleep(0.02)
    assert len(requests) == count


@pytest.mark.asyncio
async def test_typing_failure_is_best_effort_and_cleanup_cancels_every_task(monkeypatch):
    transport = DiscordRoomTransport(Path("/tmp/root"), object())

    async def request(*_args, **_kwargs):
        raise RuntimeError("presentation outage")

    transport._request = request
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_token", lambda *_: "token"
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_proxy", lambda *_: None
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport.TYPING_REFRESH_SECONDS", 0.01
    )

    await transport.start_typing(profile="default", thread_id="one")
    await transport.start_typing(profile="coder", thread_id="two")
    await asyncio.sleep(0.015)
    assert len(transport._typing_tasks) == 2
    await transport.stop_all_typing()
    assert transport._typing_tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_helper", ["_profile_token", "_profile_proxy"])
async def test_typing_setup_failure_is_consumed_and_removes_finished_task(
    monkeypatch, failing_helper
):
    transport = DiscordRoomTransport(Path("/tmp/root"), object())

    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_token", lambda *_: "token"
    )
    monkeypatch.setattr(
        "hermes_discord_botrooms.bot_mode.discord_transport._profile_proxy", lambda *_: None
    )

    def fail(*_args):
        raise RuntimeError("secret or proxy lookup failed")

    monkeypatch.setattr(
        f"hermes_discord_botrooms.bot_mode.discord_transport.{failing_helper}", fail
    )

    await transport.start_typing(profile="coder", thread_id="thread")
    await asyncio.sleep(0)
    assert transport._typing_tasks == {}
    await transport.stop_all_typing()
