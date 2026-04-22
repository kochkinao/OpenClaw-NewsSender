#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="/root/tgpost"
VENV_PY="$PROJECT_DIR/venv/bin/python"
cd "$PROJECT_DIR"
"$VENV_PY" send_test_alert.py --config config.json
