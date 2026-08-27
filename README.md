# Hermes Discord Bot Rooms

Turn any 2–6 existing Hermes profiles into a real Discord group room. Each
profile keeps its own model, memory, tools, SOUL, and Discord bot identity.
One controller receives human messages; the room scheduler runs members in
serial rounds and posts every answer through the bot that produced it.

This is a standalone Hermes plugin. It does not replace your Hermes install,
create profiles for you, or turn several independent auto-replying bots loose
in one channel.

## What it includes

- Real Discord threads for room conversations
- Selective `@mentions`, quiet `(pass)` responses, and bounded rounds
- Persistent per-member room sessions
- Each working member's own Discord typing indicator
- Images, PDFs, and ordinary file attachments
- Clarification and approval controls
- Durable restart recovery and duplicate-delivery protection
- Interactive setup plus deterministic `--dry-run` and `--json` modes

Normal profile chats are unchanged. Room instructions apply only inside the
hidden sessions owned by this plugin, so Agent Inbox and `message_agent` remain
available elsewhere.

## Requirements

- Python 3.11–3.13
- macOS or Linux
- An installed Hermes version accepted by `hermes botrooms doctor --pre-install`
- 2–6 existing local Hermes profiles
- One Discord application and bot token per participating profile
- Every bot can view the room channel, read its history, and send in threads
- The controller can create public threads
- Message Content intent enabled for every bot

The plugin refuses incompatible Hermes builds. It does not monkey-patch core
files or silently weaken Discord authorization.

## Install

This repository is currently a private beta. Your machine must already have
Git access to the repository. Clone it and run the compatibility check before
installing anything:

```bash
git clone git@github.com:DanielOu1208/hermes-discord-botrooms.git
cd hermes-discord-botrooms
python -m hermes_discord_botrooms.compat --json
hermes plugins doctor . --ci
```

Then install the reviewed revision at its full commit SHA:

```bash
hermes plugins install git@github.com:DanielOu1208/hermes-discord-botrooms.git \
  --enable \
  --ref <40-character-commit-sha>

hermes botrooms doctor --pre-install
hermes botrooms setup --restart
```

The setup wizard discovers profile names from the current Hermes installation.
It does not assume a default profile, particular agent names, a home-directory
layout, or a specific service manager.

For automation, preview first:

```bash
hermes botrooms setup \
  --room-id <room-id> \
  --profiles <profile-a>,<profile-b>,<profile-c> \
  --controller <profile-a> \
  --guild-id <discord-server-id> \
  --channel-id <discord-channel-id> \
  --non-interactive \
  --yes \
  --dry-run \
  --json
```

Remove `--dry-run` and run again after reviewing the JSON. Setup restarts only
the selected profiles' gateways by default; `--no-restart` is an explicit
development escape hatch and is not safe for an active Discord room rollout.

## Let an agent install it

Send the repository URL to your coding agent and say:

> Follow the repository's agent guidance and `AGENT_INSTALL.md`. Inspect my
> existing Hermes setup, show me a dry run, and install Discord Bot Rooms for
> the profiles I select. Never display or ask me to paste a Discord token in
> chat.

The agent instructions separate safe automatic work from Discord Developer
Portal steps that require your authenticated account.

## Hermes compatibility

Bot Rooms uses two small machine-client hooks: a session-only instruction
append and opt-in tool lifecycle events.
`python -m hermes_discord_botrooms.compat` verifies those hooks without
changing the Hermes installation. The plugin also recognizes the original
in-core Bot Mode bridge used by early testers. It refuses any other build
instead of editing Hermes behind the user's back.

Official Hermes `main` does not yet expose the versioned hook as of this
private-beta revision. The repository includes a patch pinned to one exact
Hermes commit under `patches/`; CI applies that exact combination and runs the
compatibility probe. The agent installation playbook explains the guarded
source-checkout path. Do not apply the patch to another Hermes revision.

## Discord setup

For every participating profile:

1. Create a Discord application and bot in the Discord Developer Portal.
2. Enable Message Content intent. Enable Server Members intent when your
   Hermes authorization rules resolve usernames or roles.
3. Invite the bot with `bot` and `applications.commands` scopes.
4. Grant View Channel, Send Messages, Read Message History, Send Messages in
   Threads, and Attach Files. Grant Create Public Threads to the controller.
5. Store its token using Hermes setup for that profile. Do not put tokens in
   this repository or in the Bot Rooms configuration.

## Operations

```bash
hermes botrooms list
hermes botrooms status
hermes botrooms doctor
hermes botrooms doctor --live
hermes botrooms remove <room-id>
hermes botrooms uninstall
```

`doctor --live` verifies each bot identity and channel read access without
sending a message. Send/thread permissions still require the real acceptance
check described below.

`uninstall` preserves profiles, normal conversations, credentials, and room
history. Use `--restart` to restart the affected gateways. Add `--purge-state`
only when the retained room database and cached attachments should also be
deleted.

State lives under:

```text
<hermes-root>/plugin-data/hermes-discord-botrooms/
```

Configuration lives under the plugin's namespaced entry in the installation
root `config.yaml`.

## Security boundaries

- Bot tokens stay in each profile's Hermes credential file and are resolved
  only when sending as that bot.
- Human ingress still uses the controller profile's existing Discord user,
  role, and channel authorization.
- Messages authored by bots never trigger a new room turn.
- Configured room channels are reserved across member gateways, preventing a
  normal profile session from also answering the same message.
- Discord mentions are suppressed on outbound room messages.
- Ambiguous network failures are not blindly reposted.

## Development

Run the plugin against a Hermes checkout:

```bash
python -m pytest
ruff check .
```

The live Discord acceptance checklist is in `AGENT_INSTALL.md`.
