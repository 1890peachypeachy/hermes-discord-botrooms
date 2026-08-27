import asyncio
import threading
import time
from pathlib import Path

import pytest
import yaml

from hermes_discord_botrooms.bot_mode.config import (
    BotRoomConfigError,
    bot_room_from_mapping,
    discord_channel_is_configured,
    load_bot_room_registry,
    room_for_discord_channel,
)
from hermes_discord_botrooms.bot_mode.models import MemberTurnResult, PendingPrompt
from hermes_discord_botrooms.bot_mode.service import BotRoomService
from hermes_discord_botrooms.bot_mode.store import RoomStore


class PassExecutor:
    def __init__(self):
        self.calls = []

    async def turn(self, room, member, prompt, attachments, **kwargs):
        self.calls.append((member.profile, kwargs))
        return MemberTurnResult(text="(pass)", stored_session_id=f"stored-{member.profile}")


class InterruptibleExecutor(PassExecutor):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.interrupts = []

    async def turn(self, room, member, prompt, attachments, **kwargs):
        self.calls.append((member.profile, kwargs))
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return MemberTurnResult(text="(pass)", stored_session_id=f"stored-{member.profile}")

    async def interrupt_room_member(self, room_id, member):
        self.interrupts.append((room_id, member.profile))
        self.release.set()
        return True


