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

"""``PYNE_DEBUG`` logging toggle + ``GET /stats`` runtime counters.

Three layers under test:

- :mod:`pynescript.debug` — the env toggle and the ``PYNE_DEBUG_SCOPE`` filter.
- :mod:`pynescript.stats` — the counter / latency / gauge registry.
- :mod:`backend.logging_setup` + ``backend.app`` — level policy, request
  instrumentation and the public ``/stats`` payload.
"""

from __future__ import annotations

import logging

import pytest

from flask.testing import FlaskClient

from backend.app import app
from backend.logging_setup import configure_logging
from backend.logging_setup import debug_report
from pynescript import stats as stats_mod
from pynescript.debug import debug_enabled
from pynescript.debug import debug_scopes
from pynescript.debug import reset_debug_caches
from pynescript.debug import scope_enabled


_SCRIPT = '//@version=6\nindicator("stats")\nplot(ta.sma(close, 3))'


def _bars(n: int, base: float = 100.0, t0: int = 1_700_000_000) -> list[dict]:
    return [
        {
            "open": base + i,
            "high": base + i + 1,
            "low": base + i - 1,
            "close": base + i + 0.5,
            "volume": 10,
            "time": t0 + i * 3600,
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _clean_debug_env(monkeypatch):
    """Every test starts with the flag off and no scope filter."""
    monkeypatch.delenv("PYNE_DEBUG", raising=False)
    monkeypatch.delenv("PYNE_DEBUG_SCOPE", raising=False)
    reset_debug_caches()
    yield
    reset_debug_caches()


@pytest.fixture
def client(tmp_path, monkeypatch) -> FlaskClient:
    app.config["TESTING"] = True
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token-stats")
    monkeypatch.setenv("STORE_BACKEND", "json")
    monkeypatch.setenv("API_KEY_STORE", str(tmp_path / "api_keys.json"))
    with app.test_client() as client:
        yield client


class TestDebugToggle:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_truthy_values_enable(self, monkeypatch, raw: str):
        monkeypatch.setenv("PYNE_DEBUG", raw)
        reset_debug_caches()
        assert debug_enabled() is True

    def test_env_file_sets_the_debug_flag(self, tmp_path, monkeypatch):
        """``.env`` is honored by ``backend.app`` when python-dotenv is present."""
        pytest.importorskip("dotenv")
        import os

        from backend.app import _load_env_file

        monkeypatch.delenv("PYNE_DEBUG", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("PYNE_DEBUG=1\n")
        assert _load_env_file() is True
        assert os.environ.get("PYNE_DEBUG") == "1"
        reset_debug_caches()
        assert debug_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "banana"])
    def test_falsy_and_unknown_values_disable(self, monkeypatch, raw: str):
        monkeypatch.setenv("PYNE_DEBUG", raw)
        reset_debug_caches()
        assert debug_enabled() is False

    def test_unset_disables(self):
        assert debug_enabled() is False

    def test_picks_up_runtime_change_without_reset(self, monkeypatch):
        """The cache is keyed on the raw env string, so a live flip works."""
        assert debug_enabled() is False
        monkeypatch.setenv("PYNE_DEBUG", "1")
        assert debug_enabled() is True
        monkeypatch.setenv("PYNE_DEBUG", "0")
        assert debug_enabled() is False

    def test_scope_empty_means_no_filter(self, monkeypatch):
        monkeypatch.setenv("PYNE_DEBUG", "1")
        assert debug_scopes() == frozenset()
        assert scope_enabled("pynescript.runtime.host") is True

    def test_scope_filters_by_prefix(self, monkeypatch):
        monkeypatch.setenv("PYNE_DEBUG", "1")
        monkeypatch.setenv("PYNE_DEBUG_SCOPE", "pynescript.runtime, backend.app")
        assert debug_scopes() == frozenset({"pynescript.runtime", "backend.app"})
        assert scope_enabled("pynescript.runtime.host") is True
        assert scope_enabled("backend.app") is True
        assert scope_enabled("pynescript.compiler.engine") is False

    def test_scope_ignored_when_disabled(self, monkeypatch):
        monkeypatch.setenv("PYNE_DEBUG_SCOPE", "pynescript.runtime")
        assert scope_enabled("pynescript.runtime.host") is False


class TestLoggingPolicy:
    def test_info_level_by_default(self):
        policy = configure_logging(force=True)
        assert policy["enabled"] is False
        assert policy["level"] == "INFO"
        assert logging.getLogger("pynescript").level == logging.INFO

    def test_backend_app_logger_is_managed_by_the_debug_policy(self, monkeypatch):
        """``backend.app``'s module logger must sit inside the managed prefix.

        Under ``python -m backend.app`` the module name is ``__main__``, which
        ``configure_logging`` does not manage — the per-request access log was
        silently dropped. The module therefore uses an explicit logger name.
        """
        from backend import app as app_module

        monkeypatch.setenv("PYNE_DEBUG", "1")
        try:
            configure_logging(force=True)
            assert app_module.logger.name == "backend.app"
            assert app_module.logger.isEnabledFor(logging.DEBUG)
        finally:
            monkeypatch.delenv("PYNE_DEBUG", raising=False)
            configure_logging(force=True)

    def test_debug_level_when_enabled(self, monkeypatch):
        monkeypatch.setenv("PYNE_DEBUG", "1")
        try:
            policy = configure_logging(force=True)
            assert policy["enabled"] is True
            assert policy["level"] == "DEBUG"
            assert logging.getLogger("pynescript").level == logging.DEBUG
            assert logging.getLogger("backend").level == logging.DEBUG
            # Third-party noise stays down.
            assert logging.getLogger("urllib3").level == logging.WARNING
        finally:
            monkeypatch.delenv("PYNE_DEBUG", raising=False)
            configure_logging(force=True)

    def test_debug_report_reflects_env(self, monkeypatch):
        monkeypatch.setenv("PYNE_DEBUG", "1")
        monkeypatch.setenv("PYNE_DEBUG_SCOPE", "pynescript.runtime")
        report = debug_report()
        assert report["env"] == "PYNE_DEBUG"
        assert report["enabled"] is True
        assert report["scopes"] == ["pynescript.runtime"]


class TestStatsRegistry:
    def test_count_with_and_without_dimension(self):
        stats_mod.reset()
        stats_mod.count("t.c")
        stats_mod.count("t.c")
        stats_mod.count("t.c", 5, dim="x")
        snap = stats_mod.snapshot()
        assert snap["counters"]["t.c"]["total"] == 7
        assert snap["counters"]["t.c"]["by"] == {"x": 5}

    def test_latency_summary_shape(self):
        stats_mod.reset()
        for ms in (10.0, 20.0, 30.0):
            stats_mod.latency("t.l", ms / 1000.0)
        summary = stats_mod.snapshot()["latencies"]["t.l"]
        assert summary["count"] == 3
        assert summary["total_ms"] == pytest.approx(60.0)
        assert summary["avg_ms"] == pytest.approx(20.0)
        assert summary["min_ms"] == pytest.approx(10.0)
        assert summary["max_ms"] == pytest.approx(30.0)
        assert summary["p50_ms"] == pytest.approx(20.0)
        assert summary["p95_ms"] == pytest.approx(30.0)

    def test_gauges_set_inc_dec_and_max(self):
        stats_mod.reset()
        stats_mod.set_gauge("t.g", 5)
        assert stats_mod.snapshot()["gauges"]["t.g"] == 5
        stats_mod.inc_gauge("t.g", 3)
        assert stats_mod.snapshot()["gauges"]["t.g"] == 8
        stats_mod.dec_gauge("t.g", 20)
        assert stats_mod.snapshot()["gauges"]["t.g"] == 0
        stats_mod.max_gauge("t.g", 7)
        stats_mod.max_gauge("t.g", 2)
        assert stats_mod.snapshot()["gauges"]["t.g"] == 7

    def test_timer_records_latency(self):
        stats_mod.reset()
        with stats_mod.timer("t.timer"):
            pass
        assert stats_mod.snapshot()["latencies"]["t.timer"]["count"] == 1

    def test_snapshot_is_json_safe_and_stable(self):
        stats_mod.reset()
        stats_mod.count("t.j", 3)
        snap = stats_mod.snapshot()
        assert isinstance(snap["uptime_seconds"], float)
        assert snap["counters"]["t.j"]["total"] == 3  # integral floats collapse to int


class TestStatsEndpoint:
    def test_stats_shape(self, client: FlaskClient):
        resp = client.get("/stats")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        for section in ("debug", "counters", "latencies", "gauges", "compile_cache", "sessions"):
            assert section in body, section
        assert body["debug"]["env"] == "PYNE_DEBUG"
        assert "sessions" in body["sessions"] or body["sessions"].get("error") == "unavailable"

    def test_stats_is_public(self, client: FlaskClient):
        """No API key required — ops polls it like /health."""
        resp = client.get("/stats")
        assert resp.status_code == 200

    def test_stats_counts_http_requests(self, client: FlaskClient):
        client.get("/stats")
        body = client.get("/stats").get_json()
        endpoints = body["counters"]["http.by_endpoint"]["by"]
        assert endpoints.get("GET /stats") >= 1
        assert body["latencies"]["http.request"]["count"] >= 1

    def test_stats_records_engine_run(self, client: FlaskClient):
        resp = client.post("/run", json={"script": _SCRIPT, "data": _bars(12), "mode": "interpret"})
        assert resp.status_code == 200, resp.get_json()
        body = client.get("/stats").get_json()
        assert body["counters"]["run.total"]["total"] >= 1
        assert body["counters"]["run.bars"]["total"] >= 12
        assert body["counters"]["run.by_mode"]["by"].get("interpret", 0) >= 1
        assert body["counters"]["run.mode_resolved"]["by"].get("interpret", 0) >= 1
        assert body["latencies"]["run.total"]["count"] >= 1

    def test_stats_records_delta_and_sessions(self, client: FlaskClient):
        created = client.post("/session", json={"script": _SCRIPT, "data": _bars(10)})
        assert created.status_code == 200, created.get_json()
        sid = created.get_json()["session_id"]

        appended = client.post(f"/session/{sid}/append", json={"bars": _bars(3, base=200.0, t0=1_700_003_600)})
        assert appended.status_code == 200, appended.get_json()

        last = _bars(1, base=300.0, t0=1_700_003_600 + 2 * 3600)[0]
        assert client.post(f"/session/{sid}/tick", json={"bar": last}).status_code == 200

        body = client.get("/stats").get_json()
        assert body["counters"]["delta.append_calls"]["total"] >= 1
        assert body["counters"]["delta.bars_appended"]["total"] >= 3
        assert body["counters"]["delta.update_last_bar_calls"]["total"] >= 1
        assert body["counters"]["session.created"]["total"] >= 1
        assert body["gauges"]["sessions.active"] >= 1
        assert body["counters"]["http.by_endpoint"]["by"].get("POST /session") == 1

    def test_stats_counts_client_errors(self, client: FlaskClient):
        client.post("/run", json={"script": _SCRIPT, "data": []})
        body = client.get("/stats").get_json()
        assert body["counters"]["http.by_status_class"]["by"].get("4xx", 0) >= 1
        assert body["counters"]["http.client_errors"]["total"] >= 1

    def test_stats_is_advertised_on_health(self, client: FlaskClient):
        body = client.get("/health").get_json()
        assert "GET /stats" in body["endpoints"]


class TestEngineDebugLogging:
    def test_engine_emits_debug_lines_when_enabled(self, client: FlaskClient, monkeypatch, caplog):
        monkeypatch.setenv("PYNE_DEBUG", "1")
        configure_logging(force=True)
        with caplog.at_level(logging.DEBUG, logger="pynescript.runtime.host"):
            client.post("/run", json={"script": _SCRIPT, "data": _bars(8), "mode": "interpret"})
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "bar-loop start" in text
        assert "bar-loop done" in text
        assert "run finished" in text

    def test_engine_is_quiet_when_disabled(self, client: FlaskClient, caplog):
        with caplog.at_level(logging.DEBUG, logger="pynescript.runtime.host"):
            client.post("/run", json={"script": _SCRIPT, "data": _bars(8), "mode": "interpret"})
        assert caplog.records == []

    def test_delta_path_logs_when_enabled(self, client: FlaskClient, monkeypatch, caplog):
        monkeypatch.setenv("PYNE_DEBUG", "1")
        configure_logging(force=True)
        created = client.post("/session", json={"script": _SCRIPT, "data": _bars(10)})
        sid = created.get_json()["session_id"]
        with caplog.at_level(logging.DEBUG, logger="pynescript.runtime.host"):
            client.post(f"/session/{sid}/append", json={"bars": _bars(2, base=200.0, t0=1_700_003_600)})
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "append_bars" in text
        assert "delta=2" in text
