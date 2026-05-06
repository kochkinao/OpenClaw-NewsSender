#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="/root/NewsSender"
RUN_LOG="$PROJECT_DIR/logs/runner_story.log"
mkdir -p "$PROJECT_DIR/logs"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Story publishing was removed from the project"
  echo
} >> "$RUN_LOG" 2>&1
