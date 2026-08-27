from __future__ import annotations

import tui_gateway.server as server


def test_versioned_machine_session_hook_is_available():
    assert server.MACHINE_SESSION_HOOKS_VERSION == 1


def test_tool_lifecycle_subscription_is_validated_and_effective():
    subscriptions = server._normalize_event_subscriptions(["tool_lifecycle"])
    assert subscriptions == frozenset({"tool_lifecycle"})

    server._sessions["botrooms-contract"] = {
        "event_subscriptions": subscriptions,
        "tool_progress_mode": "off",
    }
    try:
        assert server._tool_progress_enabled("botrooms-contract") is True
    finally:
        server._sessions.pop("botrooms-contract", None)
