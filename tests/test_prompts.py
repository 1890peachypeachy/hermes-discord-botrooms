from hermes_discord_botrooms.bot_mode.config import BotRoomMember
from hermes_discord_botrooms.bot_mode.models import RoomEvent
from hermes_discord_botrooms.bot_mode.prompts import (
    apply_hold_directive,
    is_pass_text,
    parse_mentions,
    resolve_responders,
    should_commit_turn,
)

MEMBERS = (
    BotRoomMember("researcher", display_name="Research Buddy"),
    BotRoomMember("coder"),
    BotRoomMember("default"),
)


def _event(event_id: int, author_kind: str, text: str) -> RoomEvent:
    return RoomEvent(
        id=event_id,
        event_uid=str(event_id),
        room_id="agents",
        thread_id="thread-1",
        run_id="run",
        kind="message",
        author_kind=author_kind,
        author_id="user" if author_kind == "user" else "researcher",
        author_name="Daniel" if author_kind == "user" else "Research Buddy",
        text=text,
    )


def test_pass_protocol_matches_desktop_forms():
    assert is_pass_text("")
    assert is_pass_text("pass")
    assert is_pass_text("(PASS).")
    assert not is_pass_text("I pass this to @coder")


def test_mentions_support_profile_friendly_and_primary_aliases():
    everyone, mentioned = parse_mentions("@research-buddy and @coder, then @hermes", MEMBERS)
    assert not everyone
    assert mentioned == {"researcher", "coder", "default"}


def test_responder_selection_accumulates_mentions_since_last_user():
    log = [
        _event(1, "user", "old message"),
        _event(2, "member", "old reply"),
        _event(3, "user", "@researcher investigate"),
        _event(4, "member", "@coder validate this"),
    ]
    assert [member.profile for member in resolve_responders(log, MEMBERS)] == [
        "researcher",
        "coder",
    ]


def test_no_mentions_selects_everyone_and_user_mention_is_ignored():
    log = [_event(1, "user", "Please decide; report back to @user")]
    assert resolve_responders(log, MEMBERS) == list(MEMBERS)


def test_holds_are_room_scoped_and_direct_address_releases():
    holds = apply_hold_directive(set(), "@coder stop", MEMBERS)
    assert holds == {"coder"}
    assert apply_hold_directive(holds, "@coder what did you find?", MEMBERS) == set()
    assert apply_hold_directive(set(), "@all pause", MEMBERS) == {
        "researcher",
        "coder",
        "default",
    }


def test_stale_cross_thread_result_can_commit_but_same_thread_result_cannot():
    assert not should_commit_turn(1, 2, newer_user_in_thread=True)
    assert should_commit_turn(1, 2, newer_user_in_thread=False)
