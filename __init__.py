"""Directory-plugin shim loaded by Hermes."""

try:
    from .hermes_discord_botrooms import register
except ImportError:  # direct checkout import (pytest/plugin doctor)
    from hermes_discord_botrooms import register

__all__ = ["register"]
