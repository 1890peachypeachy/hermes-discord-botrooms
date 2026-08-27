"""Hermes plugin entry point for Discord Bot Rooms."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    # Keep these imports lazy so the compatibility preflight can run without
    # importing Discord or initializing the plugin registration surface.
    from .adapter import register_platform
    from .cli import botrooms_command, register_cli
    from .compat import check_compatibility

    ctx.register_cli_command(
        name="botrooms",
        help="Configure and inspect multi-profile Discord Bot Rooms",
        setup_fn=register_cli,
        handler_fn=botrooms_command,
        description=(
            "Run bounded multi-profile Hermes rooms through existing Discord bot identities."
        ),
    )
    report = check_compatibility()
    if report.compatible:
        register_platform(ctx)
    else:
        logger.error(
            "Hermes Discord Bot Rooms is installed but inactive: %s. Run `hermes botrooms doctor`.",
            "; ".join(report.problems),
        )
