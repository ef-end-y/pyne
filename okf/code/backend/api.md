---
type: "Code Module"
title: "backend/api"
description: "HTTP API blueprints for the Pro API (beyond core /run routes)."
resource: "backend/api"
tags: [api, backend, code]
status: stable
generated:
  by: process:axis-okf/1
  at: 2026-10-17T11:02:46Z
sources:
  - id: tree
    resource: "backend/api"
    title: "backend/api"
    author: process:git
okf_lock: generated
---

# Files

* `__init__.py`
* `data_series.py` — collect_candles, data_error_response, fetch_history, provider_for, series_bars, series_fetch, series_provide, series_status, sync_data_session
* `datafeed.py` — bind_session, fetch_markets, fetch_ohlcv, gateway_health, register_watch_route, run_watch_producer, unbind_session, watch_rest_poll
* `feed_manager.py` — ExchangeFeed, FeedManager, apply_feed_candles, get_feed_manager, normalize_candles, now_seconds
* `git_oauth.py` — device_poll, device_start
* `live_ensure.py` — ensure_live_session, key_text_symbol, parse_live_key, resume_live_session
* `live_push.py` — PushHub, attach_feed, emit_push_frames, emit_series_push, persist_feed_bars
* `live_ws.py` — LiveConnection, LiveRegistry, canonical_series_key, clamp_history_bars, closed_upto, extract_handshake_key, make_close, make_delta, make_full, make_status, register_live_route, serve_live_ws
* `lsp_http.py` — lsp_completion, lsp_convert, lsp_diagnostics, lsp_hover
* `preview.py` — chart_preview, indicator_preview, quick_backtest
* `runner.py` — cron_run, delete_script, get_cron_jobs, get_script, list_scripts, put_cron_jobs, put_script
