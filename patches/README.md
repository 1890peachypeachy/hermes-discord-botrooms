# Hermes compatibility patch

`hermes-machine-session-hooks.patch` is the beta compatibility patch
for Hermes Agent commit:

```text
ef46ec03e11452eab74e261147668fb64a3d9fd3
```

It adds version 1 of two generic JSON-RPC capabilities used by trusted machine
clients:

- a bounded, session-only system instruction append
- opt-in `tool_lifecycle` events independent of the human progress setting

The patch changes no Discord code and contains no profile-specific behavior.
CI applies it to that exact upstream commit and runs the compatibility probe,
gateway regression tests, plugin tests, and plugin validation.

Focused Hermes regression command:

```bash
python -m pytest -q tests/test_tui_gateway_server.py \
  -k "session_create or session_resume or tool_progress or reset_session_agent"
```

Do not apply it to a different revision. Rebasing this patch is maintainer
development work that must produce a newly reviewed, newly pinned patch and a
full test run; it is not a supported installation path. Prefer an upstream
Hermes release containing `MACHINE_SESSION_HOOKS_VERSION = 1` once one exists.
