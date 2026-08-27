# Installation playbook for agents

This playbook is designed for an agent that receives only this repository URL
and access to the user's machine. It is safe to follow on macOS and Linux.

## 1. Inspect without changing anything

Clone the private repository using the user's existing Git credentials. Then
run:

```bash
hermes --version
hermes profile list
hermes gateway status
python -m hermes_discord_botrooms.compat --json
hermes plugins doctor . --ci
```

If the compatibility probe fails, stop. Do not improvise changes to Hermes.
This private beta includes one guarded source-checkout option for upstream
commit `ef46ec03e11452eab74e261147668fb64a3d9fd3` only. Offer it only when all
of these are true:

- the user explicitly approves changing their Hermes source checkout
- the checkout is clean and its `HEAD` exactly matches that commit
- the active Hermes executable and gateway services are verified to use that
  checkout
- `git apply --check patches/hermes-machine-session-hooks.patch` succeeds

Create a named branch, apply the patch, run the focused gateway tests stated in
`patches/README.md`, and rerun the compatibility probe. If any condition or
test fails, restore no files destructively; report the blocker and stop.

Resolve the actual Hermes root through Hermes itself or `HERMES_HOME`. A named
profile normally lives below `<root>/profiles/<name>`, but verify rather than
constructing paths from that convention.

For every discovered profile, report only:

- profile name and friendly name
- whether Discord credentials are configured
- connected Discord bot identity, when safely available
- gateway service status and owning Hermes home

Do not display environment files or secret-scope dictionaries.

## 2. Ask for the choices that cannot be discovered

Ask the user to choose:

- 2–6 profiles
- one selected profile as controller
- Discord server ID
- Discord channel ID
- a short room ID

Profile names and Discord numeric IDs are safe to share. Tokens are not.

If a profile lacks Discord credentials, stop and ask the user to run the
ordinary Hermes setup flow locally for that profile. Do not accept the token
through the conversation.

The user may also need to create applications, enable Message Content intent,
invite bots, or change channel permissions in the Discord Developer Portal.
Those authenticated steps are user-assisted unless the user explicitly
authorizes browser automation in their already signed-in browser.

## 3. Preview

Before installation, report the compatibility result and the exact full commit
SHA from the checked-out repository. Stop if either preflight command fails.
After the user approves the selected revision, install that revision once in
the current Hermes profile:

```bash
hermes plugins install git@github.com:DanielOu1208/hermes-discord-botrooms.git \
  --enable \
  --ref <40-character-sha>
hermes botrooms doctor --pre-install --json
```

Then preview the room and profile fan-out:

```bash
hermes botrooms setup \
  --room-id <room-id> \
  --profiles <profile-a>,<profile-b> \
  --controller <profile-a> \
  --guild-id <guild-id> \
  --channel-id <channel-id> \
  --plugin-source git@github.com:DanielOu1208/hermes-discord-botrooms.git \
  --plugin-ref <40-character-sha> \
  --non-interactive \
  --yes \
  --restart \
  --dry-run \
  --json
```

Show the user the redacted JSON. Confirm that it names only the selected
profiles and gateways. The dry run must not modify config, install profile
copies, restart services, or contact Discord.

## 4. Install

After approval, rerun the same command without `--dry-run`. The setup command
installs the same pinned plugin revision in each selected profile, writes the
installation-level room registry atomically, and restarts only the affected
gateways.

Do not manually copy plugin directories between profiles. The Hermes plugin
installer maintains source and revision metadata needed for updates.

## 5. Verify

Run:

```bash
hermes botrooms doctor --live --json
hermes botrooms list --json
hermes botrooms status --json
```

Then verify each affected gateway's active process, Hermes home, loaded plugin
revision, and connected Discord identity. Do not infer ownership from display
names or stale status files.

Ask the user to post a harmless message in the configured channel. Acceptance
requires all of the following:

- the controller creates exactly one Discord thread
- no ordinary profile gateway posts a duplicate response
- selected members run serially and may pass silently
- the currently working member shows its own typing indicator
- replies are posted by the member that produced them
- `@member` limits or extends the responding set correctly
- `/room-status` reports the run
- `/stop` stops an active run
- a normal chat outside the room still uses the profile's ordinary messaging

Report unit checks, runtime checks, and the real Discord acceptance separately.

## 6. Roll back

To release one room channel without deleting state:

```bash
hermes botrooms remove <room-id> --yes
```

To remove the plugin from configured profiles while retaining room history:

```bash
hermes botrooms uninstall --yes --restart
```

Use `--purge-state` only when the user explicitly asks to delete the retained
room database and cached attachments.

Every Bot Rooms config write preserves the previous installation config at
`<hermes-root>/config.yaml.botrooms-backup`. If setup fails after the write,
inspect that backup, restore it to `<hermes-root>/config.yaml`, and restart
only the profiles named in the approved dry run. Do not restore an unverified
or stale backup blindly.
