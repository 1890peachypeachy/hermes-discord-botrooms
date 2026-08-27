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
- An installed Hermes version accepted by the compatibility probe below
- 2–6 existing local Hermes profiles
- One Discord application and bot token per participating profile
- Every bot can view the room channel, read its history, and send in threads
- The controller can create public threads
- Message Content intent enabled for every bot

The plugin refuses incompatible Hermes builds. It does not monkey-patch core
files or silently weaken Discord authorization.

## Quick start

This repository is currently a private beta. Your machine must already have
Git access to the repository. Run these commands from the Python environment
where `hermes` is installed:

```bash
git clone git@github.com:DanielOu1208/hermes-discord-botrooms.git &&
cd hermes-discord-botrooms &&
python -m hermes_discord_botrooms.compat --json &&
hermes plugins doctor . --ci
```

Continue only if both commands succeed.

Then install the clean, checked-out revision at its full commit SHA. Confirm
that the printed SHA is the revision the user or repository owner approved:

```bash
if [ -n "$(git status --porcelain)" ]; then
  printf 'Stop: the cloned Bot Rooms repository has local changes.\n'
  false
else
  BOTROOMS_REF="$(git rev-parse HEAD)" &&
  printf 'Installing Bot Rooms commit: %s\n' "$BOTROOMS_REF" &&
  hermes plugins install git@github.com:DanielOu1208/hermes-discord-botrooms.git \
    --enable \
    --ref "$BOTROOMS_REF" &&
  hermes botrooms doctor --pre-install &&
  hermes botrooms setup --restart
fi
```

The setup wizard discovers profile names from the current Hermes installation.
It does not assume a default profile, particular agent names, a home-directory
layout, or a specific service manager.

If the profiles already exist, their Discord credentials are configured, and
the compatibility check passes, `hermes botrooms setup --restart` is the only
interactive setup command. The wizard asks for a room ID, 2–6 profiles, one
controller, a Discord server ID, and a channel ID.

For automation, replace every angle-bracket placeholder before running the
preview:

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

## Give this repository to an agent

Send the repository URL to your coding agent and say:

> Clone the repository and read its README from start to finish. Inspect my
> existing Hermes setup without changing it, run the compatibility checks,
> and tell me what is ready or missing. Ask me which profiles, controller,
> Discord server, channel, and room ID to use. Show me the redacted dry run
> before making changes. Install the exact selected commit only after I
> approve it, restart only the selected gateways, and complete the live
> verification checklist. Never display or ask me to paste a Discord token in
> chat.

An installing agent should follow this order:

1. Run `hermes --version`, `hermes profile list`, `hermes gateway status`, the
   compatibility probe, and `hermes plugins doctor . --ci` without changing
   anything.
2. Report profile names, whether Discord credentials exist, gateway ownership,
   and connected bot identities without reading credentials aloud.
3. Ask the user to select 2–6 profiles, one controller, a room ID, Discord
   server ID, and Discord channel ID. Show the exact commit and ask for approval
   to install it. Tokens are never conversation input.
4. After that approval, install the exact commit, run
   `hermes botrooms doctor --pre-install --json`, and show the setup `--dry-run`
   output.
5. Ask for approval of the displayed room changes and gateway restarts. Then
   rerun the same setup without `--dry-run` and perform the runtime and Discord
   acceptance checks below.

Creating Discord applications, enabling intents, inviting bots, and changing
channel permissions normally remain user-assisted steps in the user's signed-in
Discord account.

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
compatibility probe. Do not apply the patch to another Hermes revision.

### If the compatibility check fails

Stop rather than improvising changes to Hermes. The included patch may be used
only when all of these are true:

- the user explicitly approves changing their Hermes source checkout
- the checkout is clean and its `HEAD` is exactly
  `ef46ec03e11452eab74e261147668fb64a3d9fd3`
- the active `hermes` executable and gateway services are verified to use that
  checkout
- the patch check below succeeds

Test the patch in a temporary Git worktree first so a failure does not modify
the active Hermes checkout. From the Bot Rooms repository, run:

