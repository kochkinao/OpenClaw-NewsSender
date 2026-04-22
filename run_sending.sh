#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="/root/tgpost"
VENV_PY="$PROJECT_DIR/venv/bin/python"
RUN_LOG="$PROJECT_DIR/logs/runner_send.log"
mkdir -p "$PROJECT_DIR/logs"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] START sending"
  cd "$PROJECT_DIR"
  "$VENV_PY" send_posts.py --config config.json
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] END sending"
  echo
} >> "$RUN_LOG" 2>&1
