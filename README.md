# Hermes Discord Bot Rooms

Turn 2–6 existing Hermes profiles into a Discord group room. Each profile keeps
its own model, memory, tools, SOUL, and bot identity; the plugin coordinates
serial replies, persistent room context, typing indicators, attachments,
approvals, and restart recovery.

The configured Discord channel is a room template: every new top-level message
creates a separate Bot Room thread with fresh agent context. Replies inside that
thread continue only that room.

Normal one-to-one profile chats and Hermes agent-to-agent messaging tools are
unchanged outside Bot Rooms.

> **Beta:** users need a compatible Hermes build. The compatibility check is
> mandatory. This checkout documents **v0.3.0 beta 1** (`0.3.0b1`).

Use a dedicated Discord channel for each configured room. A top-level human
message in that channel always starts a new Bot Room; it is not treated as a
follow-up to an older thread.

## Install with an agent (recommended)

Before starting, the user needs:

- Hermes on macOS or Linux with Python 3.11–3.13
- 2–6 existing Hermes profiles
- one Discord bot identity per participating profile
- the Discord server and channel IDs

Bot Rooms does not install Hermes or create profiles. Complete
[Hermes Agent](https://github.com/NousResearch/hermes-agent) setup first. The
recommended installer is a coding agent with terminal access to the machine;
the supplied prompt requires it to inspect first and ask before every change.

Send [`https://github.com/DanielOu1208/hermes-discord-botrooms`](https://github.com/DanielOu1208/hermes-discord-botrooms)
to a coding agent with the following prompt:

> Clone this repository and follow its README and linked documentation. Inspect
> my Hermes profiles, gateways, active runtime, and Discord readiness without
> changing anything or exposing credentials. Tell me what is ready or missing.
> Ask me to choose 2–6 profiles, one controller, a room ID, Discord server ID,
> and channel ID. First show me the exact plugin commit and ask permission to
> install it. After installation, show me the redacted room dry run and ask
> again before writing configuration or restarting selected gateways. Complete
> the live Discord acceptance checklist. Never display or ask me to paste a
> Discord token in chat.

The agent should:

1. Run the compatibility probe and plugin doctor before modifying anything.
2. Report profile, gateway, plugin-revision, and Discord-identity readiness
   without printing credentials.
3. Ask for the room choices and approval of the exact commit.
4. Install the pinned commit, show `hermes botrooms setup --dry-run --json`,
   and ask for approval of config changes and gateway restarts.
5. Apply the setup, run `hermes botrooms doctor --live`, and complete the real
   Discord test.

Creating Discord applications, enabling intents, inviting bots, and changing
channel permissions remain user-assisted steps in the user's signed-in Discord
account.

## Manual quick start

Run from the Python environment where `hermes` is installed:

```bash
git clone https://github.com/DanielOu1208/hermes-discord-botrooms.git &&
cd hermes-discord-botrooms &&
python -m hermes_discord_botrooms.compat --json &&
hermes plugins doctor . --ci
```

If both checks pass, follow the [manual installation guide](docs/manual-install.md).
If compatibility fails, stop and read the guarded
[Hermes compatibility procedure](docs/hermes-compatibility.md).

Already running v0.2? Read the
[v0.3.0 beta 1 upgrade notes](docs/releases/v0.3.0-beta.1.md) before replacing
any installed plugin copy.

## Documentation

- [Agent installation checklist](docs/agent-installation.md) — read-only
  discovery, two approval gates, rollout, and evidence boundaries
- [Manual installation](docs/manual-install.md) — pinned install, guided setup,
  and non-interactive dry runs
- [Discord setup](docs/discord-setup.md) — bots, intents, permissions, tokens,
  and IDs
- [Hermes compatibility](docs/hermes-compatibility.md) — preflight and the
  exact-revision compatibility patch
- [Official Bot Mode parity](docs/parity.md) — synchronized coordination
  behavior, deliberate Discord differences, and drift alerts
- [Operations and rollback](docs/operations.md) — status, live acceptance,
  removal, backup restore, and security boundaries
- [v0.3.0 beta 1 release and upgrade notes](docs/releases/v0.3.0-beta.1.md) —
  behavior changes, migration effects, and the upgrade checklist
- [Changelog](CHANGELOG.md) — release history

## Core behavior

- one controller receives each top-level human message and creates a fresh,
  isolated room thread
- selected members run serially and can respond selectively with `@mentions`
- each working member shows its own typing indicator and posts through its own
  Discord bot
- configured room channels are reserved so ordinary gateways do not duplicate
  replies
- each thread's sessions, run state, delivery state, and attachments survive
  restarts without leaking into sibling threads
- `/room-status` in the parent channel summarizes active threads; mutating
  controls such as `/stop` must be used inside the target thread
- bot tokens stay in existing Hermes profile credential stores

For example, `@coder review this` routes the turn to the room member whose
profile or configured handle is `coder`; no routing mention means every member
may respond. Selecting a Discord bot with the mention picker works too. Bot
responses render these handles as plain text and do not ping Discord users.

The room uses real Hermes profile sessions and a reviewed Python port of the
[official Bot Mode coordination logic](docs/parity.md), with Discord-specific
durability and delivery safeguards.

## Development

```bash
python -m pytest
ruff check .
```
