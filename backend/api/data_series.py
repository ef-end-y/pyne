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

"""Engine-owned OHLCV routes (``/data/series*``) and CCXT paging.

AXIS sends market identity only. PYNE Pro persists the bars (see
:mod:`backend.datastore.series`) and fetches them through the same CCXT
provider the datafeed gateway uses. ``/data/session*`` routes are registered in
``backend/app.py`` next to ``/session`` because they share its helpers.
"""

from __future__ import annotations

import logging

from collections.abc import Callable
from typing import Any

from flask import Blueprint
from flask import jsonify
from flask import request

from backend.api import datafeed
from backend.datastore import live
from backend.datastore import series as series_store
from backend.datastore.series import DataError
from backend.datastore.series import SeriesKey
from backend.middleware.auth import require_api_key
from backend.middleware.schemas import DATA_SERIES_FETCH_SCHEMA
from backend.middleware.schemas import DATA_SERIES_PROVIDE_SCHEMA
from backend.middleware.schemas import validate


logger = logging.getLogger(__name__)

bp = Blueprint("data_series", __name__, url_prefix="/data")

# CCXT page size and a hard stop on pages per request (bounds a runaway walk).
FETCH_PAGE_LIMIT = 1000
MAX_FETCH_PAGES = 200
# A tick re-reads this many bars before the stored newest so a corrected or
# still-forming bar is picked up.
TICK_OVERLAP_BARS = 2
# A tick only ever needs to catch up a few bars. When the stored anchor is
# staler than this window the walk is CLAMPED to the most recent TICK_MAX_BARS
# bars instead of paging through the whole gap — a stale anchor used to run the
# full 200-page budget (~200k sequential CCXT round trips), pin the request for
# minutes and serialize every other request on the same series behind key_lock.
TICK_MAX_BARS = 2500
SOURCE_FETCH = "ccxt-fetch"
SOURCE_TICK = "ccxt-tick"


def data_error_response(exc: DataError) -> tuple[Any, int]:
    return jsonify(exc.body()), exc.status


def provider_for(exchange: str) -> Any | None:
    """CCXT provider for *exchange*, or ``None`` when CCXT is not installed."""
    _, credentials = datafeed._resolve_session(exchange, "")
    return datafeed._make_provider(exchange, credentials)


def _ccxt_unavailable() -> DataError:
    return DataError(
        "CCXT_UNAVAILABLE",
        "CCXT not installed. Install with: pip install 'pynescript[datafeed]'",
        503,
    )


def collect_candles(
    provider: Any,
    key: SeriesKey,
    *,
    since_ms: int,
    now_ms: int,
    max_bars: int | None = None,
) -> list[dict[str, float | int]]:
    """Walk forward through CCXT ``fetch_ohlcv`` pages starting at ``since_ms``.

    Each page starts just after the previous page's newest candle. The walk
    stops when the newest candle is the forming bar, when a page is empty, when
    ``max_bars`` candles are held, or after ``MAX_FETCH_PAGES`` pages. Returns
    candles ascending by time, unique per time.
    """
    interval_s = key.interval_seconds
    now_s = now_ms // 1000
    found: dict[int, dict[str, float | int]] = {}
    cursor_ms = since_ms
    for _ in range(MAX_FETCH_PAGES):
        raw = provider.fetch_ohlcv(key.symbol, key.interval, since=cursor_ms, limit=FETCH_PAGE_LIMIT)
        rows = [
            bar
            for bar in (datafeed._candle_to_bar(c) for c in raw or [])
            if bar is not None and int(bar["time"]) * 1000 >= cursor_ms
        ]
        if not rows:
            break
        for bar in rows:
            found[int(bar["time"])] = bar
        last = max(int(bar["time"]) for bar in rows)
        if last + interval_s > now_s:
            break
        cursor_ms = (last + interval_s) * 1000
        if max_bars is not None and len(found) >= max_bars:
            break
    return [found[t] for t in sorted(found)]


def _collect_or_raise(
    provider: Any,
    key: SeriesKey,
    *,
    since_ms: int,
    now_ms: int,
    max_bars: int | None,
) -> list[dict[str, float | int]]:
    try:
        return collect_candles(provider, key, since_ms=since_ms, now_ms=now_ms, max_bars=max_bars)
    except DataError:
        raise
    except Exception as exc:  # every provider failure is an upstream failure here
        if isinstance(exc, ImportError) or "not installed" in str(exc).lower():
            raise _ccxt_unavailable() from exc
        logger.warning("data series upstream %s: %s", key.exchange, exc)
        raise DataError("UPSTREAM", f"Failed to fetch OHLCV from {key.exchange}.", 502) from exc


def fetch_history(key: SeriesKey, history_bars: int) -> dict[str, Any]:
    """Fetch the newest ``history_bars`` bars for *key* and persist them."""
    provider = provider_for(key.exchange)
    if provider is None:
        raise _ccxt_unavailable()
    store = series_store.get_store()
    with store.key_lock(key.text):
        now_ms = series_store.now_seconds() * 1000
        since_ms = max(0, now_ms - history_bars * key.interval_seconds * 1000)
        candles = _collect_or_raise(provider, key, since_ms=since_ms, now_ms=now_ms, max_bars=history_bars)
        window = candles[-history_bars:]
        status = store.provide(key, window, source=SOURCE_FETCH)
    return {"fetched": len(window), **status}


