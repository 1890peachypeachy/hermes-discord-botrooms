# Official Bot Mode parity

This plugin is a bridge into real Hermes profile sessions. Its room scheduler
is a reviewed Python port of the conversation logic in official Hermes Bot
Mode, not a second model router.

The current reference is official Hermes commit
[`e078b2fe`](https://github.com/NousResearch/hermes-agent/blob/e078b2fe7c02ae08902704f573e64ab68d57a70f/apps/desktop/src/plugins/hermes-bots/plugin.js).
The machine-readable snapshot is in
[`parity/hermes-bot-mode.json`](../parity/hermes-bot-mode.json).

## Kept in sync

- deterministic `@mention` routing and serial round robin
- 3 rounds, 10 messages, 2 unresolved-handoff continuations, and 24 history entries
- selective pass behavior and substantive terminal reply selection
- persistent member holds and direct-mention release
- friendly empty-response handling and adjacent duplicate-echo suppression
- honest `settled`, `capped`, `stopped`, and superseded outcomes

## Deliberate Discord differences

The plugin keeps SQLite run and delivery ledgers, Discord typing indicators,
attachments, approval relays, restart recovery, and idempotent message
delivery. Superseded or timed-out turns are interrupted promptly. Room members
are told not to use Agent Inbox with each other inside the room; their normal
chats and coordination outside Bot Rooms are unchanged.

Desktop UI layout and cross-machine room synchronization are outside this
plugin's scope.

## Drift policy

The daily GitHub workflow hashes only the official coordination region. If it
changes, the workflow opens or updates one issue for human review. It never
changes this plugin automatically. Maintainers review the official diff, port
the relevant behavior, update tests, and then advance the pinned snapshot.

Run the same check manually:

```bash
python scripts/check_upstream_parity.py --json
```
