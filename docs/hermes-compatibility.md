# Hermes compatibility

Bot Rooms needs two versioned machine-client hooks: a session-only instruction
append and opt-in `tool_lifecycle` events. The plugin refuses unsupported
Hermes builds rather than changing them silently.

Check the active runtime from the Bot Rooms repository:

```bash
python -m hermes_discord_botrooms.compat --json
```

The plugin also recognizes the early in-core Bot Mode bridge. Official Hermes
`main` did not expose the generic versioned hook when v0.3.0 beta 1 was
prepared, so most users of this beta need the exact guarded patch below.

## Guarded compatibility patch

The included patch supports only Hermes commit:

```text
ef46ec03e11452eab74e261147668fb64a3d9fd3
```

Use it only when:

- the user explicitly approves changing the Hermes source checkout
- the checkout is clean and its `HEAD` is exactly that commit
- the active `hermes` executable and gateway services are verified to use that
  checkout
- the isolated patch check below succeeds

Do not patch another revision or a runtime whose source and process ownership
are unclear.

## Test without changing the active checkout

Run from the Bot Rooms repository, replacing the Hermes path:

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

If any check fails, stop and report the temporary worktree path. The active
checkout remains unchanged.

## Activate after approval

Only after the isolated test passes and the user approves activation, run from
the Bot Rooms repository and replace both paths:

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

Keep the named branch until Bot Rooms is uninstalled so the pinned base remains
an explicit rollback point. CI applies this same patch to the exact base and
runs the compatibility probe, gateway regressions, plugin tests, and plugin
validation. See also [the patch notes](../patches/README.md).