def sync_data_session(
    session: Any,
    binding: live.SeriesBinding,
    *,
    is_alive: Callable[[], bool] | None = None,
    on_apply: Callable[[str, Any], None] | None = None,
) -> live.SyncOutcome:
    """Pull candles since the stored newest and advance *session*.

    The network walk runs **outside** ``key_lock`` so a slow upstream cannot
    hold up every other request for the same series (head-of-line blocking);
    the lock only guards the fast local commit: the status read, the liveness
    check, the closed-bar persist and the session advance.

    When the stored anchor is staler than :data:`TICK_MAX_BARS`, the walk is
    clamped to a bounded recent window and the outcome is flagged ``stale`` so
    the client can re-sync history instead of paging through the whole gap.

    ``is_alive`` (optional) re-checks session liveness after the fetch; when it
    returns ``False`` the tick raises ``SESSION_GONE`` before persisting or
    advancing anything.
    """
    key = binding.key
    interval_s = key.interval_seconds
    provider = provider_for(key.exchange)
    if provider is None:
        raise _ccxt_unavailable()
    store = series_store.get_store()
    now_s = series_store.now_seconds()

    with store.key_lock(key.text):
        newest = store.status(key)["newest"]
    anchor = binding.last_time if newest is None else int(newest)
    stale = now_s - anchor > TICK_MAX_BARS * interval_s
    if stale:
        # Clamp to a bounded recent window instead of walking the whole gap.
        since_ms = max(0, now_s - (TICK_MAX_BARS + TICK_OVERLAP_BARS) * interval_s) * 1000
    else:
        since_ms = max(0, anchor - TICK_OVERLAP_BARS * interval_s) * 1000

    # Upstream walk outside the per-series lock. Bounded by the clamp above,
    # by max_bars and by MAX_FETCH_PAGES in collect_candles.
    candles = _collect_or_raise(provider, key, since_ms=since_ms, now_ms=now_s * 1000, max_bars=TICK_MAX_BARS)

    # The local commit (liveness, persist, session advance) is fast, so it
    # serializes concurrent ticks under the per-series lock — but the network
    # walk above does not hold it.
    with store.key_lock(key.text):
        if is_alive is not None and not is_alive():
            raise DataError("SESSION_GONE", "Session was released while the tick was in flight.", 404)
        closed = [c for c in candles if c["time"] + interval_s <= now_s]
        if closed:
            store.provide(key, closed, source=SOURCE_TICK)
        return live.sync_session(session, binding, candles, now_s, stale=stale, on_apply=on_apply)


def _key_from_args(args: Any) -> SeriesKey:
    return series_store.make_key(args.get("exchange"), args.get("symbol"), args.get("interval"), args.get("market"))


def _key_from_body(body: dict[str, Any]) -> SeriesKey:
    return series_store.make_key(body.get("exchange"), body.get("symbol"), body.get("interval"), body.get("market"))


def _int_arg(args: Any, name: str, default: int, low: int, high: int) -> int:
    raw = args.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise DataError(f"INVALID_{name.upper()}", f"{name} must be an integer between {low} and {high}") from exc
    if value < low or value > high:
        raise DataError(f"INVALID_{name.upper()}", f"{name} must be an integer between {low} and {high}")
    return value


bp.register_error_handler(DataError, data_error_response)


@bp.route("/series", methods=["GET"])
@require_api_key
def series_status():
    """Stored bounds for one series. Unknown series report count 0."""
    key = _key_from_args(request.args)
    return jsonify({"status": "success", **series_store.get_store().status(key)}), 200


@bp.route("/series/bars", methods=["GET"])
@require_api_key
def series_bars():
    """Newest stored bars for a series, ascending."""
    key = _key_from_args(request.args)
    limit = _int_arg(
        request.args,
        "limit",
        series_store.DEFAULT_WINDOW_LIMIT,
        1,
        series_store.MAX_WINDOW_LIMIT,
    )
    bars = series_store.get_store().window(key, limit)
    return jsonify({"status": "success", "key": key.text, "count": len(bars), "bars": bars}), 200


@bp.route("/series/provide", methods=["POST"])
@require_api_key
def series_provide():
    """Upsert closed bars uploaded by the client (at most 5,000 per request)."""
    validated, err = validate(request.get_json(silent=True) or {}, DATA_SERIES_PROVIDE_SCHEMA)
    if err is not None:
        return err
    key = _key_from_body(validated)
    bars = validated["bars"]
    if len(bars) > series_store.MAX_PROVIDE_BARS:
        return jsonify(
            {
                "status": "error",
                "code": "BATCH_TOO_LARGE",
                "message": f"At most {series_store.MAX_PROVIDE_BARS} bars per request; split the upload into chunks.",
            }
        ), 413
    result = series_store.get_store().provide(key, bars, source=validated["source"])
    return jsonify(
        {
            "status": "success",
            "key": key.text,
            "added": result["added"],
            "updated": result["updated"],
            "count": result["count"],
            "newest": result["newest"],
        }
    ), 200


@bp.route("/series/fetch", methods=["POST"])
@require_api_key
def series_fetch():
    """Fetch the newest ``history_bars`` bars from the exchange via CCXT and persist them."""
    validated, err = validate(request.get_json(silent=True) or {}, DATA_SERIES_FETCH_SCHEMA)
    if err is not None:
        return err
    key = _key_from_body(validated)
    history = series_store.check_history_bars(validated["history_bars"])
    result = fetch_history(key, history)
    return jsonify(
        {
            "status": "success",
            "key": key.text,
            "fetched": result["fetched"],
            "count": result["count"],
            "oldest": result["oldest"],
            "newest": result["newest"],
        }
    ), 200
