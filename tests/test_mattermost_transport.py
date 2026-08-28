"""Unit tests for the Mattermost transport (no network)."""

from hermes_discord_botrooms.bot_mode.mattermost_transport import (
    MAX_CHUNKS,
    _chunks,
)


def test_chunks_empty_and_short():
    assert _chunks("") == []
    assert _chunks("   ") == []
    assert _chunks("hello room") == ["hello room"]


def test_chunks_split_on_newlines_within_limit():
    text = "\n".join(f"line {i}" for i in range(500))
    chunks = _chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= 3900 for c in chunks)
    # Reassembly preserves all content (modulo split points).
    assert "\n".join(chunks).replace("\n\n", "\n").count("line ") == 500


def test_chunks_hard_split_when_no_breakpoint():
    text = "x" * 10000
    chunks = _chunks(text)
    assert all(len(c) <= 3900 for c in chunks)


def test_chunks_truncation_notice_at_cap():
    text = "y" * (3900 * MAX_CHUNKS + 500)
    chunks = _chunks(text)
    assert len(chunks) == MAX_CHUNKS
    assert "truncated" in chunks[-1]
