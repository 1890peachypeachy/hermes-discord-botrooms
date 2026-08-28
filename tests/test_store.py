import concurrent.futures
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_discord_botrooms.bot_mode.config import BotRoomConfig, BotRoomMember
from hermes_discord_botrooms.bot_mode.models import PendingPrompt, RoomAttachment
from hermes_discord_botrooms.bot_mode.prompts import resolve_responders
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


def _desktop_room() -> BotRoomConfig:
    return BotRoomConfig(
        room_id="desktop-room",
        display_name="Desktop Room",
        platform="desktop",
        channel_id="",
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
    assert "bot_room_thread_sessions" in tables
    assert "bot_room_thread_prompts" in tables


def test_existing_thread_table_gains_isolation_columns_additively(tmp_path: Path):
    data_dir = tmp_path / "plugin-data" / "hermes-discord-botrooms"
    data_dir.mkdir(parents=True)
    with sqlite3.connect(data_dir / "rooms.db") as db:
        db.executescript(
            """
            CREATE TABLE bot_room_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO bot_room_meta(key,value) VALUES('schema_version','1');
            CREATE TABLE bot_rooms (
                room_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                config_json TEXT NOT NULL,
                epoch INTEGER NOT NULL DEFAULT 0,
                active_run_id TEXT,
                holds_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE bot_room_threads (
                room_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                guild_id TEXT,
                channel_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (room_id, thread_id)
            );
            """
        )

    store = RoomStore(tmp_path)
    with store.read() as db:
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(bot_room_threads)").fetchall()
        }
    assert {"epoch", "active_run_id", "holds_json", "context_start_event_id"} <= columns


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

    store.save_session("agents", "t1", "coder", "coder", "stored-1")
    assert store.session_id("agents", "t1", "coder") == "stored-1"
    store.record_delivery(
        submitted.event_id,
        platform="discord",
        destination="t1",
        status="delivered",
        platform_message_id="m1",
    )
    assert len(store.thread_events("agents", "t1")) == 1


def test_discord_threads_isolate_runs_sessions_holds_and_prompts(tmp_path: Path):
    store = RoomStore(tmp_path)
    room = _room()
    first = store.submit_user_event(
        room,
        thread_id="t1",
        event_uid="discord:isolation-1",
        text="first thread",
        author_id="user",
        author_name="You",
    )
    second = store.submit_user_event(
        room,
        thread_id="t2",
        event_uid="discord:isolation-2",
        text="second thread",
        author_id="user",
        author_name="You",
    )

    assert second.superseded_run_id == ""
    assert store.run_row(first.run_id)["status"] == "queued"
    assert store.run_row(second.run_id)["status"] == "queued"
    assert store.room_epoch("agents", "t1") == 1
    assert store.room_epoch("agents", "t2") == 1

    replacement = store.submit_user_event(
        room,
        thread_id="t1",
        event_uid="discord:isolation-3",
        text="replace only thread one",
        author_id="user",
        author_name="You",
    )
    assert replacement.superseded_run_id == first.run_id
    assert store.run_row(first.run_id)["status"] == "superseded"
    assert store.run_row(second.run_id)["status"] == "queued"

    store.save_session("agents", "t1", "coder", "coder", "stored-t1")
    store.save_session("agents", "t2", "coder", "coder", "stored-t2")
    assert store.session_id("agents", "t1", "coder") == "stored-t1"
    assert store.session_id("agents", "t2", "coder") == "stored-t2"

    for thread_id, run_id in (("t1", replacement.run_id), ("t2", second.run_id)):
        store.set_pending_prompt(
            PendingPrompt(
                room_id="agents",
                run_id=run_id,
                thread_id=thread_id,
                member_key="coder",
                kind="clarify",
                request_id=f"request-{thread_id}",
                payload={"question": thread_id},
                runtime_session_id=f"runtime-{thread_id}",
            )
        )
    assert store.pending_prompt("agents", "t1", "coder").request_id == "request-t1"
    assert store.pending_prompt("agents", "t2", "coder").request_id == "request-t2"
    thread_status = store.status("agents", "t1")
    assert thread_status["active_run_id"] == replacement.run_id
    assert [item["thread_id"] for item in thread_status["pending_prompts"]] == ["t1"]
    aggregate_status = store.status("agents")
    assert {item["thread_id"] for item in aggregate_status["active_threads"]} == {
        "t1",
        "t2",
    }
    assert {item["thread_id"] for item in aggregate_status["pending_prompts"]} == {
        "t1",
        "t2",
    }

    recoverable = store.recoverable_runs(["agents"], before=10**12)
    assert {row["run_id"] for row in recoverable} == {replacement.run_id, second.run_id}

    store.request_stop("agents", "t1", ["default", "coder"])
    assert store.holds("agents", "t1") == {"default", "coder"}
    assert store.holds("agents", "t2") == set()
    assert store.run_row(second.run_id)["status"] == "queued"
    remaining = store.recoverable_runs(["agents"], before=10**12)
    assert {row["run_id"] for row in remaining} == {second.run_id}


