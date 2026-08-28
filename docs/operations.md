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

In Discord, `/room-status` in the parent channel summarizes active threads.
Inside a Bot Room thread, it reports that thread. `/stop`, `/room-answer`, and
`/room-approval` must be used inside the intended thread so a command can never
affect a sibling room by accident.

## Live acceptance checklist

Ask the user to post a harmless message in the configured channel. A successful
installation means:

- the controller creates exactly one Discord thread
- a second top-level message creates a second room with fresh agent context
- activity in either thread does not cancel, hold, or answer prompts in the other
- replies inside one thread continue only that thread's context
- no ordinary profile gateway posts a duplicate response
- selected members run serially and may pass silently
- the currently working member shows its own typing indicator
- replies are posted by the bot identity that produced them
- `@member` limits or extends the responding set correctly
- `/room-status` reports the run and `/stop` stops an active run
- `/stop`, `/room-answer`, and `/room-approval` refuse to guess a target when
  used in the parent channel
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
`<hermes-root>/config.yaml.botrooms-backup`. It represents only the most recent
Bot Rooms config write.

If setup fails after writing configuration but before normal removal works,
compare the backup's SHA-256 and modification time with the pre-setup values
recorded alongside the approved dry run. If those values were not recorded or
do not match, stop instead of guessing. Preserve the failed config at a new,
unused path before restoring the verified backup, then restart only the
profiles named in the approved dry run:

```bash
HERMES_ROOT="/absolute/path/to/verified/hermes-root"
FAILED_CONFIG="/absolute/path/to/new-unused/config.yaml.failed-botrooms"
test -f "$HERMES_ROOT/config.yaml" &&
test -f "$HERMES_ROOT/config.yaml.botrooms-backup" &&
test ! -e "$FAILED_CONFIG" &&
cp -p "$HERMES_ROOT/config.yaml" "$FAILED_CONFIG" &&
cp -p "$HERMES_ROOT/config.yaml.botrooms-backup" \
  "$HERMES_ROOT/config.yaml" &&
hermes --profile "profile-a" gateway restart &&
hermes --profile "profile-b" gateway restart
```

Never restore an unverified or stale backup, and preserve `FAILED_CONFIG` until
recovery is verified.

## Restore a pre-upgrade state backup

Use this only to return a failed v0.3 upgrade to the exact v0.2 plugin revision
and state snapshot recorded before the upgrade. Configuration rollback and
state rollback are separate; the setup backup above restores configuration,
while this procedure restores the room database and attachment cache.

1. Verify the old 40-character plugin commit, backup source, Hermes root, and
   complete participating-profile list. Do not guess any path or revision.
2. Stop every participating gateway with `hermes --profile <profile> gateway stop`.
3. Reinstall the recorded old commit for every participating profile, replacing
   every placeholder with a verified value:

   ```bash
   hermes --profile <profile> plugins install <verified-git-source> \
     --enable \
     --ref <old-40-character-commit> \
     --force
   ```

   Repeat the command for every profile before starting any gateway.
4. Move the failed v0.3 state directory to a new, unused recovery path; do not
   delete or overwrite it. Copy the verified pre-upgrade
   `hermes-discord-botrooms` directory back under
   `<hermes-root>/plugin-data/`.
5. Restart every participating gateway and verify its loaded plugin revision,
   Discord identity, `hermes botrooms doctor --live`, and the live acceptance
   checklist.

Never restore the state directory while any participating gateway is running,
and never combine a v0.2 state snapshot with mixed plugin revisions. Preserve
the failed v0.3 directory until rollback has been verified.

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
