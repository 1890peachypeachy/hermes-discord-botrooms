# Hermes Discord Bot Rooms

Turn 2–6 existing Hermes profiles into a Discord group room. Each profile keeps
its own model, memory, tools, SOUL, and bot identity; the plugin coordinates
serial replies, persistent room context, typing indicators, attachments,
approvals, and restart recovery.

Normal profile chats are unchanged. Room-only instructions do not disable
Agent Inbox or `message_agent` anywhere else.

> **Beta:** users need a compatible Hermes build. The compatibility check is
> mandatory.

## Install with an agent (recommended)

Before starting, the user needs:

- Hermes on macOS or Linux with Python 3.11–3.13
- 2–6 existing Hermes profiles
- one Discord bot identity per participating profile
- the Discord server and channel IDs

Send this repository URL to a coding agent with the following prompt:

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
git clone git@github.com:DanielOu1208/hermes-discord-botrooms.git &&
cd hermes-discord-botrooms &&
python -m hermes_discord_botrooms.compat --json &&
hermes plugins doctor . --ci
```

If both checks pass, follow the [manual installation guide](docs/manual-install.md).
If compatibility fails, stop and read the guarded
[Hermes compatibility procedure](docs/hermes-compatibility.md).

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

## Core behavior

- one controller receives human messages and creates the room thread
- selected members run serially and can respond selectively with `@mentions`
- each working member shows its own typing indicator and posts through its own
  Discord bot
- configured room channels are reserved so ordinary gateways do not duplicate
  replies
- room sessions, delivery state, and attachments survive restarts
- bot tokens stay in existing Hermes profile credential stores

The room uses real Hermes profile sessions and a reviewed Python port of the
[official Bot Mode coordination logic](docs/parity.md), with Discord-specific
durability and delivery safeguards.

## Development

```bash
python -m pytest
ruff check .
```
