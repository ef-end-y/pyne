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

"""PYNE Model Context Protocol server (stdio).

Install the extra::

    pip install "hoox-pyne[mcp]"

Then run ``pyne-mcp`` or ``pyne mcp``, or print client JSON with ``pyne mcp config``.
"""

from __future__ import annotations

from pynescript.mcp.server import client_config
from pynescript.mcp.server import create_server
from pynescript.mcp.server import run_stdio


__all__ = ["client_config", "create_server", "run_stdio"]
