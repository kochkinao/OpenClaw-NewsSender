#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="/root/tgpost"
VENV_PY="$PROJECT_DIR/venv/bin/python"
RUN_LOG="$PROJECT_DIR/logs/runner_story.log"
mkdir -p "$PROJECT_DIR/logs"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] START story"
  cd "$PROJECT_DIR"
  FIRST_POST=$(ls sent_posts/*.md 2>/dev/null | sort | head -n 1 || true)
  if [ -n "$FIRST_POST" ]; then
    "$VENV_PY" send_story.py --config config.json --post-file "$FIRST_POST"
  else
    echo "No sent posts found"
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] END story"
  echo
} >> "$RUN_LOG" 2>&1
