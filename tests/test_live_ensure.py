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
