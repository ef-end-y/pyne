#!/bin/sh
# Copyright (C) 2024-2026 jango_blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Production entrypoint for the Pro API (gunicorn).
# Worker/thread/timeout counts are env-tunable without rebuilding the image.
#
# WebSocket (/ws/run) note:
#   Default gthread/sync workers do NOT speak WebSocket. AXIS will fall back
#   to POST /run. For native WS, run a compatible stack (e.g. geventwebsocket)
#   or keep preferWs=false in AXIS settings. Raise GUNICORN_TIMEOUT for large
#   compile-mode runs (default 120s).
#
# Warm compile (H2):
#   PYNE_COMPILE_DISK_CACHE=1 (default) + PYNE_COMPILE_CACHE_DIR=/data/compile-cache
#   PYNE_COMPILE_PREWARM=1 → once-per-worker builtin warm on first /run (and
#   optional POST /compile/prewarm from readiness probes).

set -eu

PORT="${PORT:-8080}"
# Default 1, deliberately. The live-session routes (/session, …/append, …/tick)
# keep parsed script + series + TA state in process so a re-tick is O(1); that
# state is per-process, so a second worker would 404 on ~half of all follow-up
# calls. gevent still handles thousands of concurrent connections per worker.
# Override only if session state has moved to an external store.
WORKERS="${GUNICORN_WORKERS:-1}"
THREADS="${GUNICORN_THREADS:-4}"
TIMEOUT="${GUNICORN_TIMEOUT:-120}"
BIND="${GUNICORN_BIND:-0.0.0.0:${PORT}}"
# Worker recycling bounds per-worker memory growth across requests
# (Numba / caches). 0 disables. Mirrors the VPS systemd unit.
MAX_REQUESTS="${GUNICORN_MAX_REQUESTS:-500}"
MAX_REQUESTS_JITTER="${GUNICORN_MAX_REQUESTS_JITTER:-50}"

# Ensure IR / Numba disk cache directory exists on the data volume (best-effort).
CACHE_DIR="${PYNE_COMPILE_CACHE_DIR:-/data/compile-cache}"
if [ "${PYNE_COMPILE_DISK_CACHE:-1}" != "0" ]; then
  mkdir -p "${CACHE_DIR}" 2>/dev/null || true
fi
if [ "${PYNE_RUNNER:-0}" != "0" ]; then
  mkdir -p "$(dirname "${PYNE_RUNNER_DB:-/data/runner.db}")" 2>/dev/null || true
fi

exec gunicorn \
  --bind "${BIND}" \
  --workers "${WORKERS}" \
  --threads "${THREADS}" \
  --timeout "${TIMEOUT}" \
  --max-requests "${MAX_REQUESTS}" \
  --max-requests-jitter "${MAX_REQUESTS_JITTER}" \
  --access-logfile "-" \
  --error-logfile "-" \
  backend.app:app
