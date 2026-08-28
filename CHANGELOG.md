# Changelog

All notable user-facing changes to Hermes Discord Bot Rooms are documented
here.

## 0.3.0b1 - 2026-08-28

### Added

- Independent Bot Room instances for every Discord thread. Each top-level
  parent-channel message now creates a fresh room with separate agent sessions,
  run state, holds, prompts, attachments, and delivery state.
- Parent-channel `/room-status` aggregation across active threads.
- A guarded, one-time migration for existing Discord rooms, including
  cross-process migration locking and rollback/re-upgrade reconciliation.
- User-facing release and upgrade guidance for existing v0.2 installations.

### Changed

- Replies inside a Discord thread continue only that thread. They no longer
  inherit or mutate state from a sibling thread in the same configured channel.
- `/stop`, `/room-answer`, and `/room-approval` now require a Discord thread;
  the parent channel no longer guesses which room instance to control.
- Public installation examples use HTTPS Git URLs.

### Fixed

- Work queued behind another room on the same Hermes profile can now be
  cancelled before it starts.
- SQLite setup and migration are serialized across participating gateway
  processes.

## 0.2.0

- Initial public beta of persistent multi-profile Hermes Bot Rooms on Discord.
