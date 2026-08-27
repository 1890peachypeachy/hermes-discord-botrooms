"""Hermes compatibility probes used before the Discord adapter is replaced."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CompatibilityReport:
    compatible: bool
    mode: str
    hermes_version: str
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        return "unknown"


def _generic_session_hooks_available() -> bool:
    """Detect the public session hooks consumed by the standalone plugin."""

    try:
        from tui_gateway import server
    except Exception:
        return False
    return int(getattr(server, "MACHINE_SESSION_HOOKS_VERSION", 0) or 0) == 1


def _legacy_bot_room_hooks_available() -> bool:
    """Accept the already-tested in-core Bot Mode compatibility bridge."""

    try:
        import inspect

        from tools.bot_mode_probe import is_bot_room_source
        from tui_gateway import server

        source = inspect.getsource(server._tool_progress_enabled)
    except Exception:
        return False
    return callable(is_bot_room_source) and "is_bot_room_source" in source


def check_compatibility() -> CompatibilityReport:
    problems: list[str] = []
    try:
        from plugins.platforms.discord import adapter as base

        required = (
            "DiscordAdapter",
            "_apply_yaml_config",
            "_build_adapter",
            "_is_connected",
            "_standalone_send",
            "check_discord_requirements",
            "discord_deps_present",
            "interactive_setup",
        )
        missing = [name for name in required if not hasattr(base, name)]
        if missing:
            problems.append("Hermes Discord adapter is missing: " + ", ".join(missing))
        adapter_methods = (
            "connect",
            "disconnect",
            "_cache_discord_document",
            "_cache_discord_image",
            "_check_slash_authorization",
            "_derive_auto_thread_name",
            "_discord_channel_keys",
            "_discord_max_attachment_bytes",
            "_dispatch_discord_message",
            "_dispatch_recovered_message",
            "_get_allowed_channels",
            "_get_ignored_channels",
            "_get_parent_channel_id",
            "_is_allowed_user",
            "_is_discord_voice_message_attachment",
            "_register_slash_commands",
            "_run_simple_slash",
            "_warn_if_fail_closed_default",
        )
        missing_methods = [
            name for name in adapter_methods if not hasattr(base.DiscordAdapter, name)
        ]
        if missing_methods:
            problems.append("Hermes DiscordAdapter is missing: " + ", ".join(missing_methods))
    except Exception as exc:
        problems.append(f"Hermes Discord adapter could not be imported: {exc}")

    if _generic_session_hooks_available():
        mode = "generic-session-hooks"
    elif _legacy_bot_room_hooks_available():
        mode = "legacy-bot-room-hooks"
    else:
        mode = "unsupported"
        problems.append(
            "Hermes lacks persistent internal-session instructions and "
            "machine-facing tool lifecycle events"
        )

    return CompatibilityReport(
        compatible=not problems,
        mode=mode,
        hermes_version=_version(),
        problems=tuple(problems),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether this Hermes build can run Discord Bot Rooms"
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    report = check_compatibility()
    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.compatible:
        print(f"Compatible Hermes {report.hermes_version} ({report.mode}).")
    else:
        print(f"Incompatible Hermes {report.hermes_version}.")
        for problem in report.problems:
            print(f"  ERROR: {problem}")
    return 0 if report.compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())
