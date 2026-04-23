
import json
import logging
import os
import re
import struct
import sys
import time
import zlib
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
from telethon import utils
from telethon import TelegramClient, functions, types
from telethon.errors import RPCError


def parse_args():
    parser = argparse.ArgumentParser(description="Публикация story через Telethon от имени канала")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--env", choices=["test", "prod"], default=None)
    parser.add_argument("--post-file", required=True)
    parser.add_argument("--message-id", type=int, default=None)
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


def get_publication_metadata(metadata: dict, env_name: str) -> dict:
    publication = metadata.get("publication") or {}
    if publication.get("env") in {None, env_name}:
        return publication
    return {}


def generate_default_story_background(width: int = 1080, height: int = 1920) -> bytes:
    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    top = (18, 32, 48)
    bottom = (7, 12, 20)
    rows = []
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = bytes(
            int(top[i] * (1 - ratio) + bottom[i] * ratio)
            for i in range(3)
        )
        rows.append(b"\x00" + color * width)

    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw, 9))
        + png_chunk(b"IEND", b"")
    )


def resolve_story_background(config: dict, metadata: dict) -> tuple[str | bytes | None, str | None]:
    stories = config.get("content", {}).get("stories", {})
    background_path = stories.get("background_image_path")
    if background_path and Path(background_path).exists():
        return background_path, None

    background_url = stories.get("background_image_url")
    if background_url:
        response = requests.get(background_url, timeout=60)
        response.raise_for_status()
        return response.content, "story_background.jpg"

    # Fallback keeps old posts usable, but story no longer depends on the post image when a background is configured.
    local_image_path = metadata.get("local_image_path")
    if local_image_path and Path(local_image_path).exists():
        return local_image_path, None

    image_url = metadata.get("image_url")
    if image_url:
        response = requests.get(image_url, timeout=60)
        response.raise_for_status()
        return response.content, "story_background.jpg"

    return generate_default_story_background(), "story_background.png"


def build_post_area(config: dict, input_channel, message_id: int):
    area_cfg = config.get("content", {}).get("stories", {}).get("post_area", {})
    coordinates = types.MediaAreaCoordinates(
        x=float(area_cfg.get("x", 50)),
        y=float(area_cfg.get("y", 62)),
        w=float(area_cfg.get("w", 82)),
        h=float(area_cfg.get("h", 34)),
        rotation=float(area_cfg.get("rotation", 0)),
        radius=float(area_cfg.get("radius", 16)),
    )
    return types.InputMediaAreaChannelPost(
        coordinates=coordinates,
        channel=input_channel,
        msg_id=message_id,
    )


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
            publication = get_publication_metadata(metadata, env_name)
            message_id = args.message_id or publication.get("message_id")
            if not message_id:
                raise ValueError("Нет message_id опубликованного поста для story")

            caption = build_story_caption(config, post_text)
            story_chat_id = env_cfg.get("story_chat_id")
            if not story_chat_id:
                raise ValueError(f"Не заполнен environments.{env_name}.story_chat_id")
            channel_chat_id = publication.get("chat_id") or env_cfg.get("channel_chat_id")
            if not channel_chat_id:
                raise ValueError(f"Не заполнен environments.{env_name}.channel_chat_id")

            background, background_file_name = resolve_story_background(config, metadata)
            if args.dry_run:
                logger.info("dry-run: story была бы опубликована | env=%s | file=%s | message_id=%s", env_name, post_file, message_id)
                return

            tg = config.get("telegram", {})
            async with TelegramClient(tg["session_name"], tg["api_id"], tg["api_hash"]) as client:
                peer = await client.get_input_entity(story_chat_id)
                await client(functions.stories.CanSendStoryRequest(peer=peer))

                if isinstance(background, bytes):
                    uploaded = await client.upload_file(background, file_name=background_file_name or "story_background.jpg")
                else:
                    uploaded = await client.upload_file(background)

                media = types.InputMediaUploadedPhoto(file=uploaded)
                channel_entity = await client.get_entity(channel_chat_id)
                input_channel = utils.get_input_channel(channel_entity)
                media_area = build_post_area(config, input_channel, int(message_id))

                await client(functions.stories.SendStoryRequest(
                    peer=peer,
                    media=media,
                    media_areas=[media_area],
                    caption=caption,
                    privacy_rules=[types.InputPrivacyValueAllowAll()],
                    period=stories.get("period", 86400),
                ))

            story_state[f"sent_{env_name}"] = True
            story_state[f"sent_at_{env_name}"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            story_state[f"post_file_{env_name}"] = str(post_file)
            story_state[f"message_id_{env_name}"] = int(message_id)
            save_state(state_path, state)
            logger.info("Story опубликована | env=%s | post=%s | message_id=%s", env_name, post_file, message_id)

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
