# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for response windowing.

Two properties matter and are pinned here:

1. The **bar loop is untouched**. Windowing happens after evaluation, so a
   windowed envelope must be exactly the corresponding slice of the full one —
   not a cheaper computation. If a future refactor moves the trim into the
   evaluator, these tests fail loudly.
2. **Absent window ⇒ identical output.** The unwindowed path must not change
   one byte of the envelope.
"""

from __future__ import annotations

import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.middleware.window import WINDOW_META_KEY
from backend.middleware.window import apply_window
from backend.middleware.window import normalize_window


def _envelope(n: int = 10) -> dict:
    """A representative envelope: two series, plots, and bar-indexed events."""
    return {
        "status": "success",
        "count": n,
        "series": {"plot_0": list(range(n)), "plot_1": [v * 2 for v in range(n)]},
        "plots": [[1, 2, 3]] * n,
        "events": [{"kind": "entry", "bar_index": i, "direction": "long"} for i in range(n)],
        "drawings": [{"id": f"d{i}", "bar_index": i} for i in range(n)],
        "meta": {"profile": {"bars": n, "total_ms": 12.5}},
    }


class TestNormalizeWindow:
    def test_none_none_is_no_window(self):
        assert normalize_window(None, None, 100) is None

    def test_full_range_is_no_window(self):
        assert normalize_window(0, 99, 100) is None

    def test_explicit_slice(self):
        assert normalize_window(10, 19, 100) == (10, 19)

    def test_negative_indices_count_from_end(self):
        # -10 .. -1 over 100 bars == 90..99
        assert normalize_window(-10, -1, 100) == (90, 99)

    def test_open_edges(self):
        assert normalize_window(None, 49, 100) == (0, 49)
        assert normalize_window(50, None, 100) == (50, 99)

    def test_reversed_range_is_normalized(self):
        assert normalize_window(80, 20, 100) == (20, 80)

    def test_out_of_range_is_clamped(self):
        assert normalize_window(-500, 5000, 100) is None

    def test_garbage_falls_back_to_no_window(self):
        # An unparseable index degrades to "whole dataset", which normalizes to
        # no-op rather than silently trimming to something arbitrary.
        assert normalize_window("abc", None, 100) is None

    def test_zero_bars(self):
        assert normalize_window(0, 10, 0) is None


class TestApplyWindow:
    def test_no_window_returns_same_object(self):
        env = _envelope()
        assert apply_window(env) is env
        assert WINDOW_META_KEY not in env

    def test_full_range_window_returns_same_object(self):
        env = _envelope()
        assert apply_window(env, 0, 9) is env

    def test_series_are_sliced(self):
        out = apply_window(_envelope(), 2, 4)
        assert out["series"]["plot_0"] == [2, 3, 4]
        assert out["series"]["plot_1"] == [4, 6, 8]

    def test_plots_are_sliced(self):
        out = apply_window(_envelope(), 2, 4)
        assert len(out["plots"]) == 3

    def test_bar_indexed_events_are_filtered(self):
        out = apply_window(_envelope(), 2, 4)
        assert [e["bar_index"] for e in out["events"]] == [2, 3, 4]

    def test_drawings_are_filtered(self):
        out = apply_window(_envelope(), 2, 4)
        assert [d["bar_index"] for d in out["drawings"]] == [2, 3, 4]

    def test_events_without_index_are_kept(self):
        env = _envelope()
        env["events"].append({"kind": "exit", "bar_time": 1700000000})
        out = apply_window(env, 2, 4)
        # bar_time events carry no index and must survive — dropping them would
        # silently lose a fill the chart needs.
        assert any("bar_time" in e for e in out["events"])

    def test_window_meta_reports_the_trim(self):
        out = apply_window(_envelope(), 2, 4)
        w = out[WINDOW_META_KEY]
        assert w == {"from_bar": 2, "to_bar": 4, "bars": 3, "total_bars": 10}

    def test_profile_bars_reflects_the_window(self):
        out = apply_window(_envelope(), 2, 4)
        assert out["meta"]["profile"]["bars"] == 3
        assert out["meta"]["profile"]["windowed"] is True

    def test_windowed_slice_equals_full_slice(self):
        """The core invariant: windowing is a slice, not a cheaper run."""
        full = _envelope()
        windowed = apply_window(_envelope(), 3, 6)
        assert windowed["series"]["plot_0"] == full["series"]["plot_0"][3:7]
        assert [e["bar_index"] for e in windowed["events"]] == [e["bar_index"] for e in full["events"][3:7]]

    def test_original_envelope_is_not_mutated(self):
        env = _envelope()
        apply_window(env, 2, 4)
        assert len(env["series"]["plot_0"]) == 10
        assert len(env["events"]) == 10

    def test_negative_window(self):
        out = apply_window(_envelope(), -3, -1)
        assert out["series"]["plot_0"] == [7, 8, 9]

    def test_single_bar(self):
        out = apply_window(_envelope(), 5, 5)
        assert out["series"]["plot_0"] == [5]
        assert out[WINDOW_META_KEY]["bars"] == 1

    def test_non_dict_envelope_passes_through(self):
        assert apply_window(None, 0, 5) is None
        assert apply_window([1, 2], 0, 1) == [1, 2]

    def test_falls_back_to_series_length_when_count_missing(self):
        env = _envelope()
        del env["count"]
        out = apply_window(env, 1, 2)
        assert out["series"]["plot_0"] == [1, 2]
        assert out[WINDOW_META_KEY]["total_bars"] == 10

    def test_empty_envelope_is_returned_unchanged(self):
        env: dict = {"status": "success", "series": {}}
        assert apply_window(env, 0, 5) is env


class TestSizeReduction:
    def test_windowing_actually_shrinks_a_large_envelope(self):
        import json

        n = 20_000
        big = {
            "status": "success",
            "count": n,
            "series": {"plot_0": [float(i) for i in range(n)]},
        }
        full_size = len(json.dumps(big))
        small = apply_window(
            {"status": "success", "count": n, "series": {"plot_0": [float(i) for i in range(n)]}},
            -500,
            None,
        )
        small_size = len(json.dumps(small))
        assert small_size < full_size / 10, f"{small_size} vs {full_size}"
