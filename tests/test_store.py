from pathlib import Path

from hermes_discord_botrooms.bot_mode.config import BotRoomConfig, BotRoomMember
from hermes_discord_botrooms.bot_mode.models import RoomAttachment
from hermes_discord_botrooms.bot_mode.store import RoomStore


def _room() -> BotRoomConfig:
    return BotRoomConfig(
        room_id="agents",
        display_name="Agents",
        platform="discord",
        channel_id="123",
        controller_profile="default",
        members=(BotRoomMember("default"), BotRoomMember("coder")),
    )


def test_additive_ledgers_keep_the_rollback_compatible_schema_marker(tmp_path: Path):
    store = RoomStore(tmp_path)
    with store.read() as db:
        marker = db.execute(
            "SELECT value FROM bot_room_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        tables = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert marker == "1"
    assert "bot_room_turn_attempts" in tables
    assert "bot_room_delivery_chunks" in tables


def test_additive_v2_marker_is_downgraded_for_executable_rollback(tmp_path: Path):
    store = RoomStore(tmp_path)
    with store.transaction() as db:
        db.execute("UPDATE bot_room_meta SET value='2' WHERE key='schema_version'")

    reopened = RoomStore(tmp_path)
    with reopened.read() as db:
        marker = db.execute(
            "SELECT value FROM bot_room_meta WHERE key='schema_version'"
        ).fetchone()["value"]
    assert marker == "1"


def test_submit_is_idempotent_and_supersedes_the_previous_run(tmp_path: Path):
    store = RoomStore(tmp_path)
    first = store.submit_user_event(
        _room(),
        thread_id="t1",
        event_uid="discord:1",
        text="first",
        author_id="u1",
        author_name="Daniel",
    )
    duplicate = store.submit_user_event(
        _room(),
        thread_id="t1",
        event_uid="discord:1",
        text="first",
        author_id="u1",
        author_name="Daniel",
    )
    assert duplicate == type(duplicate)(
        first.room_id, first.thread_id, first.run_id, first.event_id, False
    )

    second = store.submit_user_event(
        _room(),
        thread_id="t1",
        event_uid="discord:2",
        text="second",
        author_id="u1",
        author_name="Daniel",
        attachments=(RoomAttachment("/tmp/a.png", "a.png", "image", "image/png"),),
    )
    assert store.run_row(first.run_id)["status"] == "superseded"
    assert store.run_row(second.run_id)["epoch"] == 2
    assert store.thread_events("agents", "t1")[-1].attachments[0].name == "a.png"


def test_watermarks_sessions_and_delivery_are_separate_from_room_log(tmp_path: Path):
    store = RoomStore(tmp_path)
    submitted = store.submit_user_event(
        _room(),
        thread_id="t1",
        event_uid="desktop:1",
        text="hello",
        author_id="user",
        author_name="You",
    )
    delta, seen = store.delta_for_member("agents", "t1", "coder")
    assert seen == 0
    assert [event.text for event in delta] == ["hello"]
    store.advance_watermark("agents", "t1", "coder", delta[-1].id)
    assert store.delta_for_member("agents", "t1", "coder")[0] == []

    store.save_session("agents", "coder", "coder", "stored-1")
    assert store.session_id("agents", "coder") == "stored-1"
    store.record_delivery(
        submitted.event_id,
        platform="discord",
        destination="t1",
        status="delivered",
        platform_message_id="m1",
    )
    assert len(store.thread_events("agents", "t1")) == 1


def test_agent_event_replay_does_not_double_count_the_run(tmp_path: Path):
    store = RoomStore(tmp_path)
    submitted = store.submit_user_event(
        _room(),
        thread_id="t1",
        event_uid="discord:1",
        text="hello",
        author_id="user",
        author_name="You",
    )
    payload = {
        "room_id": "agents",
        "thread_id": "t1",
        "run_id": submitted.run_id,
        "member_key": "coder",
        "member_name": "Coder",
        "text": "answer",
        "event_uid": f"agent:{submitted.run_id}:0:coder",
    }
    first = store.append_agent_event(**payload)
    second = store.append_agent_event(**payload)
    assert first.id == second.id
    assert store.run_row(submitted.run_id)["message_count"] == 1


def test_adjacent_member_echo_is_suppressed_but_source_changes_are_not(tmp_path: Path):
    store = RoomStore(tmp_path)
    submitted = store.submit_user_event(
        _room(),
        thread_id="t1",
        event_uid="discord:echo",
        text="hello",
        author_id="user",
        author_name="You",
    )
    base = {
        "room_id": "agents",
        "thread_id": "t1",
        "run_id": submitted.run_id,
        "member_key": "coder",
        "member_name": "Coder",
        "text": "same answer",
    }
    first = store.append_agent_event(**base, event_uid="agent:echo:1", metadata={"source": "a"})
    duplicate = store.append_agent_event(
        **base, event_uid="agent:echo:2", metadata={"source": "a"}
    )
    distinct = store.append_agent_event(
        **base, event_uid="agent:echo:3", metadata={"source": "b"}
    )
    assert duplicate.id == first.id
    assert distinct.id != first.id
    assert store.run_row(submitted.run_id)["message_count"] == 2


def test_stop_bumps_epoch_and_holds_every_room_member(tmp_path: Path):
    store = RoomStore(tmp_path)
    submitted = store.submit_user_event(
        _room(),
        thread_id="t1",
        event_uid="discord:stop",
        text="work",
        author_id="user",
        author_name="You",
    )
    before = store.room_epoch("agents")
    stopped = store.request_stop("agents", "t1", ["default", "coder"])
    assert stopped and stopped["run_id"] == submitted.run_id
    assert store.room_epoch("agents") == before + 1
    assert store.holds("agents") == {"default", "coder"}
    assert store.run_row(submitted.run_id)["status"] == "stopping"


def test_atomic_agent_commit_rechecks_stop_and_newer_user_state(tmp_path: Path):
    store = RoomStore(tmp_path)
    room = _room()
    stopped_run = store.submit_user_event(
        room,
        thread_id="t1",
        event_uid="discord:atomic-1",
        text="first",
        author_id="user",
        author_name="You",
    )
    anchor = stopped_run.event_id
    epoch = store.room_epoch("agents")
    store.request_stop("agents", "t1", ["default", "coder"])
    stopped = store.commit_agent_event(
        room_id="agents",
        thread_id="t1",
        run_id=stopped_run.run_id,
        member_key="coder",
        member_name="Coder",
        text="stale",
        event_uid="agent:atomic:stopped",
        dispatch_epoch=epoch,
        anchor_id=anchor,
    )
    assert stopped.status == "stopped"

    old_run = store.submit_user_event(
        room,
        thread_id="t1",
        event_uid="discord:atomic-2",
        text="second",
        author_id="user",
        author_name="You",
    )
    old_epoch = store.room_epoch("agents")
    store.submit_user_event(
        room,
        thread_id="t1",
        event_uid="discord:atomic-3",
        text="newer",
        author_id="user",
        author_name="You",
    )
    superseded = store.commit_agent_event(
        room_id="agents",
        thread_id="t1",
        run_id=old_run.run_id,
        member_key="coder",
        member_name="Coder",
        text="stale",
        event_uid="agent:atomic:superseded",
        dispatch_epoch=old_epoch,
        anchor_id=old_run.event_id,
    )
    assert superseded.status == "superseded"
    assert "stale" not in [event.text for event in store.thread_events("agents", "t1")]


def test_recovery_and_delivery_queries_only_return_unfinished_work(tmp_path: Path):
    store = RoomStore(tmp_path)
    room = _room()
    submitted = store.submit_user_event(
        room,
        thread_id="t1",
        event_uid="discord:recovery",
        text="hello",
        author_id="user",
        author_name="You",
    )
    rows = store.recoverable_runs([room.room_id], before=10**12)
    assert [row["run_id"] for row in rows] == [submitted.run_id]

    event = store.append_agent_event(
        room_id=room.room_id,
        thread_id="t1",
        run_id=submitted.run_id,
        member_key="coder",
        member_name="Coder",
        text="answer",
        event_uid="agent:recovery:0:coder",
    )
    assert [
        item.id for item in store.undelivered_member_events(room.room_id, platform="discord")
    ] == [event.id]
    store.record_delivery(
        event.id,
        platform="discord",
        destination="t1",
        status="delivered",
        platform_message_id="message-1",
    )
    assert store.undelivered_member_events(room.room_id, platform="discord") == []
    assert store.delivered_message_id(event.id, platform="discord", destination="t1") == "message-1"


def test_turn_phase_and_chunk_receipts_are_durable(tmp_path: Path):
    store = RoomStore(tmp_path)
    room = _room()
    submitted = store.submit_user_event(
        room,
        thread_id="t1",
        event_uid="discord:ledger",
        text="hello",
        author_id="user",
        author_name="You",
    )
    store.save_turn_attempt(
        run_id=submitted.run_id,
        room_id=room.room_id,
        thread_id="t1",
        member_key="coder",
        runtime_session_id="runtime",
        stored_session_id="stored",
        baseline_row_id=41,
    )
    store.mark_turn_dispatched(submitted.run_id, "coder")
    assert store.turn_attempt(submitted.run_id, "coder")["phase"] == "dispatched"
    store.save_turn_result(
        submitted.run_id,
        "coder",
        text="answer",
        status="complete",
    )
    assert store.turn_attempt(submitted.run_id, "coder")["result_text"] == "answer"

    event = store.append_agent_event(
        room_id=room.room_id,
        thread_id="t1",
        run_id=submitted.run_id,
        member_key="coder",
        member_name="Coder",
        text="answer",
        event_uid="agent:ledger:0:coder",
    )
    store.begin_delivery_chunk(
        event.id,
        platform="discord",
        destination="t1",
        chunk_index=0,
        nonce="nonce",
    )
    assert (
        store.delivery_chunk_status(
            event.id,
            platform="discord",
            destination="t1",
            chunk_index=0,
        )
        == "unknown"
    )
    assert store.delivered_message_id(event.id, platform="discord", destination="t1").startswith(
        "ambiguous:"
    )
    assert [
        row.id for row in store.undelivered_member_events(room.room_id, platform="discord")
    ] == [event.id]
    store.fail_delivery_chunk_definitively(
        event.id,
        platform="discord",
        destination="t1",
        chunk_index=0,
        nonce="nonce",
        error="Discord API 403",
    )
    assert (
        store.delivery_chunk_status(
            event.id,
            platform="discord",
            destination="t1",
            chunk_index=0,
        )
        == "failed"
    )
    assert store.delivered_message_id(event.id, platform="discord", destination="t1").startswith(
        "ambiguous:"
    )
    store.record_delivery_chunk(
        event.id,
        platform="discord",
        destination="t1",
        chunk_index=0,
        nonce="nonce",
        status="delivered",
        platform_message_id="chunk-1",
    )
    assert (
        store.delivered_chunk_message_id(
            event.id,
            platform="discord",
            destination="t1",
            chunk_index=0,
        )
        == "chunk-1"
    )
    assert (
        store.delivery_chunk_status(
            event.id,
            platform="discord",
            destination="t1",
            chunk_index=0,
        )
        == "delivered"
    )
