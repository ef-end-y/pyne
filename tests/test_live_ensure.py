# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for autonomous history ensure + resume (F-13). Store is isolated per
test via ``PYNE_DATA_DIR``; bars are provided locally so no network is used.
"""

from __future__ import annotations

import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from backend.api.live_ensure import ensure_live_session
from backend.api.live_ensure import resume_live_session


KEY = "binance|spot|BTC/USDT|1m"


def _bars10():
    return [
        {"time": 1_700_000_000 + i * 60, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
        for i in range(10)
    ]


@pytest.fixture()
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNE_DATA_DIR", str(tmp_path / "data"))


def _provide(bars):
    from backend.datastore import series as series_store

    key = series_store.make_key("binance", "BTC/USDT", "1m", "spot")
    series_store.get_store().provide(key, bars, source="test")
    return key


def test_bad_key_rejected(_isolated):
    out = ensure_live_session("series", "junk", 50)
    assert out["outcome"] == "error"
    assert out["code"] == "BAD_KEY"


def test_script_without_script_rejected(_isolated):
    out = ensure_live_session("script", KEY, 50)
    assert out["outcome"] == "error"
    assert out["code"] == "NO_SCRIPT"


def test_series_kind_ready_without_session(_isolated):
    _provide(_bars10())
    out = ensure_live_session("series", KEY, 10)
    assert out["outcome"] == "ready"
    assert out["session_id"] is None
    assert out["count"] == 10


def test_script_kind_creates_bound_session(_isolated):
    _provide(_bars10())
    out = ensure_live_session("script", KEY, 10, script="indicator()", symbol="BTCUSDT")
    assert out["outcome"] == "ready"
    assert out["session_id"]
    resumed = resume_live_session(out["session_id"])
    assert resumed["outcome"] == "ready"
    assert resumed["count"] == 10


def test_identical_script_subs_share_one_session(_isolated):
    _provide(_bars10())
    a = ensure_live_session("script", KEY, 10, script="indicator()", symbol="BTCUSDT")
    b = ensure_live_session("script", KEY, 10, script="indicator()", symbol="BTCUSDT")
    assert a["outcome"] == b["outcome"] == "ready"
    assert a["session_id"] == b["session_id"]
    other = ensure_live_session("script", KEY, 10, script='indicator("x")', symbol="BTCUSDT")
    assert other["session_id"] != a["session_id"]


def test_resume_unknown_is_gone(_isolated):
    assert resume_live_session("nope") == {"outcome": "gone"}


def test_stale_pooled_session_recreates_over_current_store(_isolated):
    """A pool hit whose window the store moved past recreates (no zombie).

    Regression: after a repair / purge + refetch, resubscribing resumed the
    pre-repair pooled session by fingerprint, evaluating bars that no longer
    overlap the chart (stale overlays surviving reloads). A lag beyond the
    requested window must drop the entry and create fresh.
    """
    from backend.middleware.sessions import SESSIONS

    def bars10(start):
        return [
            {"time": start + i * 60, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
            for i in range(10)
        ]

    script = 'indicator("stale-recreate-probe")'
    _provide(bars10(1_700_000_000))
    a = ensure_live_session("script", KEY, 10, script=script, symbol="BTCUSDT")
    assert a["outcome"] == "ready"
    # The store window moves a full day past the pooled anchor.
    _provide(bars10(1_700_000_000 + 86_400))
    b = ensure_live_session("script", KEY, 10, script=script, symbol="BTCUSDT")
    assert b["outcome"] == "ready"
    assert b["session_id"] != a["session_id"]
    assert SESSIONS.get(a["session_id"]) is None
    assert b["newest"] == 1_700_000_000 + 86_400 + 9 * 60


def test_healthy_pooled_session_keeps_sharing(_isolated):
    """A pool hit lagging less than the window still shares (no churn)."""
    script = 'indicator("healthy-share-probe")'
    _provide(_bars10())
    a = ensure_live_session("script", KEY, 10, script=script, symbol="BTCUSDT")
    # One new bar lands (a normal tick): the anchor trails by a single bar.
    _provide(
        [
            {
                "time": 1_700_000_000 + 10 * 60,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
        ]
    )
    b = ensure_live_session("script", KEY, 10, script=script, symbol="BTCUSDT")
    assert b["outcome"] == "ready"
    assert b["session_id"] == a["session_id"]
