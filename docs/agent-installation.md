# Agent installation checklist

This is the detailed path for a coding agent installing Bot Rooms on someone
else's machine. It separates read-only discovery, plugin installation, room
activation, and real Discord acceptance.

## 1. Inspect without changing anything

From the cloned Bot Rooms repository, run:

```bash
command -v hermes
hermes --version
hermes profile list
hermes gateway status
python -m hermes_discord_botrooms.compat --json
hermes plugins doctor . --ci
```

For every candidate profile, also run the profile-scoped gateway status:

```bash
hermes --profile "profile-name" gateway status
```

Use the discovered profile names; do not guess them. Obtain a gateway PID from
Hermes status or its known managed-service entry, then inspect only that PID:

```bash
GATEWAY_PID="12345"
ps -p "$GATEWAY_PID" -o pid=,ppid=,user=,comm=
```

Use `launchctl` on macOS or the user-level `systemctl` service listing on Linux
when Hermes reports a managed service. Map each selected profile to:

- its actual Hermes home
- its gateway process or managed service
- the active `hermes` executable and Python environment
- the loaded plugin revision, when already installed
- the connected Discord bot identity, when the gateway is live

Do not infer ownership from display names, stale status JSON, or a directory
that merely looks conventional. If the runtime source must be matched to a
checkout, print the imported module paths from the same Python environment and
compare them with the verified checkout:

```bash
python -c 'import hermes_constants, tui_gateway.server; print(hermes_constants.__file__); print(tui_gateway.server.__file__)'
git -C "/absolute/path/to/hermes-checkout" rev-parse HEAD
git -C "/absolute/path/to/hermes-checkout" status --porcelain
```

If exact process arguments are still required, inspect only the already
identified gateway PID and redact credential-bearing arguments before placing
any result in the conversation. Never run or paste a broad full-command-line
process dump.

Report only whether Discord credentials are configured and which bot identity
is connected. Never print credential files, environment files, secret-scope
dictionaries, or token values. If a gateway is not live, report its connected
identity as unverified rather than guessing.

Stop if either preflight command fails. Use the
[guarded compatibility procedure](hermes-compatibility.md) only when every
condition there is proven and the user explicitly approves changing Hermes.

## 2. Collect the user's choices

Ask for:

- 2–6 existing profiles
- one selected profile as controller
- a short room ID
- Discord server ID
- Discord channel ID

Profile names and numeric Discord IDs are safe conversation input. Tokens are
not. Missing applications, intents, invites, permissions, or credentials route
to [Discord setup](discord-setup.md) and remain user-assisted unless the user
explicitly authorizes work in a signed-in browser.

## 3. First approval: install the plugin

Show:

- compatibility and plugin-doctor results
- the exact clean 40-character repository commit
- the profiles being considered
- any readiness gaps or unverified identities

Ask the user to approve that exact plugin revision. After approval, follow the
pinned installation section in [Manual installation](manual-install.md), then
run:

```bash
hermes botrooms doctor --pre-install --json
```

On an already compatible Hermes runtime, installing the plugin is the first
mutation and does not yet create the room. If Hermes needs the guarded patch,
that procedure has its own earlier approval before changing the source
checkout; return here only after the patched runtime passes verification.

## 4. Second approval: activate the room

Build the non-interactive setup command with the approved choices and run it
with `--dry-run --json`. The preview must not write configuration, install
profile copies, restart gateways, or contact Discord.

Show the redacted JSON and confirm it names only the selected profiles,
configuration path, exact plugin revision, and gateway restart commands. Ask
the user to approve those room changes and restarts.

After approval, rerun the same command without `--dry-run`. Do not manually
copy plugin directories between profiles; the setup command preserves source
and revision metadata while installing the selected profile copies.

## 5. Verify the rollout

Run:

```bash
hermes botrooms doctor --live --json
hermes botrooms list --json
hermes botrooms status --json
```

Recheck each affected gateway's active process, Hermes home, loaded plugin
revision, and connected Discord identity. Then complete the
[live acceptance checklist](operations.md#live-acceptance-checklist).

Report automated checks, runtime checks, and real Discord acceptance as three
separate evidence levels. A running process alone is not acceptance.

If rollout fails, follow [Operations and rollback](operations.md) rather than
improvising destructive Git or configuration commands.
