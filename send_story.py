
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
import asyncio
from telethon import TelegramClient, functions, types
from telethon.errors import RPCError


def parse_args():
    parser = argparse.ArgumentParser(description="Публикация story через Telethon от имени канала")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--env", choices=["test", "prod"], default=None)
    parser.add_argument("--post-file", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_state(path: str):
    return load_json(path) if os.path.exists(path) else {"days": {}, "send_queue": [], "updated_at": None}


def save_state(path: str, state: dict):
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_json(path, state)


def read_metadata(md_path: Path) -> dict:
    meta_path = md_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    try:
        return load_json(str(meta_path))
    except Exception:
        return {}


def build_story_caption(config: dict, post_text: str) -> str:
    stories = config.get("content", {}).get("stories", {})
    caption = stories.get("caption", "").strip()
    if stories.get("append_title_from_post", False):
        first_line = next((line.strip() for line in post_text.splitlines() if line.strip()), "")
        if first_line:
            return f"{caption}\n\n{first_line}"[:2048] if caption else first_line[:2048]
    return caption[:2048]


async def main():
    args = parse_args()
    config = load_json(args.config)
    paths = get_paths(config)
    logger = setup_logging("send_story", paths.get("log_dir", "logs"), config.get("runtime", {}).get("log_level", "INFO"), "stories.log")
    lock_path = config.get("locks", {}).get("send_story", "locks/send_story.lock")

    with LockFile(lock_path):
        try:
            stories = config.get("content", {}).get("stories", {})
            if not stories.get("enabled", False):
                logger.info("Stories отключены")
                return

            env_name = get_env_name(config, args.env)
            env_cfg = get_env_config(config, args.env)
            post_file = Path(args.post_file)
            if not post_file.exists():
                raise FileNotFoundError(f"Файл поста не найден: {post_file}")

            state_path = paths.get("state_path", "state.json")
            state = load_state(state_path)
            day_label = post_file.name[:10]
            day_state = state.setdefault("days", {}).setdefault(day_label, {})
            story_state = day_state.setdefault("story", {})
            if stories.get("use_first_post_only", True) and story_state.get(f"sent_{env_name}", False):
                logger.info("Story за %s для %s уже опубликована", day_label, env_name)
                return

            post_text = post_file.read_text(encoding="utf-8").strip()
            metadata = read_metadata(post_file)
            local_image_path = metadata.get("local_image_path")
            image_url = metadata.get("image_url")
            if not local_image_path and not image_url:
                raise ValueError("Нет изображения для story")

            caption = build_story_caption(config, post_text)
            story_chat_id = env_cfg.get("story_chat_id")
            if not story_chat_id:
                raise ValueError(f"Не заполнен environments.{env_name}.story_chat_id")

            if args.dry_run:
                logger.info("dry-run: story была бы опубликована | env=%s | file=%s", env_name, post_file)
                return

            tg = config.get("telegram", {})
            async with TelegramClient(tg["session_name"], tg["api_id"], tg["api_hash"]) as client:
                peer = await client.get_input_entity(story_chat_id)
                await client(functions.stories.CanSendStoryRequest(peer=peer))

                if local_image_path and Path(local_image_path).exists():
                    uploaded = await client.upload_file(local_image_path)
                else:
                    response = requests.get(image_url, timeout=60)
                    response.raise_for_status()
                    uploaded = await client.upload_file(response.content)

                media = types.InputMediaUploadedPhoto(file=uploaded)

                await client(functions.stories.SendStoryRequest(
                    peer=peer,
                    media=media,
                    caption=caption,
                    privacy_rules=[types.InputPrivacyValueAllowAll()],
                    period=stories.get("period", 86400),
                ))

            story_state[f"sent_{env_name}"] = True
            story_state[f"sent_at_{env_name}"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            story_state[f"post_file_{env_name}"] = str(post_file)
            save_state(state_path, state)
            logger.info("Story опубликована | env=%s | post=%s", env_name, post_file)

        except RPCError as e:
            logger.exception("RPC ошибка send_story.py: %s", e)
            send_alert(config, "Ошибка публикации story", f"Ошибка: {e}", logger)
            raise
        except Exception as e:
            logger.exception("Критическая ошибка send_story.py: %s", e)
            send_alert(config, "Критическая ошибка send_story.py", f"Ошибка: {e}", logger)
            raise


if __name__ == "__main__":
    asyncio.run(main())
