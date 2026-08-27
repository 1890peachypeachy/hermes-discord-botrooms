# Operations and rollback

## Routine commands

```bash
hermes botrooms list
hermes botrooms status
hermes botrooms doctor
hermes botrooms doctor --live
hermes botrooms remove <room-id>
hermes botrooms uninstall
```

`doctor --live` verifies each bot identity and channel read access without
sending a message.

## Live acceptance checklist

Ask the user to post a harmless message in the configured channel. A successful
installation means:

- the controller creates exactly one Discord thread
- no ordinary profile gateway posts a duplicate response
- selected members run serially and may pass silently
- the currently working member shows its own typing indicator
- replies are posted by the bot identity that produced them
- `@member` limits or extends the responding set correctly
- `/room-status` reports the run and `/stop` stops an active run
- normal chats outside the room still use each profile's ordinary messaging

Report automated checks, runtime checks, and this real Discord test separately.

## Release one room

If acceptance fails, release the channel while preserving room state. Replace
the examples and restart every profile that participated in the room:

```bash
ROOM_ID="my-room"
hermes botrooms remove "$ROOM_ID" --yes &&
hermes --profile "profile-a" gateway restart &&
hermes --profile "profile-b" gateway restart
```

Add one restart line for each additional room profile.

## Uninstall the plugin

To remove Bot Rooms while retaining its state and a compatible Hermes branch:

```bash
hermes botrooms uninstall --yes --restart
```

Add `--purge-state` only when the user explicitly asks to delete the retained
room database and cached attachments.

## Roll back the compatibility branch

To remove both the plugin and the Hermes compatibility branch, check the
checkout before uninstalling, switch back to the pinned base, then restart
every affected profile:

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

Keep the named compatibility branch for inspection until rollback is verified.

## Restore a setup backup

Every Bot Rooms configuration write keeps the previous installation config at
`<hermes-root>/config.yaml.botrooms-backup`.

If setup fails after writing configuration but before normal removal works,
verify that the backup belongs to this setup, then restore it and restart only
the profiles named in the approved dry run:

```bash
HERMES_ROOT="/absolute/path/to/verified/hermes-root"
cp -p "$HERMES_ROOT/config.yaml.botrooms-backup" \
  "$HERMES_ROOT/config.yaml" &&
hermes --profile "profile-a" gateway restart &&
hermes --profile "profile-b" gateway restart
```

Never restore an unverified or stale backup.

## Data and security boundaries

State lives under:

```text
<hermes-root>/plugin-data/hermes-discord-botrooms/
```

Configuration lives in the plugin's namespaced entry in the installation root
`config.yaml`.

- Bot tokens remain in each profile's Hermes credential store.
- Human ingress keeps the controller's existing Discord authorization rules.
- Bot-authored messages never trigger another room turn.
- Configured room channels are reserved across member gateways.
- Outbound Discord mentions are suppressed.
- Ambiguous network failures are not blindly reposted.
