#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="/root/tgpost"
VENV_PY="$PROJECT_DIR/venv/bin/python"
RUN_LOG="$PROJECT_DIR/logs/runner_generate.log"
mkdir -p "$PROJECT_DIR/logs"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] START generation"
  cd "$PROJECT_DIR"
  "$VENV_PY" get_posts.py --config config.json --mode yesterday
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] END generation"
  echo
} >> "$RUN_LOG" 2>&1