def test_registry_is_installation_scoped_and_threads_match_the_parent(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "bot_mode": {
                    "rooms": [
                        {
                            "room_id": "agents",
                            "platform": "discord",
                            "guild_id": "guild",
                            "channel_id": "parent",
                            "controller_profile": "default",
                            "members": [
                                "default",
                                {"profile": "coder", "title": "Builder"},
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    registry = load_bot_room_registry(tmp_path)
    assert registry["agents"].members[1].label == "Builder"
    assert (
        room_for_discord_channel(
            registry,
            channel_id="thread",
            parent_channel_id="parent",
            guild_id="guild",
        ).room_id
        == "agents"
    )
    assert (
        room_for_discord_channel(
            registry,
            channel_id="thread",
            parent_channel_id="parent",
            guild_id="other-guild",
        )
        is None
    )


def test_registry_prefers_namespaced_plugin_rooms(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "bot_mode": {
                    "rooms": [
                        {
                            "room_id": "legacy",
                            "platform": "discord",
                            "channel_id": "10",
                            "members": ["default", "legacy-agent"],
                        }
                    ]
                },
                "plugins": {
                    "entries": {
                        "hermes-discord-botrooms": {
                            "settings": {
                                "rooms": [
                                    {
                                        "room_id": "public-room",
                                        "platform": "discord",
                                        "channel_id": "20",
                                        "controller_profile": "alpha",
                                        "members": ["alpha", "beta"],
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    registry = load_bot_room_registry(tmp_path)

    assert list(registry) == ["public-room"]
    assert [member.profile for member in registry["public-room"].members] == [
        "alpha",
        "beta",
    ]


def test_registry_rejects_rooms_that_cannot_be_scheduled(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        "bot_mode:\n  rooms:\n    - room_id: agents\n      platform: discord\n"
        "      channel_id: '1'\n      controller_profile: missing\n"
        "      members: [default, coder]\n",
        encoding="utf-8",
    )
    with pytest.raises(BotRoomConfigError, match="controller_profile"):
        load_bot_room_registry(tmp_path)
    assert discord_channel_is_configured(tmp_path, channel_id="1", guild_id="")


def test_registry_rejects_duplicate_discord_channel_claims(tmp_path: Path):
    rooms = [
        {
            "room_id": room_id,
            "platform": "discord",
            "channel_id": "same-channel",
            "controller_profile": "alpha",
            "members": ["alpha", "beta"],
        }
        for room_id in ("first", "second")
    ]
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {"plugins": {"entries": {"hermes-discord-botrooms": {"settings": {"rooms": rooms}}}}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(BotRoomConfigError, match="claimed by both"):
        load_bot_room_registry(tmp_path)


def test_legacy_switch_releases_discord_room_reservations(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(
        "bot_mode:\n"
        "  room_engine: legacy\n"
        "  rooms:\n"
        "    - room_id: agents\n"
        "      platform: discord\n"
        "      channel_id: '1'\n"
        "      members: [default, coder]\n",
        encoding="utf-8",
    )
    assert load_bot_room_registry(tmp_path) == {}
    assert not discord_channel_is_configured(tmp_path, channel_id="1")


@pytest.mark.asyncio
async def test_service_queues_run_on_its_private_loop_and_publishes_completion(
    tmp_path: Path,
):
    service = BotRoomService(tmp_path, executor=PassExecutor())
    service.create_or_update(
        {
            "room_id": "desktop-room",
            "display_name": "Desktop Room",
            "platform": "desktop",
            "controller_profile": "default",
            "members": ["default", "coder"],
        }
    )
    finished = asyncio.Event()
    received = []

    async def subscriber(event):
        received.append(event)
        if event.get("kind") == "run.finished":
            finished.set()

    token = service.subscribe(subscriber)
    try:
        submitted = await service.submit(
            room_id="desktop-room",
            thread_id="thread",
            event_uid="desktop:1",
            text="hello",
            author_id="user",
            author_name="You",
        )
        await asyncio.wait_for(finished.wait(), timeout=5)
        assert submitted.created
        assert service.status("desktop-room", "thread")["run"]["status"] == "settled"
        assert any(event.get("kind") == "member.passed" for event in received)
    finally:
        service.unsubscribe(token)
        service.close()


@pytest.mark.asyncio
async def test_service_recovers_a_run_persisted_before_startup(tmp_path: Path):
    room = bot_room_from_mapping(
        {
            "room_id": "desktop-room",
            "display_name": "Desktop Room",
            "platform": "desktop",
            "controller_profile": "default",
            "members": ["default", "coder"],
        }
    )
    store = RoomStore(tmp_path)
    submitted = store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="desktop:before-restart",
        text="recover me",
        author_id="user",
        author_name="You",
    )
    time.sleep(0.01)
    executor = PassExecutor()
    service = BotRoomService(tmp_path, executor=executor)
    finished = asyncio.Event()

    async def subscriber(event):
        if event.get("kind") == "run.finished":
            finished.set()

    token = service.subscribe(subscriber)
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
        assert service.store.run_row(submitted.run_id)["status"] == "settled"
        assert executor.calls
        assert all(call[1].get("recovering") for call in executor.calls)
    finally:
        service.unsubscribe(token)
        service.close()


@pytest.mark.asyncio
async def test_service_clears_stale_prompt_while_recovering_blocked_run(
    tmp_path: Path,
):
    room = bot_room_from_mapping(
        {
            "room_id": "desktop-room",
            "display_name": "Desktop Room",
            "platform": "desktop",
            "controller_profile": "default",
            "members": ["default", "coder"],
        }
    )
    store = RoomStore(tmp_path)
    submitted = store.submit_user_event(
        room,
        thread_id="thread",
        event_uid="desktop:blocked-before-restart",
        text="recover my blocked turn",
        author_id="user",
        author_name="You",
    )
    store.update_run(submitted.run_id, "blocked", current_member="default")
    store.set_pending_prompt(
        PendingPrompt(
            room_id=room.room_id,
            run_id=submitted.run_id,
            thread_id="thread",
            member_key="default",
            kind="clarify",
            request_id="stale-request",
            payload={"question": "stale"},
            runtime_session_id="dead-runtime",
        )
    )
    time.sleep(0.01)
    executor = PassExecutor()
    service = BotRoomService(tmp_path, executor=executor)
    finished = asyncio.Event()

    async def subscriber(event):
        if event.get("kind") == "run.finished":
            finished.set()

    token = service.subscribe(subscriber)
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
        assert service.store.run_row(submitted.run_id)["status"] == "settled"
        assert service.store.pending_prompt(room.room_id) is None
        assert executor.calls
        assert all(call[1].get("recovering") for call in executor.calls)
    finally:
        service.unsubscribe(token)
        service.close()


@pytest.mark.asyncio
async def test_new_submit_interrupts_the_superseded_active_turn(tmp_path: Path):
    executor = InterruptibleExecutor()
    service = BotRoomService(tmp_path, executor=executor)
    service.create_or_update(
        {
            "room_id": "desktop-room",
            "display_name": "Desktop Room",
            "platform": "desktop",
            "controller_profile": "default",
            "members": ["default", "coder"],
        }
    )
    token = service.subscribe(lambda _event: asyncio.sleep(0))
    try:
        first = await service.submit(
            room_id="desktop-room",
            thread_id="thread",
            event_uid="desktop:first",
            text="first",
            author_id="user",
            author_name="You",
        )
        assert await asyncio.to_thread(executor.started.wait, 2)
        second = await service.submit(
            room_id="desktop-room",
            thread_id="thread",
            event_uid="desktop:second",
            text="second",
            author_id="user",
            author_name="You",
        )
        assert second.superseded_run_id == first.run_id
        assert executor.interrupts == [("desktop-room", "default")]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if service.store.run_row(second.run_id)["status"] == "settled":
                break
            await asyncio.sleep(0.02)
        assert service.store.run_row(second.run_id)["status"] == "settled"
    finally:
        service.unsubscribe(token)
        service.close()


@pytest.mark.asyncio
async def test_graceful_close_preserves_active_run_and_ledger_for_restart(
    tmp_path: Path,
):
    executor = InterruptibleExecutor()
    service = BotRoomService(tmp_path, executor=executor)
    service.create_or_update(
        {
            "room_id": "desktop-room",
            "display_name": "Desktop Room",
            "platform": "desktop",
            "controller_profile": "default",
            "members": ["default", "coder"],
        }
    )
    token = service.subscribe(lambda _event: asyncio.sleep(0))
    submitted = await service.submit(
        room_id="desktop-room",
        thread_id="thread",
        event_uid="desktop:shutdown",
        text="survive restart",
        author_id="user",
        author_name="You",
    )
    assert await asyncio.to_thread(executor.started.wait, 2)
    service.store.save_turn_attempt(
        run_id=submitted.run_id,
        room_id="desktop-room",
        thread_id="thread",
        member_key="default",
        runtime_session_id="runtime",
        stored_session_id="stored",
        baseline_row_id=4,
    )
    service.store.mark_turn_dispatched(submitted.run_id, "default")
    service.unsubscribe(token)
    service.close()

    store = RoomStore(tmp_path)
    assert store.run_row(submitted.run_id)["status"] == "running"
    assert store.turn_attempt(submitted.run_id, "default")["phase"] == "dispatched"

    restarted = BotRoomService(tmp_path, executor=PassExecutor())
    finished = asyncio.Event()

    async def subscriber(event):
        if event.get("kind") == "run.finished":
            finished.set()

    restart_token = restarted.subscribe(subscriber)
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
        assert restarted.store.run_row(submitted.run_id)["status"] == "settled"
        assert restarted.store.turn_attempt(submitted.run_id, "default") is None
    finally:
        restarted.unsubscribe(restart_token)
        restarted.close()
