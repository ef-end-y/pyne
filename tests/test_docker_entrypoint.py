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

"""Guards for the gunicorn worker class used by the Pro API image.

flask-sock performs the HTTP→WS upgrade itself by hijacking the worker
socket (``gunicorn.socket``). The gevent-websocket worker
(``geventwebsocket.gunicorn.workers.GeventWebSocketWorker``) *also* upgrades
the socket, so both write a handshake: the client sees two ``101`` responses,
the frame stream desyncs, and every WS frame is dropped (close 1002
"control frame is fragmented") — while REST keeps working. That regression
shipped once (0.6.11) because the image default flipped to the gevent worker.

These checks are cheap and textual: they keep the default on ``gthread``
(``sync``/``gthread`` serve WS through flask-sock) everywhere the image can
pick a worker class.
"""

from __future__ import annotations

import re

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENTRYPOINT = _REPO_ROOT / "docker" / "entrypoint-api.sh"
_COMPOSE_PROD = _REPO_ROOT / "docker-compose.prod.yml"
_COMPOSE_BASE = _REPO_ROOT / "docker-compose.yml"
_SYSTEMD_UNIT = _REPO_ROOT / "deploy" / "pynescript-api.service"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

_GEVENT_WORKER = "geventwebsocket.gunicorn.workers.GeventWebSocketWorker"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestEntrypointWorkerClass:
    def test_default_worker_class_is_gthread(self):
        entrypoint = _text(_ENTRYPOINT)
        match = re.search(r'WORKER_CLASS="\$\{GUNICORN_WORKER_CLASS:-([^}]+)\}"', entrypoint)
        assert match, "entrypoint must define WORKER_CLASS with a default"
        assert match.group(1) == "gthread", (
            "the API image must default to the gthread worker — flask-sock owns "
            "the WS upgrade and the gevent-websocket worker corrupts the frames"
        )

    def test_gevent_worker_is_not_the_default(self):
        assert _GEVENT_WORKER not in _text(_ENTRYPOINT).split("exec gunicorn")[0].replace(
            "Do NOT select geventwebsocket.gunicorn.workers.GeventWebSocketWorker", ""
        )

    def test_entrypoint_documents_the_gevent_worker_hazard(self):
        entrypoint = _text(_ENTRYPOINT)
        assert "GeventWebSocketWorker" in entrypoint
        assert "hijack" in entrypoint  # explains who owns the upgrade


class TestComposeWorkerClass:
    def test_prod_compose_pins_gthread(self):
        compose = _text(_COMPOSE_PROD)
        assert re.search(r"GUNICORN_WORKER_CLASS:\s*gthread", compose), (
            "the prod overlay must pin the worker class (never interpolate it from the host .env)"
        )

    def test_systemd_unit_uses_gthread(self):
        unit = _text(_SYSTEMD_UNIT)
        assert "--worker-class gthread" in unit or "-k gthread" in unit

    def test_env_example_documents_gthread(self):
        assert re.search(r"^GUNICORN_WORKER_CLASS=gthread$", _text(_ENV_EXAMPLE), re.M)


@pytest.mark.parametrize("path", [_COMPOSE_BASE, _COMPOSE_PROD, _ENTRYPOINT, _SYSTEMD_UNIT])
def test_no_file_selects_the_gevent_worker_by_default(path: Path):
    """No deploy path may *select* the gevent-websocket worker by default."""
    # Comments may mention the hazard; only assignments count.
    text = "\n".join(line for line in _text(path).splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"GUNICORN_WORKER_CLASS[=:]\s*geventwebsocket", text)