def test_thread_isolation_migration_stops_legacy_discord_work_and_starts_fresh(
    tmp_path: Path,
):
    store = RoomStore(tmp_path)
    room = _room()
    submitted = store.submit_user_event(
        room,
        thread_id="legacy-thread",
        event_uid="discord:legacy",
        text="@coder legacy context",
        author_id="user",
        author_name="You",
    )
    with store.transaction() as db:
        db.execute("DELETE FROM bot_room_meta WHERE key='thread_isolation_version'")
        db.execute(
            "UPDATE bot_room_runs SET status='running',stop_requested=0,finished_at=NULL "
            "WHERE run_id=?",
            (submitted.run_id,),
        )
        db.execute(
            "UPDATE bot_rooms SET epoch=7,active_run_id=?,holds_json='[\"coder\"]' "
            "WHERE room_id='agents'",
            (submitted.run_id,),
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_room_sessions("
            "room_id,member_key,profile,stored_session_id,updated_at) "
            "VALUES('agents','coder','coder','legacy-stored',1)"
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_room_prompts("
            "room_id,run_id,thread_id,member_key,kind,request_id,runtime_session_id,"
            "payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "agents",
                submitted.run_id,
                "legacy-thread",
                "coder",
                "clarify",
                "legacy-request",
                "legacy-runtime",
                "{}",
                1,
            ),
        )
        db.execute("DELETE FROM bot_room_thread_sessions WHERE room_id='agents'")
        db.execute("DELETE FROM bot_room_thread_prompts WHERE room_id='agents'")
        db.execute("DELETE FROM bot_room_watermarks WHERE room_id='agents'")

    migrated = RoomStore(tmp_path)
    assert migrated.run_row(submitted.run_id)["status"] == "stopped"
    assert migrated.session_id("agents", "legacy-thread", "coder") == ""
    assert migrated.pending_prompt("agents", "legacy-thread", "coder") is None
    assert migrated.holds("agents", "legacy-thread") == set()
    assert migrated.delta_for_member("agents", "legacy-thread", "coder")[0] == []
    fresh = migrated.submit_user_event(
        room,
        thread_id="legacy-thread",
        event_uid="discord:fresh",
        text="@default fresh context",
        author_id="user",
        author_name="You",
    )
    assert fresh.created
    routing_log = migrated.thread_events("agents", "legacy-thread")
    assert [event.text for event in routing_log] == ["@default fresh context"]
    assert [member.key for member in resolve_responders(routing_log, room.members)] == [
        "default"
    ]
    assert [
        event.text for event in migrated.events("agents", thread_id="legacy-thread")
    ] == ["@coder legacy context", "@default fresh context"]
    with migrated.read() as db:
        legacy = db.execute(
            "SELECT stored_session_id FROM bot_room_sessions "
            "WHERE room_id='agents' AND member_key='coder'"
        ).fetchone()
        marker = db.execute(
            "SELECT value FROM bot_room_meta WHERE key='thread_isolation_version'"
        ).fetchone()
    assert legacy["stored_session_id"] == "legacy-stored"
    assert marker["value"] == "1"


def test_reupgrade_retires_discord_work_written_by_a_rolled_back_executable(
    tmp_path: Path,
):
    store = RoomStore(tmp_path)
    room = _room()
    original = store.submit_user_event(
        room,
        thread_id="rollback-thread",
        event_uid="discord:before-rollback",
        text="before rollback",
        author_id="user",
        author_name="You",
    )
    store.update_run(original.run_id, "settled", finished=True)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO bot_room_runs("
            "run_id,room_id,thread_id,epoch,status,started_at,updated_at) "
            "VALUES('rollback-run','agents','rollback-thread',9,'running',2,2)"
        )
        db.execute(
            "INSERT INTO bot_room_events("
            "event_uid,room_id,thread_id,run_id,kind,author_kind,author_id,author_name,"
            "text,attachments_json,metadata_json,created_at) "
            "VALUES('discord:rollback-write','agents','rollback-thread','rollback-run',"
            "'message','user','user','You','rollback context','[]','{}',2)"
        )
        db.execute(
            "UPDATE bot_rooms SET epoch=9,active_run_id='rollback-run',"
            "holds_json='[\"coder\"]' WHERE room_id='agents'"
        )
        db.execute(
            "UPDATE bot_room_threads SET active_run_id=NULL WHERE room_id='agents' "
            "AND thread_id='rollback-thread'"
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_room_sessions("
            "room_id,member_key,profile,stored_session_id,updated_at) "
            "VALUES('agents','coder','coder','rollback-stored',2)"
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_room_thread_sessions("
            "room_id,thread_id,member_key,profile,stored_session_id,updated_at) "
            "VALUES('agents','rollback-thread','coder','coder','stale-thread-session',2)"
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_room_prompts("
            "room_id,run_id,thread_id,member_key,kind,request_id,runtime_session_id,"
            "payload_json,created_at) VALUES('agents','rollback-run','rollback-thread',"
            "'coder','clarify','old-request','old-runtime','{}',2)"
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_room_thread_prompts("
            "room_id,thread_id,run_id,member_key,kind,request_id,runtime_session_id,"
            "payload_json,created_at) VALUES('agents','rollback-thread','rollback-run',"
            "'coder','clarify','stale-request','stale-runtime','{}',2)"
        )

    reopened = RoomStore(tmp_path)
    assert reopened.run_row("rollback-run")["status"] == "stopped"
    assert reopened.pending_prompt("agents", "rollback-thread", "coder") is None
    assert reopened.session_id("agents", "rollback-thread", "coder") == ""
    assert reopened.delta_for_member("agents", "rollback-thread", "coder")[0] == []
    with reopened.read() as db:
        room_state = db.execute(
            "SELECT active_run_id,holds_json FROM bot_rooms WHERE room_id='agents'"
        ).fetchone()
        thread_state = db.execute(
            "SELECT active_run_id,holds_json FROM bot_room_threads "
            "WHERE room_id='agents' AND thread_id='rollback-thread'"
        ).fetchone()
        legacy_session = db.execute(
            "SELECT stored_session_id FROM bot_room_sessions "
            "WHERE room_id='agents' AND member_key='coder'"
        ).fetchone()
    assert room_state["active_run_id"] is None
    assert room_state["holds_json"] == "[]"
    assert thread_state["active_run_id"] is None
    assert thread_state["holds_json"] == "[]"
    assert legacy_session["stored_session_id"] == "rollback-stored"


def test_thread_isolation_migration_state_and_marker_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = RoomStore(tmp_path)
    submitted = store.submit_user_event(
        _room(),
        thread_id="atomic-thread",
        event_uid="discord:atomic-migration",
        text="keep until migration commits",
        author_id="user",
        author_name="You",
    )
    with store.transaction() as db:
        db.execute("DELETE FROM bot_room_meta WHERE key='thread_isolation_version'")

    def fail_after_state_write(db):
        db.execute("UPDATE bot_rooms SET active_run_id=NULL WHERE room_id='agents'")
        raise RuntimeError("simulated migration failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            RoomStore,
            "_migrate_thread_isolation",
            staticmethod(fail_after_state_write),
        )
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            with RoomStore(tmp_path).read():
                pass

    with sqlite3.connect(store.path) as db:
        active_run_id = db.execute(
            "SELECT active_run_id FROM bot_rooms WHERE room_id='agents'"
        ).fetchone()[0]
        marker = db.execute(
            "SELECT value FROM bot_room_meta WHERE key='thread_isolation_version'"
        ).fetchone()
    assert active_run_id == submitted.run_id
    assert marker is None


def test_schema_lock_allows_only_one_process_style_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original = RoomStore._migrate_thread_isolation
    call_count = 0
    call_guard = threading.Lock()

    def counting_migration(db):
        nonlocal call_count
        with call_guard:
            call_count += 1
        time.sleep(0.05)
        original(db)

    monkeypatch.setattr(
        RoomStore,
        "_migrate_thread_isolation",
        staticmethod(counting_migration),
    )

    def initialize_store():
        with RoomStore(tmp_path).read() as db:
            return db.execute(
                "SELECT value FROM bot_room_meta WHERE key='thread_isolation_version'"
            ).fetchone()["value"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: initialize_store(), range(2)))

    assert results == ["1", "1"]
    assert call_count == 1


def test_thread_isolation_migration_preserves_non_discord_state(tmp_path: Path):
    store = RoomStore(tmp_path)
    submitted = store.submit_user_event(
        _desktop_room(),
        thread_id="desktop-thread",
        event_uid="desktop:legacy",
        text="continue after upgrade",
        author_id="user",
        author_name="You",
    )
    with store.transaction() as db:
        db.execute("DELETE FROM bot_room_meta WHERE key='thread_isolation_version'")
        db.execute(
            "UPDATE bot_rooms SET epoch=4,active_run_id=?,holds_json='[\"coder\"]' "
            "WHERE room_id='desktop-room'",
            (submitted.run_id,),
        )
        db.execute(
            "UPDATE bot_room_threads SET epoch=0,active_run_id=NULL,holds_json='[]' "
            "WHERE room_id='desktop-room'"
        )
        db.execute(
            "INSERT OR REPLACE INTO bot_room_sessions("
            "room_id,member_key,profile,stored_session_id,updated_at) "
            "VALUES('desktop-room','coder','coder','desktop-stored',1)"
        )
        db.execute("DELETE FROM bot_room_thread_sessions WHERE room_id='desktop-room'")

    migrated = RoomStore(tmp_path)
    assert migrated.run_row(submitted.run_id)["status"] == "queued"
    assert migrated.room_epoch("desktop-room", "desktop-thread") == 4
    assert migrated.holds("desktop-room", "desktop-thread") == {"coder"}
    assert (
        migrated.session_id("desktop-room", "desktop-thread", "coder")
        == "desktop-stored"
    )


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
    before = store.room_epoch("agents", "t1")
    stopped = store.request_stop("agents", "t1", ["default", "coder"])
    assert stopped and stopped["run_id"] == submitted.run_id
    assert store.room_epoch("agents", "t1") == before + 1
    assert store.holds("agents", "t1") == {"default", "coder"}
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
    epoch = store.room_epoch("agents", "t1")
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
    old_epoch = store.room_epoch("agents", "t1")
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
