#!/usr/bin/env python3
import json
import os
from pathlib import Path

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    if not os.path.exists("config.json"):
        print("FAIL: config.json не найден")
        return 1

    cfg = load_json("config.json")
    checks = [
        ("telegram.api_id", cfg.get("telegram", {}).get("api_id")),
        ("telegram.api_hash", cfg.get("telegram", {}).get("api_hash")),
        ("openrouter.api_key", cfg.get("openrouter", {}).get("api_key")),
        ("sender.bot_token", cfg.get("sender", {}).get("bot_token")),
        ("sender.chat_id", cfg.get("sender", {}).get("chat_id")),
    ]

    failed = False
    for name, value in checks:
        if not value:
            print(f"FAIL: не заполнено {name}")
            failed = True

    for p in ["exports", "archive_exports", "generated_posts", "sent_posts", "failed_posts", "failed_ai_responses", "locks", "logs"]:
        Path(p).mkdir(parents=True, exist_ok=True)

    if failed:
        return 1

    print("OK: базовая проверка пройдена")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
