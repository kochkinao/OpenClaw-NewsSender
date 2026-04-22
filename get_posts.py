#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto


@dataclass
class ExportRange:
    start_dt: datetime
    end_dt: datetime
    label: str


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    ensure_dir(str(Path(path).parent))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[\\/*?:"<>|]+', "_", value)
    value = re.sub(r"\s+", "_", value.strip())
    return value[:180] if value else "file"


def setup_logging(log_dir: str = "logs", log_level: str = "INFO"):
    ensure_dir(log_dir)
    logger = logging.getLogger("get_posts")
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "export.log"),
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


def send_alert(config: dict, title: str, body: str, logger) -> None:
    alerts = config.get("alerts", {})
    if not alerts.get("enabled", False):
        return

    bot_token = alerts.get("bot_token")
    chat_id = alerts.get("chat_id")
    if not bot_token or not chat_id:
        logger.warning("alerts.enabled=true, но bot_token/chat_id не заполнены")
        return

    text = f"⚠️ {title}\n\n{body}"[:4000]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        if not r.ok:
            logger.error("Ошибка alert: %s %s", r.status_code, r.text)
    except Exception as e:
        logger.error("Ошибка отправки alert: %s", e)


def retry_request(func, logger, attempts: int = 3, delays=(10, 30, 60)):
    last_error = None
    for i in range(attempts):
        try:
            result = func()
            if hasattr(result, "status_code") and (not result.ok) and result.status_code in {408, 429, 500, 502, 503, 504}:
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


def normalize_channel_ref(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.replace("https://t.me/", "", 1)
    elif value.startswith("http://t.me/"):
        value = value.replace("http://t.me/", "", 1)
    return value.lstrip("@").strip("/")


def get_channels(config: dict, cli_channels=None) -> list[str]:
    channels = cli_channels if cli_channels else config.get("channels", [])
    if not isinstance(channels, list) or not channels:
        raise ValueError("В config.json должен быть непустой список channels")
    return list(dict.fromkeys([normalize_channel_ref(x) for x in channels]))


def get_timezone(config: dict):
    name = config.get("timezone", "Europe/Istanbul")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise RuntimeError(f"Не найдена таймзона '{name}'") from e


def parse_args():
    parser = argparse.ArgumentParser(description="Выгрузка Telegram-сообщений и генерация markdown-постов")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--mode", choices=["yesterday", "date", "range", "days"], default="yesterday")
    parser.add_argument("--date")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--days", type=int)
    parser.add_argument("--channels", nargs="+")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_day_range(target_date: date, tz) -> ExportRange:
    return ExportRange(
        start_dt=datetime.combine(target_date, dtime.min, tzinfo=tz),
        end_dt=datetime.combine(target_date, dtime.max, tzinfo=tz),
        label=target_date.isoformat(),
    )


def resolve_ranges(args, tz) -> list[ExportRange]:
    today = datetime.now(tz).date()

    if args.mode == "yesterday":
        return [build_day_range(today - timedelta(days=1), tz)]
    if args.mode == "date":
        return [build_day_range(parse_iso_date(args.date), tz)]
    if args.mode == "range":
        start = parse_iso_date(args.start_date)
        end = parse_iso_date(args.end_date)
        if end < start:
            raise ValueError("end-date не может быть меньше start-date")
        out = []
        cur = start
        while cur <= end:
            out.append(build_day_range(cur, tz))
            cur += timedelta(days=1)
        return out
    if args.mode == "days":
        if not args.days or args.days <= 0:
            raise ValueError("Для --mode days нужен положительный --days")
        return [build_day_range(today - timedelta(days=i), tz) for i in range(args.days, 0, -1)]
    raise ValueError("Неизвестный режим")


def to_iso(dt):
    return dt.astimezone().replace(microsecond=0).isoformat() if dt else None


def extract_text(message) -> str:
    return message.message or ""


def detect_media_type(message):
    if not message.media:
        return None
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    if isinstance(message.media, MessageMediaDocument):
        mime = getattr(message.file, "mime_type", None)
        if mime:
            if mime.startswith("video/"):
                return "video"
            if mime.startswith("audio/"):
                return "audio"
            if mime.startswith("image/"):
                return "image"
        return "document"
    return type(message.media).__name__


def build_message_item(message):
    item = {
        "id": message.id,
        "type": "message",
        "date": to_iso(message.date),
        "date_unixtime": str(int(message.date.timestamp())) if message.date else None,
        "edited": to_iso(message.edit_date) if message.edit_date else None,
        "edited_unixtime": str(int(message.edit_date.timestamp())) if message.edit_date else None,
        "from": None,
        "from_id": None,
        "text": extract_text(message),
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
        "replies": getattr(message.replies, "replies", None) if getattr(message, "replies", None) else None,
        "reply_to_message_id": getattr(message.reply_to, "reply_to_msg_id", None) if getattr(message, "reply_to", None) else None,
        "media_type": detect_media_type(message),
        "post_author": getattr(message, "post_author", None),
    }
    if getattr(message, "reactions", None):
        item["reactions"] = [
            {"emoji": getattr(r.reaction, "emoticon", None), "count": r.count}
            for r in (getattr(message.reactions, "results", []) or [])
        ]
    return item


async def export_channel_messages(client, channel_ref, start_dt, end_dt, logger):
    entity = await client.get_entity(channel_ref)
    out = {
        "name": getattr(entity, "title", channel_ref),
        "type": "public_channel" if getattr(entity, "username", None) else "channel",
        "id": getattr(entity, "id", None),
        "messages": [],
    }

    async for message in client.iter_messages(entity, reverse=True):
        if not message.date:
            continue
        msg_dt = message.date.astimezone(start_dt.tzinfo)
        if msg_dt < start_dt or msg_dt > end_dt:
            continue
        out["messages"].append(build_message_item(message))

    logger.info("Канал '%s' (%s): выгружено %s сообщений", out["name"], channel_ref, len(out["messages"]))
    return out


async def export_period(client, channels, export_range, timezone_name, logger):
    total_messages = 0
    channels_data = []

    for channel in channels:
        try:
            result = await export_channel_messages(client, channel, export_range.start_dt, export_range.end_dt, logger)
            channels_data.append(result)
            total_messages += len(result["messages"])
        except Exception as e:
            logger.exception("Ошибка выгрузки канала %s: %s", channel, e)
            channels_data.append({"name": channel, "type": "channel", "id": None, "error": str(e), "messages": []})

    return {
        "export_info": {
            "exported_at": datetime.now(export_range.start_dt.tzinfo).replace(microsecond=0).isoformat(),
            "timezone": timezone_name,
            "period": {
                "date": export_range.label,
                "from": export_range.start_dt.replace(microsecond=0).isoformat(),
                "to": export_range.end_dt.replace(microsecond=0).isoformat(),
            },
            "channels_count": len(channels),
            "messages_count": total_messages,
        },
        "channels": channels_data,
    }


def build_output_path(output_dir: str, prefix: str, label: str, exported_at: datetime, ext: str) -> str:
    stamp = exported_at.strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(output_dir, sanitize_filename(f"{prefix}_{label}_exported_{stamp}.{ext}"))


def build_ai_payload(day_payload: dict) -> str:
    lines = [f"Дата: {day_payload['export_info']['period']['date']}", ""]
    for channel in day_payload.get("channels", []):
        lines.append(f"Канал: {channel.get('name', 'unknown')}")
        lines.append(f"Количество сообщений: {len(channel.get('messages', []))}")
        lines.append("")
        for idx, msg in enumerate(channel.get("messages", []), start=1):
            text = (msg.get("text") or "").strip()
            if text:
                lines.append(f"[{idx}] {text}")
        lines.extend(["", "=" * 40, ""])
    return "\n".join(lines).strip()


def extract_json_block(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    return raw.strip()


def call_openrouter(config: dict, prompt_text: str, source_text: str, logger) -> str:
    openrouter = config["openrouter"]
    payload = {
        "model": openrouter.get("model", "google/gemini-2.5-flash"),
        "temperature": openrouter.get("temperature", 0.4),
        "max_tokens": openrouter.get("max_tokens", 4000),
        "messages": [
            {
                "role": "system",
                "content": 'Ты редактор финансового Telegram-канала. Верни строго JSON вида {"posts":[{"title":"...","content":"..."}]}. Без пояснений.'
            },
            {
                "role": "user",
                "content": f"{prompt_text}\n\nИсходные данные:\n{source_text}",
            },
        ],
    }

    headers = {
        "Authorization": f"Bearer {openrouter['api_key']}",
        "Content-Type": "application/json",
        "HTTP-Referer": openrouter.get("site_url") or "https://localhost",
        "X-Title": openrouter.get("app_name", "tgpost"),
    }

    def do_request():
        return requests.post(
            openrouter.get("base_url", "https://openrouter.ai/api/v1/chat/completions"),
            headers=headers,
            json=payload,
            timeout=180,
        )

    response = retry_request(do_request, logger)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def parse_ai_posts(raw: str):
    data = json.loads(extract_json_block(raw))
    out = []
    for item in data.get("posts", []):
        title = str(item.get("title", "")).strip() or "Пост"
        content = str(item.get("content", "")).strip()
        if content:
            out.append({"title": title, "content": content})
    return out


def validate_posts(posts, config):
    validation = config.get("validation", {})
    min_posts = validation.get("min_posts", 1)
    max_posts = validation.get("max_posts", 5)
    min_chars = validation.get("min_chars", 300)
    max_chars = validation.get("max_chars", 3500)

    if len(posts) < min_posts:
        raise ValueError(f"Модель вернула слишком мало постов: {len(posts)}")

    posts = posts[:max_posts]
    valid = []
    for post in posts:
        content = post["content"]
        if len(content) < min_chars:
            continue
        valid.append({"title": post["title"], "content": content})

    if not valid:
        raise ValueError("После валидации не осталось корректных постов")
    return valid


def save_raw_ai_response(raw_dir: str, day_label: str, content: str) -> str:
    ensure_dir(raw_dir)
    path = Path(raw_dir) / f"{day_label}_raw_ai_response.json"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return str(path)


def save_markdown_posts(posts, out_dir: str, day_label: str, logger):
    ensure_dir(out_dir)
    saved = []
    for idx, post in enumerate(posts, start=1):
        path = Path(out_dir) / sanitize_filename(f"{day_label}_{idx:02d}_{post['title']}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(post["content"].strip() + "\n")
        logger.info("Markdown-пост сохранён: %s", path)
        saved.append(str(path))
    return saved


def load_state(path: str):
    return load_json(path) if os.path.exists(path) else {"days": {}, "updated_at": None}


def save_state(path: str, state: dict):
    state["updated_at"] = datetime.now().replace(microsecond=0).isoformat()
    save_json(path, state)


def ensure_day_state(state: dict, day_label: str):
    return state.setdefault("days", {}).setdefault(day_label, {
        "export": {"status": "pending", "json_path": None, "messages_count": 0},
        "generation": {"status": "pending", "posts_created": 0, "md_paths": [], "raw_ai_path": None},
    })


def move_file(src: str, dst_dir: str, logger) -> str:
    ensure_dir(dst_dir)
    src_path = Path(src)
    dst_path = Path(dst_dir) / src_path.name
    if dst_path.exists():
        dst_path = Path(dst_dir) / f"{dst_path.stem}_{datetime.now().strftime('%H%M%S')}{dst_path.suffix}"
    src_path.replace(dst_path)
    logger.info("Файл перемещён: %s -> %s", src_path, dst_path)
    return str(dst_path)


async def main():
    args = parse_args()
    config = load_json(args.config)
    logger = setup_logging(config.get("log_dir", "logs"), config.get("log_level", "INFO"))
    lock_path = config.get("locks", {}).get("get_posts", "locks/get_posts.lock")

    with LockFile(lock_path):
        try:
            logger.info("Запуск get_posts.py | args=%s", vars(args))

            tz = get_timezone(config)
            ranges = resolve_ranges(args, tz)
            channels = get_channels(config, args.channels)
            state_path = config.get("state_path", "state.json")
            state = load_state(state_path)
            output_dir = config.get("output_dir", "exports")
            archive_dir = config.get("archive_exports_dir", "archive_exports")
            ensure_dir(output_dir)
            ensure_dir(config.get("md_output_dir", "generated_posts"))
            ensure_dir(config.get("raw_ai_dir", "failed_ai_responses"))
            ensure_dir(archive_dir)

            tg = config["telegram"]
            timezone_name = config.get("timezone", "Europe/Istanbul")
            day_payloads = []

            async with TelegramClient(tg["session_name"], tg["api_id"], tg["api_hash"]) as client:
                for export_range in ranges:
                    payload = await export_period(client, channels, export_range, timezone_name, logger)
                    day_state = ensure_day_state(state, export_range.label)

                    json_path = build_output_path(output_dir, config.get("output_prefix", "tg_posts"), export_range.label, datetime.now(tz), "json")
                    if not args.dry_run:
                        save_json(json_path, payload)
                        logger.info("JSON сохранён: %s", json_path)
                    else:
                        json_path = None
                        logger.info("dry-run: JSON не сохранён для %s", export_range.label)

                    day_state["export"] = {
                        "status": "done",
                        "json_path": json_path,
                        "messages_count": payload["export_info"]["messages_count"],
                    }
                    day_payloads.append((payload, json_path))

            if not args.skip_ai:
                for payload, json_path in day_payloads:
                    day_label = payload["export_info"]["period"]["date"]
                    day_state = ensure_day_state(state, day_label)
                    try:
                        if args.dry_run:
                            logger.info("dry-run: генерация пропущена для %s", day_label)
                            saved = []
                            raw_path = None
                        else:
                            raw = call_openrouter(
                                config,
                                load_text(config["openrouter"].get("prompt_file", "prompt.txt")),
                                build_ai_payload(payload),
                                logger,
                            )
                            raw_path = save_raw_ai_response(config.get("raw_ai_dir", "failed_ai_responses"), day_label, raw)
                            saved = save_markdown_posts(validate_posts(parse_ai_posts(raw), config), config.get("md_output_dir", "generated_posts"), day_label, logger)

                        day_state["generation"] = {
                            "status": "done" if saved or args.dry_run else "empty",
                            "posts_created": len(saved),
                            "md_paths": saved,
                            "raw_ai_path": raw_path,
                        }

                        if json_path and saved and not args.dry_run:
                            move_file(json_path, str(Path(archive_dir) / day_label), logger)

                    except Exception as e:
                        day_state["generation"] = {
                            "status": "failed",
                            "posts_created": 0,
                            "md_paths": [],
                            "raw_ai_path": None,
                            "error": str(e),
                        }
                        logger.exception("Ошибка генерации за %s: %s", day_label, e)
                        send_alert(
                            config,
                            "Ошибка генерации постов",
                            f"Скрипт: get_posts.py\nСервер: {socket.gethostname()}\nДата: {day_label}\nОшибка: {e}",
                            logger,
                        )

            save_state(state_path, state)
            logger.info("get_posts.py завершён успешно")

        except Exception as e:
            logger.exception("Критическая ошибка get_posts.py: %s", e)
            send_alert(
                config,
                "Критическая ошибка get_posts.py",
                f"Скрипт: get_posts.py\nСервер: {socket.gethostname()}\nОшибка: {e}",
                logger,
            )
            raise


if __name__ == "__main__":
    asyncio.run(main())
