#!/bin/sh
# ===================================================
# LJUDBUSTER - entrypoint
# Version: 1.0.9 av Johan Hörnqvist
# Uppdaterad: 2026-02-24
# Changelog:
# - FIX: Flyttar yt-dlp vendor-path från /app/vendor/py till /app/state/vendor/py
#   så runtime-update fungerar med container user 1026.
# - Förtydligad loggning för vendor/state paths.
# ===================================================

set -eu

echo "[entrypoint] LOADED v1.0.9"

# Skrivbar plats (state-mappen är bind-mountad från NAS)
STATE_DIR="/app/state"
VENDOR="${STATE_DIR}/vendor/py"
STAMP="${STATE_DIR}/yt_dlp_update_stamp"

INTERVAL_SECONDS=$((6*60*60))  # 6h
NOW="$(date +%s)"

mkdir -p "$VENDOR" 2>/dev/null || true
mkdir -p "$STATE_DIR" 2>/dev/null || true

export PYTHONPATH="${VENDOR}:${PYTHONPATH:-}"

LAST=0
if [ -f "$STAMP" ]; then
  LAST="$(tr -cd '0-9' < "$STAMP" 2>/dev/null || true)"
  [ -n "$LAST" ] || LAST=0
fi

AGE=$((NOW - LAST))

if [ "$LAST" -gt 0 ] 2>/dev/null && [ "$AGE" -lt "$INTERVAL_SECONDS" ] 2>/dev/null; then
  need_update=0
else
  need_update=1
fi

echo "[entrypoint] state:  $STATE_DIR"
echo "[entrypoint] vendor: $VENDOR"
echo "[entrypoint] node: $(command -v node 2>/dev/null || echo NO)"
echo "[entrypoint] yt-dlp (active): $(python -m yt_dlp --version 2>/dev/null || echo unknown)"

if [ "$need_update" -eq 1 ]; then
  echo "[entrypoint] Updating yt-dlp -> ${VENDOR} (best-effort)…"
  if pip install -U --no-cache-dir --target "$VENDOR" yt-dlp; then
    echo "$NOW" > "$STAMP" || true
    echo "[entrypoint] yt-dlp (after):  $(python -m yt_dlp --version 2>/dev/null || echo unknown)"
  else
    echo "[entrypoint] WARNING: yt-dlp update failed; continuing with existing version."
  fi
else
  echo "[entrypoint] Skipping yt-dlp update (recently updated)."
fi

exec uvicorn main:app --host 0.0.0.0 --port 8000
