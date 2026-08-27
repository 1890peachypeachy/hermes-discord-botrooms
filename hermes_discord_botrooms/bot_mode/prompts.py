"""Pure Bot Mode selection, mention, hold, and prompt semantics."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .config import BotRoomMember
from .models import RoomEvent

MAX_ROUNDS = 3
MAX_MESSAGES = 10
HISTORY_LIMIT = 24
ROOM_PROTOCOL_VERSION = 1


def room_system_instructions() -> str:
    """System-level boundary for sessions owned by the room scheduler."""

    return (
        "This is a Hermes Discord Bot Rooms member session. The room scheduler "
        "directly invokes every profile seated in this room. Coordinate with a "
        "seated teammate only by mentioning their @handle in your single room "
        "reply. Do not open Agent Inbox, message_agent, another profile chat, or "
        "a private handoff to a teammate already seated here. Those mechanisms "
        "remain available in normal conversations outside this room and for "
        "explicitly requested targets who are not room members. Never reveal "
        "content from private conversations."
    )


def is_pass_text(text: str) -> bool:
    value = str(text or "").strip()
    return not value or re.fullmatch(r"\(?\s*pass\s*\)?\.?", value, re.I) is not None


def parse_mentions(text: str, members: Sequence[BotRoomMember]) -> tuple[bool, set[str]]:
    handles: dict[str, str] = {}
    for member in members:
        for form in member.mention_forms:
            handles[form] = member.key
    mentioned: set[str] = set()
    everyone = False
    for match in re.finditer(r"@([a-z0-9][a-z0-9._-]*)", str(text or ""), re.I):
        handle = match.group(1).lower()
        if handle in {"everyone", "all"}:
            everyone = True
            continue
        if handle == "user":
            continue
        resolved = handles.get(handle) or handles.get(re.sub(r"[._-]+", "", handle))
        if resolved:
            mentioned.add(resolved)
    return everyone, mentioned


def resolve_responders(
    log: Sequence[RoomEvent], members: Sequence[BotRoomMember]
) -> list[BotRoomMember]:
    since_last_user: Sequence[RoomEvent] = ()
    for index in range(len(log) - 1, -1, -1):
        if log[index].author_kind == "user":
            since_last_user = log[index:]
            break
    mentioned: set[str] = set()
    everyone = False
    for entry in since_last_user:
        entry_everyone, entry_mentions = parse_mentions(entry.text, members)
        everyone = everyone or entry_everyone
        mentioned.update(entry_mentions)
    if everyone or not mentioned:
        return list(members)
    return [member for member in members if member.key in mentioned]


def rotate_speakers(members: Sequence[BotRoomMember], round_number: int) -> list[BotRoomMember]:
    if len(members) < 2:
        return list(members)
    shift = round_number % len(members)
    return [*members[shift:], *members[:shift]]


def format_room_line(entry: RoomEvent, viewer: BotRoomMember) -> str:
    attached = "".join(
        f" [attached {'PDF' if item.kind == 'pdf' else item.kind}: {item.name or item.kind}]"
        for item in entry.attachments
    )
    if entry.author_kind == "user":
        return f"{entry.author_name or 'User'} (user): {entry.text}{attached}"
    suffix = " (you)" if entry.author_id == viewer.key else ""
    source = f" [{entry.metadata.get('source')}]" if entry.metadata.get("source") else ""
    return f"{entry.author_name}{suffix}{source}: {entry.text}{attached}"


def build_turn_prompt(
    *,
    group_name: str,
    members: Sequence[BotRoomMember],
    viewer: BotRoomMember,
    delta_lines: Iterable[str],
) -> str:
    peers = [member for member in members if member.key != viewer.key]
    peer_names = ", ".join(
        (
            f"{member.label} (@{member.mention_handle})"
            if member.display_name
            else f"@{member.mention_handle}"
        )
        + (
            f" [on {member.connection_label or member.connection_id}]"
            if member.connection_id
            else ""
        )
        for member in peers
    )
    lines = [
        f'[Group chat: "{group_name}"] You are @{viewer.mention_handle}, one participant in a group chat with {peer_names or "no one else yet"} and the user.',
        "",
        "New messages in the room since your last turn (oldest first):",
        *(f"  {line}" for line in delta_lines),
        "",
        "Rules for this room:",
        "- Reply with ONE conversational message ONLY if you have something new worth adding: build on what was just said, claim or hand off work, answer a question aimed at you, or report a real result. Keep chatter short (1-3 sentences) — but when you are delivering a result, an answer the user asked for, or substantive work, give it at full quality and length; never thin out real content to fit the room.",
        '- If you have nothing new to add, reply with exactly "(pass)". Passing is good — it lets the conversation settle.',
        "- Mention a teammate as @name to pull them in; mention @user only for a judgment call or a result the user needs. Do not repeat points already made.",
        "- Do not use Agent Inbox, message_agent, another profile chat, or a private handoff for a teammate already seated in this room. The room scheduler performs that coordination.",
        "- Never reveal content from your private 1:1 chats. Your reply text goes to the room verbatim — no preamble, no meta-commentary.",
    ]
    return "\n".join(lines)


def should_commit_turn(
    dispatch_epoch: int, current_epoch: int, newer_user_in_thread: bool = True
) -> bool:
    return dispatch_epoch == current_epoch or not newer_user_in_thread


def apply_hold_directive(
    holds: set[str],
    text: str,
    members: Sequence[BotRoomMember],
) -> set[str]:
    everyone, mentioned = parse_mentions(text, members)
    stop = re.search(r"\b(stop|halt|pause)\b", text or "", re.I) is not None
    resume = re.search(r"\b(resume|continue|go|proceed)\b", text or "", re.I) is not None
    updated = set(holds)
    if stop:
        updated.update(member.key for member in members) if everyone else updated.update(mentioned)
    elif resume:
        updated.clear() if everyone else updated.difference_update(mentioned)
    else:
        updated.difference_update(mentioned)
    return updated
