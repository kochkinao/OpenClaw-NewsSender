
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    ensure_dir(str(Path(path).parent))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[\\/*?:"<>|]+', "_", value)
    value = re.sub(r"\s+", "_", value.strip())
    return value[:180] if value else "file"


def setup_logging(name: str, log_dir: str = "logs", log_level: str = "INFO", filename: str | None = None):
    ensure_dir(log_dir)
    logger = logging.getLogger(name)
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    logfile = filename or f"{name}.log"

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, logfile),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


class LockFile:
    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        ensure_dir(str(Path(self.path).parent))
        if os.path.exists(self.path):
            raise RuntimeError(f"Lock-файл уже существует: {self.path}")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return self

    def __exit__(self, exc_type, exc, tb):
        if os.path.exists(self.path):
            os.remove(self.path)


def retry_request(func, logger, attempts: int = 3, delays=(10, 30, 60)):
    last_error = None
    for i in range(attempts):
        try:
            result = func()
            if hasattr(result, "status_code") and (not result.ok) and result.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"HTTP {result.status_code}: {getattr(result, 'text', '')[:500]}")
            return result
        except Exception as e:
            last_error = e
            if i == attempts - 1:
                break
            delay = delays[min(i, len(delays) - 1)]
            logger.warning("Попытка %s/%s не удалась: %s. Повтор через %s сек.", i + 1, attempts, e, delay)
            time.sleep(delay)
    raise last_error


def get_env_name(config: dict, explicit_env: str | None = None) -> str:
    return explicit_env or config.get("env", "test")


def get_env_config(config: dict, explicit_env: str | None = None) -> dict:
    env_name = get_env_name(config, explicit_env)
    envs = config.get("environments", {})
    if env_name not in envs:
        raise ValueError(f"Не найдено окружение '{env_name}' в config.json")
    return envs[env_name]


def get_paths(config: dict) -> dict:
    return config.get("paths", {})


def send_alert(config: dict, title: str, body: str, logger) -> None:
    alerts = config.get("alerts", {})
    if not alerts.get("enabled", False):
        return
    bot_token = config.get("bots", {}).get("alert_bot_token")
    chat_ids = alerts.get("chat_ids") or ([alerts.get("chat_id")] if alerts.get("chat_id") else [])
    if not bot_token or not chat_ids:
        logger.warning("alerts.enabled=true, но alert_bot_token/chat_ids не заполнены")
        return

    text = f"⚠️ {title}\n\n{body}"[:4000]
    for chat_id in chat_ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=30,
            )
            if not r.ok:
                logger.error("Ошибка alert (%s): %s %s", chat_id, r.status_code, r.text)
        except Exception as e:
            logger.error("Ошибка отправки alert (%s): %s", chat_id, e)

import argparse

def parse_args():
    parser = argparse.ArgumentParser(description="Очистка архивов и старых файлов")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()

def iter_files_and_dirs(root: Path):
    if not root.exists():
        return []
    return sorted(root.rglob("*"), reverse=True)

def is_older_than(path: Path, days: int) -> bool:
    cutoff = datetime.now() - timedelta(days=days)
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return mtime < cutoff

def cleanup_path(root_dir: str, days: int, logger, dry_run: bool):
    removed_count = 0
    removed_bytes = 0
    root = Path(root_dir)
    if not root.exists():
        return removed_count, removed_bytes
    for path in iter_files_and_dirs(root):
        try:
            if path.is_file() and is_older_than(path, days):
                size = path.stat().st_size
                if dry_run:
                    logger.info("dry-run remove file: %s", path)
                else:
                    path.unlink(missing_ok=True)
                removed_count += 1
                removed_bytes += size
            elif path.is_dir():
                try:
                    if not any(path.iterdir()):
                        if dry_run:
                            logger.info("dry-run remove empty dir: %s", path)
                        else:
                            path.rmdir()
                except OSError:
                    pass
        except FileNotFoundError:
            continue
    return removed_count, removed_bytes

def main():
    args = parse_args()
    config = load_json(args.config)
    paths = get_paths(config)
    logger = setup_logging("cleanup", paths.get("log_dir", "logs"), config.get("runtime", {}).get("log_level", "INFO"), "cleanup.log")
    retention = config.get("retention", {})
    if not retention.get("enabled", False):
        logger.info("Cleanup отключён")
        return
    lock_path = config.get("locks", {}).get("cleanup", "locks/cleanup.lock")
    with LockFile(lock_path):
        mapping = [
            (paths.get("archive_exports_dir", "archive_exports"), retention.get("archive_exports_days", 30)),
            (paths.get("sent_posts_dir", "sent_posts"), retention.get("sent_posts_days", 30)),
            (paths.get("sent_media_dir", "sent_media"), retention.get("sent_media_days", 30)),
            (paths.get("failed_posts_dir", "failed_posts"), retention.get("failed_posts_days", 60)),
            (paths.get("failed_media_dir", "failed_media"), retention.get("failed_media_days", 60)),
            (paths.get("raw_ai_dir", "raw_ai_responses"), retention.get("raw_ai_days", 14)),
            (paths.get("log_dir", "logs"), retention.get("logs_days", 30)),
        ]
        total_files = 0
        total_bytes = 0
        for root_dir, days in mapping:
            removed_count, removed_bytes = cleanup_path(root_dir, days, logger, args.dry_run)
            total_files += removed_count
            total_bytes += removed_bytes
            logger.info("path=%s | days=%s | removed=%s | bytes=%s", root_dir, days, removed_count, removed_bytes)
        logger.info("Cleanup завершён | файлов: %s | байт: %s | dry-run=%s", total_files, total_bytes, args.dry_run)

if __name__ == "__main__":
    main()
