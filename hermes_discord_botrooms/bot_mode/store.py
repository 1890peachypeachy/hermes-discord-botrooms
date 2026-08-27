"""Durable canonical state for headless Bot Mode rooms.

Only this module knows the SQLite schema. Deleting the plugin data directory
removes the feature's runtime state without touching profile conversations or
normal gateway sessions.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import BotRoomConfig, bot_room_from_mapping
from .models import PendingPrompt, RoomAttachment, RoomEvent, SubmittedRun
from .prompts import apply_hold_directive

# The v2-named ledgers below are additive tables. Keep the compatibility
# marker at 1 so rolling the executable back simply ignores them instead of
# refusing to open an otherwise compatible room database.
SCHEMA_VERSION = 1
MAX_ROLLBACK_COMPAT_SCHEMA_VERSION = 2


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


class RoomStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.directory = self.root / "plugin-data" / "hermes-discord-botrooms"
        self.path = self.directory / "rooms.db"
        self.lock_directory = self.directory / "locks"
        self._setup_lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema(db)
        return db

    def _ensure_schema(self, db: sqlite3.Connection) -> None:
        if self._ready:
            return
        with self._setup_lock:
            if self._ready:
                return
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_room_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_rooms (
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
                CREATE TABLE IF NOT EXISTS bot_room_threads (
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    guild_id TEXT,
                    channel_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (room_id, thread_id),
                    FOREIGN KEY (room_id) REFERENCES bot_rooms(room_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bot_room_runs (
                    run_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    current_member TEXT,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    FOREIGN KEY (room_id) REFERENCES bot_rooms(room_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS bot_room_runs_room_status
                    ON bot_room_runs(room_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS bot_room_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_uid TEXT NOT NULL UNIQUE,
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    run_id TEXT,
                    kind TEXT NOT NULL,
                    author_kind TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    platform_message_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (room_id) REFERENCES bot_rooms(room_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS bot_room_events_thread
                    ON bot_room_events(room_id, thread_id, id);
                CREATE TABLE IF NOT EXISTS bot_room_watermarks (
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    member_key TEXT NOT NULL,
                    event_id INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (room_id, thread_id, member_key)
                );
                CREATE TABLE IF NOT EXISTS bot_room_sessions (
                    room_id TEXT NOT NULL,
                    member_key TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    stored_session_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (room_id, member_key)
                );
                CREATE TABLE IF NOT EXISTS bot_room_prompts (
                    room_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    member_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    runtime_session_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (room_id, member_key)
                );
                CREATE TABLE IF NOT EXISTS bot_room_turn_attempts (
                    run_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    member_key TEXT NOT NULL,
                    runtime_session_id TEXT NOT NULL,
                    stored_session_id TEXT NOT NULL,
                    baseline_row_id INTEGER NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'prepared',
                    result_text TEXT,
                    result_status TEXT,
                    result_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, member_key),
                    FOREIGN KEY (run_id) REFERENCES bot_room_runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bot_room_deliveries (
                    event_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    platform_message_id TEXT,
                    last_error TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (event_id, platform, destination),
                    FOREIGN KEY (event_id) REFERENCES bot_room_events(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bot_room_delivery_chunks (
                    event_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    nonce TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    platform_message_id TEXT,
                    last_error TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (event_id, platform, destination, chunk_index),
                    FOREIGN KEY (event_id) REFERENCES bot_room_events(id) ON DELETE CASCADE
                );
                """
            )
            row = db.execute(
                "SELECT value FROM bot_room_meta WHERE key='schema_version'"
            ).fetchone()
            if row and int(row["value"]) > MAX_ROLLBACK_COMPAT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Bot Mode room database schema {row['value']} is newer than this build"
                )
            db.execute(
                "INSERT INTO bot_room_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._ready = True

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except Exception:
            with contextlib.suppress(Exception):
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _config_payload(room: BotRoomConfig) -> dict[str, Any]:
        return {
            "room_id": room.room_id,
            "display_name": room.display_name,
            "platform": room.platform,
            "guild_id": room.guild_id,
            "channel_id": room.channel_id,
            "controller_profile": room.controller_profile,
            "members": [member.__dict__ for member in room.members],
            "enabled": room.enabled,
            **room.extra,
        }

    def sync_room(self, room: BotRoomConfig, db: sqlite3.Connection | None = None) -> None:
        now = time.time()
        values = (
            room.room_id,
            room.display_name,
            room.platform,
            _json(self._config_payload(room)),
            now,
            now,
        )
        sql = (
            "INSERT INTO bot_rooms(room_id,display_name,platform,config_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET "
            "display_name=excluded.display_name,platform=excluded.platform,"
            "config_json=excluded.config_json,updated_at=excluded.updated_at"
        )
        if db is not None:
            db.execute(sql, values)
            return
        with self.transaction() as owned:
            owned.execute(sql, values)

    def room_configs(self) -> dict[str, BotRoomConfig]:
        with self.read() as db:
            rows = db.execute(
                "SELECT config_json FROM bot_rooms ORDER BY display_name COLLATE NOCASE"
            ).fetchall()
        configs: dict[str, BotRoomConfig] = {}
        for row in rows:
            try:
                room = bot_room_from_mapping(json.loads(row["config_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            configs[room.room_id] = room
        return configs

    def delete_room(self, room_id: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute("DELETE FROM bot_rooms WHERE room_id=?", (room_id,))
        return bool(cursor.rowcount)

    def submit_user_event(
        self,
        room: BotRoomConfig,
        *,
        thread_id: str,
        event_uid: str,
        text: str,
        author_id: str,
        author_name: str,
        attachments: Sequence[RoomAttachment] = (),
        platform_message_id: str = "",
        guild_id: str = "",
        channel_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SubmittedRun:
        now = time.time()
        run_id = uuid.uuid4().hex
        with self.transaction() as db:
            self.sync_room(room, db)
            existing = db.execute(
                "SELECT id,room_id,thread_id,run_id FROM bot_room_events WHERE event_uid=?",
                (event_uid,),
            ).fetchone()
            if existing:
                return SubmittedRun(
                    room_id=existing["room_id"],
                    thread_id=existing["thread_id"],
                    run_id=existing["run_id"],
                    event_id=int(existing["id"]),
                    created=False,
                )
            current = db.execute(
                "SELECT epoch,active_run_id,holds_json FROM bot_rooms WHERE room_id=?",
                (room.room_id,),
            ).fetchone()
            epoch = int(current["epoch"] or 0) + 1
            holds = set(json.loads(current["holds_json"] or "[]"))
            holds = apply_hold_directive(holds, text, room.members)
            previous = str(current["active_run_id"] or "")
            if previous:
                db.execute(
                    "UPDATE bot_room_runs SET status='superseded',stop_requested=1,"
                    "updated_at=?,finished_at=? WHERE run_id=? AND status IN ('queued','running','blocked')",
                    (now, now, previous),
                )
            db.execute(
                "INSERT INTO bot_room_runs(run_id,room_id,thread_id,epoch,status,started_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (run_id, room.room_id, thread_id, epoch, "queued", now, now),
            )
            db.execute(
                "UPDATE bot_rooms SET epoch=?,active_run_id=?,holds_json=?,updated_at=? WHERE room_id=?",
                (epoch, run_id, _json(sorted(holds)), now, room.room_id),
            )
            db.execute(
                "INSERT INTO bot_room_threads(room_id,thread_id,platform,guild_id,channel_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(room_id,thread_id) DO UPDATE SET "
                "guild_id=excluded.guild_id,channel_id=excluded.channel_id,updated_at=excluded.updated_at",
                (
                    room.room_id,
                    thread_id,
                    room.platform,
                    guild_id,
                    channel_id,
                    now,
                    now,
                ),
            )
            cursor = db.execute(
                "INSERT INTO bot_room_events(event_uid,room_id,thread_id,run_id,kind,author_kind,"
                "author_id,author_name,text,attachments_json,platform_message_id,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_uid,
                    room.room_id,
                    thread_id,
                    run_id,
                    "message",
                    "user",
                    author_id,
                    author_name,
                    text,
                    _json([item.as_dict() for item in attachments]),
                    platform_message_id,
                    _json(metadata or {}),
                    now,
                ),
            )
            return SubmittedRun(
                room.room_id,
                thread_id,
                run_id,
                int(cursor.lastrowid),
                True,
                previous,
            )

    @staticmethod
    def _event(row: sqlite3.Row) -> RoomEvent:
        attachments = tuple(
            RoomAttachment(**item) for item in json.loads(row["attachments_json"] or "[]")
        )
        return RoomEvent(
            id=int(row["id"]),
            event_uid=row["event_uid"],
            room_id=row["room_id"],
            thread_id=row["thread_id"],
            run_id=row["run_id"] or "",
            kind=row["kind"],
            author_kind=row["author_kind"],
            author_id=row["author_id"],
            author_name=row["author_name"],
            text=row["text"],
            attachments=attachments,
            created_at=float(row["created_at"]),
            platform_message_id=row["platform_message_id"] or "",
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def thread_events(self, room_id: str, thread_id: str) -> list[RoomEvent]:
        with self.read() as db:
            rows = db.execute(
                "SELECT * FROM bot_room_events WHERE room_id=? AND thread_id=? AND kind='message' ORDER BY id",
                (room_id, thread_id),
            ).fetchall()
        return [self._event(row) for row in rows]

    def events(self, room_id: str, *, thread_id: str = "", limit: int = 384) -> list[RoomEvent]:
        bounded = max(1, min(int(limit), 2000))
        with self.read() as db:
            if thread_id:
                rows = db.execute(
                    "SELECT * FROM bot_room_events WHERE room_id=? AND thread_id=? "
                    "AND kind='message' ORDER BY id DESC LIMIT ?",
                    (room_id, thread_id, bounded),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM bot_room_events WHERE room_id=? AND kind='message' "
                    "ORDER BY id DESC LIMIT ?",
                    (room_id, bounded),
                ).fetchall()
        return [self._event(row) for row in reversed(rows)]

    def event_by_uid(self, event_uid: str) -> RoomEvent | None:
        with self.read() as db:
            row = db.execute(
                "SELECT * FROM bot_room_events WHERE event_uid=?", (event_uid,)
            ).fetchone()
        return self._event(row) if row else None

    def delta_for_member(
        self, room_id: str, thread_id: str, member_key: str
    ) -> tuple[list[RoomEvent], int]:
        with self.read() as db:
            mark = db.execute(
                "SELECT event_id FROM bot_room_watermarks WHERE room_id=? AND thread_id=? AND member_key=?",
                (room_id, thread_id, member_key),
            ).fetchone()
            seen = int(mark["event_id"] if mark else 0)
            rows = db.execute(
                "SELECT * FROM bot_room_events WHERE room_id=? AND thread_id=? "
                "AND kind='message' AND id>? ORDER BY id",
                (room_id, thread_id, seen),
            ).fetchall()
        return [self._event(row) for row in rows], seen

    def advance_watermark(
        self, room_id: str, thread_id: str, member_key: str, event_id: int
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO bot_room_watermarks(room_id,thread_id,member_key,event_id,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(room_id,thread_id,member_key) DO UPDATE SET "
                "event_id=MAX(event_id,excluded.event_id),updated_at=excluded.updated_at",
                (room_id, thread_id, member_key, int(event_id), time.time()),
            )

    def append_agent_event(
        self,
        *,
        room_id: str,
        thread_id: str,
        run_id: str,
        member_key: str,
        member_name: str,
        text: str,
        event_uid: str,
        metadata: dict[str, Any] | None = None,
    ) -> RoomEvent:
        now = time.time()
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO bot_room_events(event_uid,room_id,thread_id,run_id,kind,"
                "author_kind,author_id,author_name,text,attachments_json,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'[]',?,?)",
                (
                    event_uid,
                    room_id,
                    thread_id,
                    run_id,
                    "message",
                    "member",
                    member_key,
                    member_name,
                    text.strip(),
                    _json(metadata or {}),
                    now,
                ),
            )
            inserted = cursor.rowcount > 0
            event_id = int(cursor.lastrowid or 0) if inserted else 0
            if not inserted:
                event_id = int(
                    db.execute(
                        "SELECT id FROM bot_room_events WHERE event_uid=?", (event_uid,)
                    ).fetchone()["id"]
                )
            if inserted:
                db.execute(
                    "UPDATE bot_room_runs SET message_count=message_count+1,updated_at=? WHERE run_id=?",
                    (now, run_id),
                )
            row = db.execute("SELECT * FROM bot_room_events WHERE id=?", (event_id,)).fetchone()
        return self._event(row)

    def room_epoch(self, room_id: str) -> int:
        with self.read() as db:
            row = db.execute("SELECT epoch FROM bot_rooms WHERE room_id=?", (room_id,)).fetchone()
        return int(row["epoch"] if row else 0)

    def run_row(self, run_id: str) -> dict[str, Any] | None:
        with self.read() as db:
            row = db.execute("SELECT * FROM bot_room_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def recoverable_runs(
        self, room_ids: Sequence[str], *, before: float | None = None
    ) -> list[dict[str, Any]]:
        """Return active durable runs that need a scheduler after restart.

        The room-level file lock remains the cross-process claim. Two gateway
        processes may discover the same row, but only the first can execute it;
        the second re-checks the terminal status after acquiring that lock.
        """

        identifiers = tuple(dict.fromkeys(str(item) for item in room_ids if item))
        if not identifiers:
            return []
        placeholders = ",".join("?" for _ in identifiers)
        with self.transaction() as db:
            now = time.time()
            stopping = db.execute(
                f"SELECT run_id FROM bot_room_runs WHERE room_id IN ({placeholders}) "
                "AND status='stopping'",
                identifiers,
            ).fetchall()
            stopping_ids = [str(row["run_id"]) for row in stopping]
            if stopping_ids:
                stop_marks = ",".join("?" for _ in stopping_ids)
                db.execute(
                    f"UPDATE bot_room_runs SET status='stopped',updated_at=?,finished_at=? "
                    f"WHERE run_id IN ({stop_marks})",
                    (now, now, *stopping_ids),
                )
                db.execute(
                    f"UPDATE bot_rooms SET active_run_id=NULL,updated_at=? "
                    f"WHERE active_run_id IN ({stop_marks})",
                    (now, *stopping_ids),
                )
            cutoff = float(before if before is not None else now)
            rows = db.execute(
                f"SELECT runs.* FROM bot_room_runs AS runs "
                "JOIN bot_rooms AS rooms ON rooms.active_run_id=runs.run_id "
                f"WHERE runs.room_id IN ({placeholders}) "
                "AND runs.status IN ('queued','running','blocked') "
                "AND runs.started_at<? "
                "ORDER BY runs.started_at",
                (*identifiers, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def newer_user_in_thread(self, room_id: str, thread_id: str, after_id: int) -> bool:
        with self.read() as db:
            row = db.execute(
                "SELECT 1 FROM bot_room_events WHERE room_id=? AND thread_id=? "
                "AND author_kind='user' AND id>? LIMIT 1",
                (room_id, thread_id, int(after_id)),
            ).fetchone()
        return row is not None

    def holds(self, room_id: str) -> set[str]:
        with self.read() as db:
            row = db.execute(
                "SELECT holds_json FROM bot_rooms WHERE room_id=?", (room_id,)
            ).fetchone()
        return set(json.loads(row["holds_json"] or "[]")) if row else set()

    def update_run(
        self,
        run_id: str,
        status: str,
        *,
        current_member: str | None = None,
        error: str | None = None,
        finished: bool = False,
    ) -> None:
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "UPDATE bot_room_runs SET status=?,current_member=?,error=?,updated_at=?,"
                "finished_at=CASE WHEN ? THEN ? ELSE finished_at END WHERE run_id=?",
                (status, current_member, error, now, int(finished), now, run_id),
            )
            if finished:
                db.execute(
                    "UPDATE bot_rooms SET active_run_id=NULL,updated_at=? WHERE active_run_id=?",
                    (now, run_id),
                )

    def request_stop(self, room_id: str, thread_id: str = "") -> dict[str, Any] | None:
        now = time.time()
        with self.transaction() as db:
            sql = (
                "SELECT * FROM bot_room_runs WHERE room_id=? AND status IN ('queued','running','blocked')"
                + (" AND thread_id=?" if thread_id else "")
                + " ORDER BY started_at DESC LIMIT 1"
            )
            args: tuple[Any, ...] = (room_id, thread_id) if thread_id else (room_id,)
            row = db.execute(sql, args).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE bot_room_runs SET stop_requested=1,status='stopping',updated_at=? WHERE run_id=?",
                (now, row["run_id"]),
            )
            return dict(row)

    def run_should_stop(self, run_id: str) -> bool:
        row = self.run_row(run_id)
        return (
            not row
            or bool(row["stop_requested"])
            or row["status"]
            in {
                "superseded",
                "stopping",
                "stopped",
            }
        )

    def save_session(
        self, room_id: str, member_key: str, profile: str, stored_session_id: str
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO bot_room_sessions(room_id,member_key,profile,stored_session_id,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(room_id,member_key) DO UPDATE SET "
                "profile=excluded.profile,stored_session_id=excluded.stored_session_id,"
                "updated_at=excluded.updated_at",
                (room_id, member_key, profile, stored_session_id, time.time()),
            )

    def session_id(self, room_id: str, member_key: str) -> str:
        with self.read() as db:
            row = db.execute(
                "SELECT stored_session_id FROM bot_room_sessions WHERE room_id=? AND member_key=?",
                (room_id, member_key),
            ).fetchone()
        return str(row["stored_session_id"] or "") if row else ""

    def set_pending_prompt(self, prompt: PendingPrompt) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO bot_room_prompts(room_id,run_id,thread_id,member_key,kind,request_id,"
                "runtime_session_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(room_id,member_key) DO UPDATE SET run_id=excluded.run_id,"
                "thread_id=excluded.thread_id,kind=excluded.kind,request_id=excluded.request_id,"
                "runtime_session_id=excluded.runtime_session_id,payload_json=excluded.payload_json,"
                "created_at=excluded.created_at",
                (
                    prompt.room_id,
                    prompt.run_id,
                    prompt.thread_id,
                    prompt.member_key,
                    prompt.kind,
                    prompt.request_id,
                    prompt.runtime_session_id,
                    _json(prompt.payload),
                    time.time(),
                ),
            )

    def pending_prompt(self, room_id: str, member_key: str = "") -> PendingPrompt | None:
        with self.read() as db:
            if member_key:
                row = db.execute(
                    "SELECT * FROM bot_room_prompts WHERE room_id=? AND member_key=?",
                    (room_id, member_key),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM bot_room_prompts WHERE room_id=? ORDER BY created_at LIMIT 1",
                    (room_id,),
                ).fetchone()
        if not row:
            return None
        return PendingPrompt(
            room_id=row["room_id"],
            run_id=row["run_id"],
            thread_id=row["thread_id"],
            member_key=row["member_key"],
            kind=row["kind"],
            request_id=row["request_id"],
            payload=json.loads(row["payload_json"] or "{}"),
            runtime_session_id=row["runtime_session_id"],
        )

    def clear_pending_prompt(self, room_id: str, member_key: str) -> None:
        with self.transaction() as db:
            db.execute(
                "DELETE FROM bot_room_prompts WHERE room_id=? AND member_key=?",
                (room_id, member_key),
            )

    def clear_run_prompts(self, run_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM bot_room_prompts WHERE run_id=?", (run_id,))

    def save_turn_attempt(
        self,
        *,
        run_id: str,
        room_id: str,
        thread_id: str,
        member_key: str,
        runtime_session_id: str,
        stored_session_id: str,
        baseline_row_id: int,
    ) -> None:
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO bot_room_turn_attempts("
                "run_id,room_id,thread_id,member_key,runtime_session_id,stored_session_id,"
                "baseline_row_id,phase,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,member_key) DO UPDATE SET "
                "runtime_session_id=excluded.runtime_session_id,"
                "stored_session_id=excluded.stored_session_id,"
                "baseline_row_id=excluded.baseline_row_id,phase='prepared',"
                "result_text=NULL,result_status=NULL,result_error=NULL,"
                "updated_at=excluded.updated_at",
                (
                    run_id,
                    room_id,
                    thread_id,
                    member_key,
                    runtime_session_id,
                    stored_session_id,
                    int(baseline_row_id),
                    "prepared",
                    now,
                    now,
                ),
            )

    def mark_turn_dispatched(self, run_id: str, member_key: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE bot_room_turn_attempts SET phase='dispatched',updated_at=? "
                "WHERE run_id=? AND member_key=?",
                (time.time(), run_id, member_key),
            )

    def save_turn_result(
        self,
        run_id: str,
        member_key: str,
        *,
        text: str,
        status: str,
        error: str = "",
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE bot_room_turn_attempts SET phase='terminal',result_text=?,"
                "result_status=?,result_error=?,updated_at=? "
                "WHERE run_id=? AND member_key=?",
                (text, status, error, time.time(), run_id, member_key),
            )

    def turn_attempt(self, run_id: str, member_key: str) -> dict[str, Any] | None:
        with self.read() as db:
            row = db.execute(
                "SELECT * FROM bot_room_turn_attempts WHERE run_id=? AND member_key=?",
                (run_id, member_key),
            ).fetchone()
        return dict(row) if row else None

    def clear_turn_attempt(self, run_id: str, member_key: str) -> None:
        with self.transaction() as db:
            db.execute(
                "DELETE FROM bot_room_turn_attempts WHERE run_id=? AND member_key=?",
                (run_id, member_key),
            )

    def clear_run_turn_attempts(self, run_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM bot_room_turn_attempts WHERE run_id=?", (run_id,))

    def status(self, room_id: str, thread_id: str = "") -> dict[str, Any]:
        with self.read() as db:
            room = db.execute("SELECT * FROM bot_rooms WHERE room_id=?", (room_id,)).fetchone()
            if not room:
                return {"room_id": room_id, "status": "not_found"}
            sql = "SELECT * FROM bot_room_runs WHERE room_id=?"
            args: tuple[Any, ...] = (room_id,)
            if thread_id:
                sql += " AND thread_id=?"
                args = (room_id, thread_id)
            sql += " ORDER BY started_at DESC LIMIT 1"
            run = db.execute(sql, args).fetchone()
            prompts = db.execute(
                "SELECT member_key,kind,request_id,payload_json FROM bot_room_prompts WHERE room_id=?",
                (room_id,),
            ).fetchall()
        return {
            "room_id": room_id,
            "display_name": room["display_name"],
            "epoch": int(room["epoch"]),
            "active_run_id": room["active_run_id"],
            "holds": json.loads(room["holds_json"] or "[]"),
            "run": dict(run) if run else None,
            "pending_prompts": [
                {
                    "member_key": item["member_key"],
                    "kind": item["kind"],
                    "request_id": item["request_id"],
                    "payload": json.loads(item["payload_json"] or "{}"),
                }
                for item in prompts
            ],
        }

    def record_delivery(
        self,
        event_id: int,
        *,
        platform: str,
        destination: str,
        status: str,
        platform_message_id: str = "",
        error: str = "",
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO bot_room_deliveries(event_id,platform,destination,status,attempts,"
                "platform_message_id,last_error,updated_at) VALUES(?,?,?,?,1,?,?,?) "
                "ON CONFLICT(event_id,platform,destination) DO UPDATE SET status=excluded.status,"
                "attempts=bot_room_deliveries.attempts+1,platform_message_id=excluded.platform_message_id,"
                "last_error=excluded.last_error,updated_at=excluded.updated_at",
                (
                    event_id,
                    platform,
                    destination,
                    status,
                    platform_message_id,
                    error,
                    time.time(),
                ),
            )

    def delivered_message_id(self, event_id: int, *, platform: str, destination: str) -> str:
        with self.read() as db:
            row = db.execute(
                "SELECT platform_message_id FROM bot_room_deliveries "
                "WHERE event_id=? AND platform=? AND destination=? AND status='delivered'",
                (int(event_id), platform, destination),
            ).fetchone()
        return str(row["platform_message_id"] or "") if row else ""

    def delivered_chunk_message_id(
        self,
        event_id: int,
        *,
        platform: str,
        destination: str,
        chunk_index: int,
    ) -> str:
        with self.read() as db:
            row = db.execute(
                "SELECT platform_message_id FROM bot_room_delivery_chunks "
                "WHERE event_id=? AND platform=? AND destination=? AND chunk_index=? "
                "AND status='delivered'",
                (int(event_id), platform, destination, int(chunk_index)),
            ).fetchone()
        return str(row["platform_message_id"] or "") if row else ""

    def delivery_chunk_status(
        self,
        event_id: int,
        *,
        platform: str,
        destination: str,
        chunk_index: int,
    ) -> str:
        with self.read() as db:
            row = db.execute(
                "SELECT status FROM bot_room_delivery_chunks "
                "WHERE event_id=? AND platform=? AND destination=? AND chunk_index=?",
                (int(event_id), platform, destination, int(chunk_index)),
            ).fetchone()
        return str(row["status"] or "") if row else ""

    def record_delivery_chunk(
        self,
        event_id: int,
        *,
        platform: str,
        destination: str,
        chunk_index: int,
        nonce: str,
        status: str,
        platform_message_id: str = "",
        error: str = "",
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO bot_room_delivery_chunks("
                "event_id,platform,destination,chunk_index,nonce,status,attempts,"
                "platform_message_id,last_error,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?) "
                "ON CONFLICT(event_id,platform,destination,chunk_index) DO UPDATE SET "
                "nonce=excluded.nonce,status=excluded.status,attempts=attempts+1,"
                "platform_message_id=excluded.platform_message_id,"
                "last_error=excluded.last_error,updated_at=excluded.updated_at",
                (
                    int(event_id),
                    platform,
                    destination,
                    int(chunk_index),
                    nonce,
                    status,
                    platform_message_id,
                    error,
                    time.time(),
                ),
            )

    def begin_delivery_chunk(
        self,
        event_id: int,
        *,
        platform: str,
        destination: str,
        chunk_index: int,
        nonce: str,
    ) -> None:
        """Atomically suppress old and new executables before an HTTP POST."""
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO bot_room_delivery_chunks("
                "event_id,platform,destination,chunk_index,nonce,status,attempts,"
                "platform_message_id,last_error,updated_at) VALUES(?,?,?,?,?,'unknown',1,'',?,?) "
                "ON CONFLICT(event_id,platform,destination,chunk_index) DO UPDATE SET "
                "nonce=excluded.nonce,status='unknown',attempts=attempts+1,"
                "platform_message_id='',last_error=excluded.last_error,updated_at=excluded.updated_at",
                (
                    int(event_id),
                    platform,
                    destination,
                    int(chunk_index),
                    nonce,
                    "awaiting platform response",
                    now,
                ),
            )
            # Chunk-unaware rollback builds only consult this event-level row.
            # A delivered sentinel makes those builds fail closed while the
            # current build can recognize the prefix and reconcile chunks.
            db.execute(
                "INSERT INTO bot_room_deliveries(event_id,platform,destination,status,attempts,"
                "platform_message_id,last_error,updated_at) VALUES(?,?,?,'delivered',1,?,?,?) "
                "ON CONFLICT(event_id,platform,destination) DO UPDATE SET status='delivered',"
                "attempts=bot_room_deliveries.attempts+1,platform_message_id=excluded.platform_message_id,"
                "last_error=excluded.last_error,updated_at=excluded.updated_at",
                (
                    int(event_id),
                    platform,
                    destination,
                    f"ambiguous:{nonce}",
                    "delivery outcome pending",
                    now,
                ),
            )

    def fail_delivery_chunk_definitively(
        self,
        event_id: int,
        *,
        platform: str,
        destination: str,
        chunk_index: int,
        nonce: str,
        error: str,
    ) -> None:
        """Make one explicitly rejected chunk retryable in the current build.

        The event-level delivered sentinel intentionally remains in place so a
        chunk-unaware rollback build cannot replay earlier chunks. Current code
        recognizes the sentinel and resumes from the failed chunk.
        """
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "UPDATE bot_room_delivery_chunks SET nonce=?,status='failed',"
                "platform_message_id='',last_error=?,updated_at=? "
                "WHERE event_id=? AND platform=? AND destination=? AND chunk_index=?",
                (
                    nonce,
                    error,
                    now,
                    int(event_id),
                    platform,
                    destination,
                    int(chunk_index),
                ),
            )

    def undelivered_member_events(
        self,
        room_id: str,
        *,
        platform: str,
        limit: int = 100,
        after_id: int = 0,
    ) -> list[RoomEvent]:
        bounded = max(1, min(int(limit), 1000))
        with self.read() as db:
            rows = db.execute(
                "SELECT events.* FROM bot_room_events AS events "
                "WHERE events.room_id=? AND events.kind='message' "
                "AND events.author_kind='member' AND events.id>? AND NOT EXISTS ("
                "SELECT 1 FROM bot_room_deliveries AS deliveries "
                "WHERE deliveries.event_id=events.id AND deliveries.platform=? "
                "AND deliveries.destination=events.thread_id "
                "AND deliveries.status='delivered' "
                "AND deliveries.platform_message_id NOT LIKE 'ambiguous:%') "
                "ORDER BY events.id LIMIT ?",
                (room_id, int(after_id), platform, bounded),
            ).fetchall()
        return [self._event(row) for row in rows]


@contextlib.contextmanager
def room_run_lock(store: RoomStore, room_id: str) -> Iterator[None]:
    """Cross-process exclusive lock for one room's scheduler."""

    store.lock_directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in room_id)
    path = store.lock_directory / f"{safe}.lock"
    handle = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
