# Copyright (C) 2024-2026 jango_blockchained
#
# This file is part of pynescript.
#
# pynescript is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pynescript is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pynescript.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Logging configuration for the Pro API.

The single switch is :data:`~pynescript.debug.DEBUG_ENV` (``PYNE_DEBUG``):

============================  ==================================================
``PYNE_DEBUG`` unset / ``0``  INFO on ``pynescript.*`` + ``backend.*``,
                              no request logging, no handler installed when the
                              host already provides one (gunicorn).
``PYNE_DEBUG=1``              DEBUG everywhere, handler attached when missing,
                              per-request access log, engine debug lines from
                              the runtime / compiler / datafeed modules.
``PYNE_DEBUG_SCOPE``          Optional comma-separated logger allowlist, e.g.
                              ``pynescript.runtime,pynescript.compiler`` —
                              only consulted while ``PYNE_DEBUG`` is on.
============================  ==================================================

Development only. It is verbose by design (per-bar-loop, per-compile,
per-upstream-fetch) and should stay off in production.

Idempotent: safe to call from module import and again from ``__main__``.
"""

from __future__ import annotations

import logging
import sys

from typing import Any

from pynescript.debug import SCOPE_ENV
from pynescript.debug import debug_enabled
from pynescript.debug import debug_scopes


__all__ = [
    "configure_logging",
    "debug_report",
    "install_debug_handler",
]


#: Loggers we manage. Everything else (werkzeug, gunicorn, urllib3, ccxt)
#: keeps its own level unless ``PYNE_DEBUG`` is on.
MANAGED_PREFIXES: tuple[str, ...] = ("pynescript", "backend")

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

_STATE: dict[str, Any] = {"configured": False}


def _formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)


def install_debug_handler() -> logging.Handler:
    """Attach a stderr handler to the root logger when one is missing.

    Gunicorn / Flask may already provide handlers; we never add a second
    formatter on top of an existing one.
    """
    root = logging.getLogger()
    if any(getattr(h, "_pyne_debug_handler", False) for h in root.handlers):
        return root.handlers[0]
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_formatter())
    handler._pyne_debug_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    return handler


def configure_logging(*, force: bool = False) -> dict[str, Any]:
    """Apply the ``PYNE_DEBUG`` logging policy. Returns what was applied.

    Args:
        force: Re-apply even if already configured (used by tests).

    Returns:
        dict with ``enabled``, ``level``, ``scopes``, ``handler_installed``,
        ``managed``.
    """
    enabled = debug_enabled()
    scopes = debug_scopes()
    level = logging.DEBUG if enabled else logging.INFO

    if _STATE["configured"] and not force:
        return {
            "enabled": enabled,
            "level": logging.getLevelName(level),
            "scopes": sorted(scopes),
            "handler_installed": False,
            "managed": list(MANAGED_PREFIXES),
        }

    handler_installed = False
    if enabled:
        # No handler at all (bare ``python -m backend.app``): install one so the
        # debug lines are not silently dropped by the "last resort" handler.
        if not logging.getLogger().handlers:
            install_debug_handler()
            handler_installed = True
        # Keep third-party chatter down unless the user asked for a scope.
        for noisy in ("urllib3", "numba", "matplotlib", "asyncio", "werkzeug"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    for prefix in MANAGED_PREFIXES:
        logging.getLogger(prefix).setLevel(level)

    _STATE["configured"] = True
    logging.getLogger(__name__).debug(
        "logging configured: PYNE_DEBUG=%s level=%s scope=%s",
        enabled,
        logging.getLevelName(level),
        ",".join(sorted(scopes)) or "<all>",
    )
    return {
        "enabled": enabled,
        "level": logging.getLevelName(level),
        "scopes": sorted(scopes),
        "handler_installed": handler_installed,
        "managed": list(MANAGED_PREFIXES),
    }


def debug_report() -> dict[str, Any]:
    """Describe the current debug configuration (surfaced on ``GET /stats``)."""
    enabled = debug_enabled()
    return {
        "env": "PYNE_DEBUG",
        "enabled": enabled,
        "scope_env": SCOPE_ENV,
        "scopes": sorted(debug_scopes()),
        "level": logging.getLevelName(logging.getLogger("pynescript").level),
        "notes": "development only; verbose per-bar-loop/compile/fetch logging",
    }
