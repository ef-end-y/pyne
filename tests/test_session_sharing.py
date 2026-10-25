# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for refcount-pooled shared sessions (F-29). Identical tabs share
one session instead of each paying a full-window create.
"""

from __future__ import annotations

import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from backend.middleware.sessions import SessionStore


def _fp(**kw):
    args = {"script": "indicator()", "symbol": "BTCUSDT", **kw}
    return SessionStore.shared_fingerprint(**args)


def test_fingerprint_stable_and_content_keyed():
    assert _fp() == _fp()
    assert _fp(script="other") != _fp()
    assert _fp(inputs={"a": 1}) != _fp(inputs={"a": 2})


def test_acquire_shares_identical_sessions():
    store = SessionStore()
    created = []
    sid1, is_new1 = store.acquire_shared(_fp(), lambda: created.append(object()) or created[-1])
    sid2, is_new2 = store.acquire_shared(_fp(), lambda: pytest.fail("must reuse, not create"))
    assert is_new1 is True
    assert is_new2 is False
    assert sid1 == sid2
    assert len(created) == 1


def test_release_drops_mapping_at_zero_but_keeps_session():
    store = SessionStore()
    sid, _ = store.acquire_shared(_fp(), object)
    store.acquire_shared(_fp(), object)
    assert store.release_shared(sid) is True
    # One ref left: still pooled.
    sid2, is_new = store.acquire_shared(_fp(), lambda: pytest.fail("still pooled"))
    assert sid2 == sid and is_new is False
    assert store.release_shared(sid) is True
    assert store.release_shared(sid) is True
    # Zero refs: next acquire creates fresh.
    sid3, is_new3 = store.acquire_shared(_fp(), object)
    assert is_new3 is True
    assert sid3 != sid
    # The old session itself still idles out by TTL (not dropped early).
    assert store.get(sid) is not None


def test_release_unknown_is_false():
    assert SessionStore().release_shared("ghost") is False


def test_capacity_enforced_on_miss():
    store = SessionStore(max_sessions=1)
    store.acquire_shared(_fp(script="a"), object)
    with pytest.raises(RuntimeError):
        store.acquire_shared(_fp(script="b"), object)


def test_drop_clears_pooling():
    store = SessionStore()
    sid, _ = store.acquire_shared(_fp(), object)
    assert store.drop(sid) is True
    sid2, is_new = store.acquire_shared(_fp(), object)
    assert is_new is True
    assert sid2 != sid


def test_stats_reports_shared():
    store = SessionStore()
    assert store.stats()["shared"] == 0
    store.acquire_shared(_fp(), object)
    assert store.stats()["shared"] == 1


def test_drop_by_key_removes_only_bound_series():
    from types import SimpleNamespace

    from backend.datastore import live as data_live
    from backend.datastore.series import make_key

    store = SessionStore()
    bar = {"time": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    ka = make_key("binance", "BTC/USDT", "1m", "spot")
    kb = make_key("binance", "ETH/USDT", "1m", "spot")
    sa, sb, sc = SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    data_live.bind_session(sa, ka, bar)
    data_live.bind_session(sb, kb, bar)
    ida = store.put(sa)
    idb = store.put(sb)
    idc = store.put(sc)
    assert store.drop_by_key(ka.text) == 1
    assert store.get(ida) is None
    assert store.get(idb) is not None
    assert store.get(idc) is not None
    assert store.drop_by_key("") == 0
    assert store.drop_by_key("nope|nope|NOPE|1m") == 0


def test_drop_by_key_drops_journaled_unloaded_session(tmp_path):
    """A journaled session not in this worker's memory is still purged.

    Restart-shaped: bind + journal, evict memory, drop_by_key, then get/resume
    must not rebuild the thrown-away window.
    """
    from types import SimpleNamespace

    from backend.datastore import live as data_live
    from backend.datastore.series import make_key

    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    bar = {"time": 1_700_000_000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    key = make_key("binance", "BTC/USDT", "1m", "spot")
    session = SimpleNamespace(_ohlcv_data=[bar], _n_bars=1)
    data_live.bind_session(session, key, bar)
    sid = store.put(session)
    store.track(
        sid,
        script="indicator()",
        symbol="BTCUSDT",
        bars=[bar],
        binding={"key": key.text, "last_time": bar["time"], "last_bar": dict(bar)},
    )
    store._sessions.pop(sid, None)
    store._last_seen.pop(sid, None)
    assert store.drop_by_key(key.text) == 1
    assert store.get(sid) is None
    assert store._journal_meta(sid) is None


def test_describe_all_does_not_sweep_journal(tmp_path, monkeypatch):
    """The pool inspector is a snapshot: it must not TTL-delete journal rows."""
    from backend.datastore import session_log

    called: list[float] = []
    real = session_log.sweep_expired

    def spy(path, ttl):
        called.append(float(ttl))
        return real(path, ttl)

    monkeypatch.setattr(session_log, "sweep_expired", spy)
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    store.put(object())
    store._last_sweep = 0.0
    store.describe_all()
    assert called == []
