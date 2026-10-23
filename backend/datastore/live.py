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

"""Bind a live interpret session to a persisted series and advance it.

The session's last bar is the *tail*: the forming bar once one has been seen,
otherwise the last closed bar. Each tick re-ticks the tail in place with
``update_last_bar`` and appends bars that are newer than the tail with
``append_bars``. Closed bars are appended before the forming bar, so the
engine confirms history in order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.datastore.series import SeriesKey


SERIES_BINDING_ATTR = "series_binding"


@dataclass
class SeriesBinding:
    """The series a session is bound to, and the tail bar it currently holds."""

    key: SeriesKey
    last_time: int
    last_bar: dict[str, Any]


@dataclass(frozen=True)
class SyncOutcome:
    result: dict[str, Any]
    appended: int
    forming: bool
    unchanged: bool
    stale: bool = False
    # The previous tail bar was revised in place before appending (its values
    # changed while its time did not). Delta envelopes must include the revised
    # tail cell in addition to the appended bars.
    reticked: bool = False
    # Bars the session grew by in this tick (closed appends + a newer forming
    # bar). Delta tail length is ``grown + reticked``; ``appended`` alone misses
    # a new forming bar, which grows the session without closing anything.
    grown: int = 0


def bind_session(session: Any, key: SeriesKey, last_bar: dict[str, Any]) -> SeriesBinding:
    binding = SeriesBinding(key=key, last_time=int(last_bar["time"]), last_bar=dict(last_bar))
    setattr(session, SERIES_BINDING_ATTR, binding)
    return binding


def binding_of(session: Any) -> SeriesBinding | None:
    binding = getattr(session, SERIES_BINDING_ATTR, None)
    return binding if isinstance(binding, SeriesBinding) else None


def _ohlcv(bar: dict[str, Any]) -> tuple[Any, ...]:
    return (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"])


def sync_session(  # session, binding, candles, clock + two options
    session: Any,
    binding: SeriesBinding,
    candles: list[dict[str, Any]],
    now_s: int,
    *,
    stale: bool = False,
    on_apply: Callable[[str, Any], None] | None = None,
) -> SyncOutcome:
    """Advance ``session`` from ``candles`` (ascending, unique by time).

    ``stale`` marks an outcome whose walk was clamped to a recent window
    because the stored anchor was too old — the client may want to re-sync
    history instead of relying on the bounded catch-up.

    ``on_apply`` (optional) reports each in-place mutation — ``("tick", bar)``
    after ``update_last_bar``, ``("append", bars)`` after ``append_bars`` — so
    the session journal can mirror the object without re-reading it.
    """
    """Advance ``session`` from ``candles`` (ascending, unique by time).

    ``stale`` marks an outcome whose walk was clamped to a recent window
    because the stored anchor was too old — the client may want to re-sync
    history instead of relying on the bounded catch-up.
    """
    interval_s = binding.key.interval_seconds
    tail = binding.last_time
    forming = [c for c in candles if c["time"] + interval_s > now_s]
    forming_bar = forming[-1] if forming else None
    closed_newer = [c for c in candles if c["time"] > tail and c["time"] + interval_s <= now_s]

    result: dict[str, Any] | None = None
    tail_bar = next((c for c in candles if c["time"] == tail), None)
    reticked = False
    if tail_bar is not None and _ohlcv(tail_bar) != _ohlcv(binding.last_bar):
        result = session.update_last_bar(tail_bar)
        binding.last_bar = dict(tail_bar)
        reticked = True
        if on_apply is not None:
            on_apply("tick", tail_bar)

    batch = list(closed_newer)
    if forming_bar is not None and forming_bar["time"] > tail:
        batch.append(forming_bar)
    grown = len(batch)
    if batch:
        result = session.append_bars(batch)
        binding.last_time = int(batch[-1]["time"])
        binding.last_bar = dict(batch[-1])
        if on_apply is not None:
            on_apply("append", batch)

    unchanged = result is None
    if result is None:
        result = session.result()
    return SyncOutcome(
        result=result,
        appended=len(closed_newer),
        forming=forming_bar is not None,
        unchanged=unchanged,
        stale=stale,
        reticked=reticked,
        grown=grown,
    )