```bash
BOTROOMS_REPO="$(pwd)"
HERMES_CHECKOUT="/absolute/path/to/verified/hermes-checkout"
HERMES_BASE=ef46ec03e11452eab74e261147668fb64a3d9fd3
HERMES_TEST_PARENT="$(mktemp -d /tmp/hermes-botrooms-check.XXXXXX)"
HERMES_TEST_WORKTREE="${HERMES_TEST_PARENT:+$HERMES_TEST_PARENT/source}"

PATCH_TEST_OK=0
test -n "$HERMES_TEST_PARENT" &&
git -C "$HERMES_CHECKOUT" worktree add --detach \
  "$HERMES_TEST_WORKTREE" "$HERMES_BASE" &&
git -C "$HERMES_TEST_WORKTREE" apply --check \
  "$BOTROOMS_REPO/patches/hermes-machine-session-hooks.patch" &&
git -C "$HERMES_TEST_WORKTREE" apply \
  "$BOTROOMS_REPO/patches/hermes-machine-session-hooks.patch" &&
(
  cd "$HERMES_TEST_WORKTREE" &&
  python -m pytest -q tests/test_tui_gateway_server.py \
    -k "session_create or session_resume or tool_progress or reset_session_agent"
) &&
(
  cd "$BOTROOMS_REPO" &&
  PYTHONPATH="$HERMES_TEST_WORKTREE" \
    python -m hermes_discord_botrooms.compat --json
) &&
PATCH_TEST_OK=1

if [ "$PATCH_TEST_OK" -eq 1 ]; then
  git -C "$HERMES_CHECKOUT" worktree remove --force \
    "$HERMES_TEST_WORKTREE" &&
  rmdir "$HERMES_TEST_PARENT"
else
  printf 'Compatibility test failed; active Hermes checkout was not changed.\n'
  printf 'Temporary worktree for inspection: %s\n' "$HERMES_TEST_WORKTREE"
  false
fi
```

If any isolated check fails, stop and report the temporary worktree path; the
active checkout remains unchanged. If everything passes, the temporary
worktree is removed. Ask the user to approve activation. After approval, run
from the Bot Rooms repository, replacing both paths:

```bash
BOTROOMS_REPO="/absolute/path/to/hermes-discord-botrooms"
HERMES_CHECKOUT="/absolute/path/to/verified/hermes-checkout"
HERMES_BASE=ef46ec03e11452eab74e261147668fb64a3d9fd3
PATCH_FILE="$BOTROOMS_REPO/patches/hermes-machine-session-hooks.patch"
ACTIVATION_OK=0
ACTIVATION_STARTED=0

test -z "$(git -C "$HERMES_CHECKOUT" status --porcelain)" &&
test "$(git -C "$HERMES_CHECKOUT" rev-parse HEAD)" = "$HERMES_BASE" &&
git -C "$HERMES_CHECKOUT" apply --check "$PATCH_FILE" &&
git -C "$HERMES_CHECKOUT" switch -c botrooms-machine-session-hooks &&
ACTIVATION_STARTED=1 &&
git -C "$HERMES_CHECKOUT" apply "$PATCH_FILE" &&
(
  cd "$BOTROOMS_REPO" &&
  python -m hermes_discord_botrooms.compat --json
) &&
git -C "$HERMES_CHECKOUT" add \
  tui_gateway/methods_session.py tui_gateway/server.py &&
git -C "$HERMES_CHECKOUT" commit -m "Add versioned machine session hooks" &&
ACTIVATION_OK=1

if [ "$ACTIVATION_OK" -ne 1 ]; then
  if [ "$ACTIVATION_STARTED" -eq 1 ]; then
    git -C "$HERMES_CHECKOUT" restore --staged -- \
      tui_gateway/methods_session.py tui_gateway/server.py 2>/dev/null || true
    if git -C "$HERMES_CHECKOUT" apply --reverse --check "$PATCH_FILE"; then
      git -C "$HERMES_CHECKOUT" apply --reverse "$PATCH_FILE"
    fi
    if git -C "$HERMES_CHECKOUT" switch --detach "$HERMES_BASE"; then
      printf 'Activation failed; Hermes was returned to the pinned base commit.\n'
    else
      printf 'Activation failed and automatic rollback could not finish. Stop and inspect: %s\n' \
        "$HERMES_CHECKOUT"
    fi
  else
    printf 'Activation prerequisites failed; Hermes was not changed.\n'
  fi
  false
fi
```

