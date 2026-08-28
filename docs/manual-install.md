# Manual installation

Use this path when installing without a coding agent. Run every Python command
from the environment where `hermes` is installed.

## Requirements

- Python 3.11–3.13 on macOS or Linux
- an existing Hermes installation
- 2–6 existing local Hermes profiles
- Git access to this repository
- one configured Discord bot identity per participating profile
- `aiohttp>=3.9,<4` and `PyYAML>=6,<7` in the Hermes Python environment

See [Discord setup](discord-setup.md) before continuing if the bots, tokens,
intents, or channel permissions are not ready.

If Bot Rooms v0.2 is already installed, stop here and follow the
[v0.3.0 beta 1 upgrade checklist](releases/v0.3.0-beta.1.md) first.

## Preflight

```bash
git clone https://github.com/DanielOu1208/hermes-discord-botrooms.git &&
cd hermes-discord-botrooms &&
python -m hermes_discord_botrooms.compat --json &&
hermes plugins doctor . --ci
```

Stop if either check fails. Follow [Hermes compatibility](hermes-compatibility.md)
only when the installed runtime and source checkout satisfy every condition in
that document.

## Install the checked-out revision

Confirm that the printed SHA is the revision you intend to install:

```bash
if [ -n "$(git status --porcelain)" ]; then
  printf 'Stop: the cloned Bot Rooms repository has local changes.\n'
  false
else
  BOTROOMS_REF="$(git rev-parse HEAD)" &&
  printf 'Installing Bot Rooms commit: %s\n' "$BOTROOMS_REF" &&
  hermes plugins install https://github.com/DanielOu1208/hermes-discord-botrooms.git \
    --enable \
    --ref "$BOTROOMS_REF" &&
  hermes botrooms doctor --pre-install
fi
```

## Configure a room

The guided setup discovers local profiles and asks for a room ID, 2–6
profiles, one controller, a Discord server ID, and a channel ID:

```bash
hermes botrooms setup --restart
```

Restarting the selected gateways is already the default; `--restart` is shown
to make that release-critical step explicit.

For automation, replace every angle-bracket placeholder and preview first:

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

Review the redacted JSON. Before applying it, record the current `config.yaml`
path, SHA-256, and modification time; the dry run's `config_path` identifies the
file. Then rerun the same command without `--dry-run`. After the write, verify
that `config.yaml.botrooms-backup` has the recorded pre-setup checksum.
Setup restarts only the selected gateways by default. `--no-restart` is a
development-only escape hatch and is unsafe for an active room rollout.

Continue with [live verification](operations.md#live-acceptance-checklist).