Do not patch a different Hermes revision or a runtime whose source and process
ownership are unclear. Keep the new branch until Bot Rooms is uninstalled so
the original Hermes revision remains an explicit rollback point.

## Discord setup

For every participating profile:

1. Create a Discord application and bot in the Discord Developer Portal.
2. Enable Message Content intent. Enable Server Members intent when your
   Hermes authorization rules resolve usernames or roles.
3. Invite the bot with `bot` and `applications.commands` scopes.
4. Grant View Channel, Send Messages, Read Message History, Send Messages in
   Threads, and Attach Files. Grant Create Public Threads to the controller.
5. Store its token using the standard Discord setup flow provided by that
   installed Hermes version and select the intended profile. Do not put tokens
   in this repository or in the Bot Rooms configuration.

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

If the live acceptance check fails, release the channel while preserving room
state. Replace the example values, then restart every profile that participated
in that room:

```bash
ROOM_ID="my-room"
hermes botrooms remove "$ROOM_ID" --yes &&
hermes --profile "profile-a" gateway restart &&
hermes --profile "profile-b" gateway restart
```

Add one restart line for each additional room profile. If the plugin itself
should also be removed while retaining its state and the compatible Hermes
branch:

```bash
hermes botrooms uninstall --yes --restart
```

To roll back both the plugin and the Hermes compatibility branch, uninstall
without restarting first, switch the clean Hermes checkout back to its pinned
base, and then restart every affected profile:

```bash
HERMES_CHECKOUT="/absolute/path/to/verified/hermes-checkout"
HERMES_BASE=ef46ec03e11452eab74e261147668fb64a3d9fd3
git -C "$HERMES_CHECKOUT" rev-parse --git-dir >/dev/null &&
test -z "$(git -C "$HERMES_CHECKOUT" status --porcelain)" &&
hermes botrooms uninstall --yes &&
git -C "$HERMES_CHECKOUT" switch --detach "$HERMES_BASE" &&
hermes --profile "profile-a" gateway restart &&
hermes --profile "profile-b" gateway restart
```

Keep the named compatibility branch for inspection until the rollback has been
verified. Add one restart line for each additional affected profile.

Confirm that the affected gateways are healthy and normal profile chats work
again. If setup failed after writing configuration but before the room could be
removed normally, inspect `<hermes-root>/config.yaml.botrooms-backup`, verify
that it is the backup created by this setup, restore it to
`<hermes-root>/config.yaml`, and restart only the profiles named in the approved
dry run. A verified manual restore looks like this:

```bash
HERMES_ROOT="/absolute/path/to/verified/hermes-root"
cp -p "$HERMES_ROOT/config.yaml.botrooms-backup" \
  "$HERMES_ROOT/config.yaml" &&
hermes --profile "profile-a" gateway restart &&
hermes --profile "profile-b" gateway restart
```

Add one restart line for each additional affected profile.

Every Bot Rooms configuration write keeps the previous installation config at
`<hermes-root>/config.yaml.botrooms-backup`. Inspect that backup before using it;
do not blindly restore an old configuration.

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

## Live acceptance checklist

After `hermes botrooms doctor --live --json`, ask the user to send a harmless
message in the configured channel. A successful installation means:

- the controller creates exactly one Discord thread
- no ordinary profile gateway posts a duplicate response
- selected members run serially and may pass silently
- the currently working member shows its own typing indicator
- replies are posted by the bot identity that produced them
- `@member` limits or extends the responding set correctly
- `/room-status` reports the current run and `/stop` stops an active run
- normal chats outside the room still use each profile's ordinary messaging

Report automated checks, runtime checks, and this real Discord test separately.

## Development

Run the plugin against a Hermes checkout:

```bash
python -m pytest
ruff check .
```
