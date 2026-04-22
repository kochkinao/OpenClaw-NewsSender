
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
from dataclasses import dataclass
from datetime import date, time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto


@dataclass
class ExportRange:
    start_dt: datetime
    end_dt: datetime
    label: str


def normalize_channel_ref(value: str) -> str:
    value = value.strip()
    if value.startswith("https://t.me/"):
        value = value.replace("https://t.me/", "", 1)
    elif value.startswith("http://t.me/"):
        value = value.replace("http://t.me/", "", 1)
    return value.lstrip("@").strip("/")


def get_channels(config: dict, cli_channels=None) -> list[str]:
    channels = cli_channels if cli_channels else config.get("content", {}).get("channels", [])
    if not isinstance(channels, list) or not channels:
        raise ValueError("В config.json должен быть непустой список content.channels")
    return list(dict.fromkeys([normalize_channel_ref(x) for x in channels]))


def get_timezone(config: dict):
    name = config.get("runtime", {}).get("timezone", "Europe/Istanbul")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise RuntimeError(f"Не найдена таймзона '{name}'") from e


def parse_args():
    parser = argparse.ArgumentParser(description="Выгрузка Telegram-сообщений, генерация markdown-постов и картинок")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--mode", choices=["yesterday", "date", "range", "days"], default="yesterday")
    parser.add_argument("--date")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--days", type=int)
    parser.add_argument("--channels", nargs="+")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--skip-images", action="store_true")
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
        "text": extract_text(message),
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
        "replies": getattr(message.replies, "replies", None) if getattr(message, "replies", None) else None,
        "media_type": detect_media_type(message),
    }
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


def call_openrouter(config: dict, prompt_text: str, source_text: str, logger, system_prompt: str | None = None) -> str:
    openrouter = config["openrouter"]
    payload = {
        "model": openrouter.get("model", "google/gemini-2.5-flash"),
        "temperature": openrouter.get("temperature", 0.4),
        "max_tokens": openrouter.get("max_tokens", 4000),
        "messages": [
            {
                "role": "system",
                "content": system_prompt or 'Ты редактор финансового Telegram-канала. Верни строго JSON вида {"posts":[{"title":"...","content":"..."}]}. Без пояснений.'
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
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "\n\n[Пост был сокращён автоматически]"
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


def build_image_query(post_text: str, config: dict, logger) -> str:
    prompt = "Сформулируй один короткий поисковый запрос на английском для редакционной иллюстрации к финансовому посту. Только сам запрос."
    raw = call_openrouter(config, prompt, post_text, logger, system_prompt=prompt)
    return raw.strip().replace("\n", " ")[:100]


def score_pixabay_hit(hit: dict) -> tuple:
    downloads = int(hit.get("downloads", 0) or 0)
    likes = int(hit.get("likes", 0) or 0)
    comments = int(hit.get("comments", 0) or 0)
    width = int(hit.get("imageWidth", 0) or 0)
    height = int(hit.get("imageHeight", 0) or 0)
    return (downloads + likes * 3 + comments * 2, width * height)


def search_image_metadata(query: str, config: dict, logger) -> dict | None:
    image_search = config.get("image_search", {})
    if not image_search.get("enabled", False):
        return None
    if image_search.get("provider", "").lower() != "pixabay":
        raise ValueError("Поддержан только Pixabay")
    params = {
        "key": image_search.get("api_key"),
        "q": query,
        "lang": image_search.get("lang", "ru"),
        "image_type": image_search.get("image_type", "photo"),
        "orientation": image_search.get("orientation", "horizontal"),
        "category": image_search.get("category", "business"),
        "safesearch": str(image_search.get("safesearch", True)).lower(),
        "order": image_search.get("order", "popular"),
        "page": 1,
        "per_page": image_search.get("per_page", 5),
    }

    def do_request():
        return requests.get("https://pixabay.com/api/", params=params, timeout=60)

    response = retry_request(do_request, logger)
    response.raise_for_status()
    data = response.json()
    hits = data.get("hits", []) or []
    if not hits:
        return None
    hits = sorted(hits, key=score_pixabay_hit, reverse=True)
    best = hits[0]
    image_url = best.get("largeImageURL") or best.get("webformatURL")
    if not image_url:
        return None
    return {
        "provider": "pixabay",
        "image_query": query,
        "image_url": image_url,
        "page_url": best.get("pageURL"),
        "author": best.get("user"),
        "author_id": best.get("user_id"),
        "tags": best.get("tags"),
    }


def download_image(image_url: str, out_dir: str, day_label: str, idx: int, logger) -> str | None:
    ensure_dir(out_dir)
    file_path = Path(out_dir) / f"{day_label}_{idx:02d}.jpg"

    def do_request():
        return requests.get(image_url, timeout=60)

    response = retry_request(do_request, logger)
    response.raise_for_status()
    with open(file_path, "wb") as f:
        f.write(response.content)
    logger.info("Изображение сохранено: %s", file_path)
    return str(file_path)


def save_post_metadata(md_path: str, metadata: dict, logger) -> str:
    meta_path = str(Path(md_path).with_suffix(".meta.json"))
    save_json(meta_path, metadata)
    logger.info("Metadata сохранена: %s", meta_path)
    return meta_path


def load_state(path: str):
    return load_json(path) if os.path.exists(path) else {"days": {}, "send_queue": [], "updated_at": None}


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
    paths = get_paths(config)
    logger = setup_logging("get_posts", paths.get("log_dir", "logs"), config.get("runtime", {}).get("log_level", "INFO"), "export.log")
    lock_path = config.get("locks", {}).get("get_posts", "locks/get_posts.lock")

    with LockFile(lock_path):
        try:
            tz = get_timezone(config)
            ranges = resolve_ranges(args, tz)
            channels = get_channels(config, args.channels)
            state_path = paths.get("state_path", "state.json")
            state = load_state(state_path)
            output_dir = paths.get("output_dir", "exports")
            archive_dir = paths.get("archive_exports_dir", "archive_exports")
            md_output_dir = paths.get("md_output_dir", "generated_posts")
            media_dir = config.get("image_storage", {}).get("dir", paths.get("media_dir", "generated_media"))

            ensure_dir(output_dir)
            ensure_dir(md_output_dir)
            ensure_dir(media_dir)
            ensure_dir(paths.get("raw_ai_dir", "raw_ai_responses"))
            ensure_dir(archive_dir)

            tg = config["telegram"]
            timezone_name = config.get("runtime", {}).get("timezone", "Europe/Istanbul")
            day_payloads = []

            async with TelegramClient(tg["session_name"], tg["api_id"], tg["api_hash"]) as client:
                for export_range in ranges:
                    payload = await export_period(client, channels, export_range, timezone_name, logger)
                    day_state = ensure_day_state(state, export_range.label)

                    json_path = build_output_path(output_dir, paths.get("output_prefix", "tgposts"), export_range.label, datetime.now(tz), "json")
                    if not args.dry_run:
                        save_json(json_path, payload)
                        logger.info("JSON сохранён: %s", json_path)
                    else:
                        json_path = None
                    day_state["export"] = {"status": "done", "json_path": json_path, "messages_count": payload["export_info"]["messages_count"]}
                    day_payloads.append((payload, json_path))

            if not args.skip_ai:
                for payload, json_path in day_payloads:
                    day_label = payload["export_info"]["period"]["date"]
                    day_state = ensure_day_state(state, day_label)

                    try:
                        if args.dry_run:
                            saved = []
                            raw_path = None
                        else:
                            prompt_text = Path(
                                config["openrouter"].get("prompt_file", "prompt.txt")
                            ).read_text(encoding="utf-8").strip()

                            raw = call_openrouter(
                                config,
                                prompt_text,
                                build_ai_payload(payload),
                                logger,
                            )

                            raw_path = save_raw_ai_response(
                                paths.get("raw_ai_dir", "raw_ai_responses"),
                                day_label,
                                raw,
                            )

                            posts = validate_posts(parse_ai_posts(raw), config)
                            saved = save_markdown_posts(posts, md_output_dir, day_label, logger)

                            if not args.skip_images:
                                for idx, (md_path, post) in enumerate(zip(saved, posts), start=1):
                                    metadata = {
                                        "image_enabled": False,
                                        "image_query": None,
                                        "image_url": None,
                                        "local_image_path": None,
                                    }

                                    try:
                                        image_meta = search_image_metadata(
                                            build_image_query(post["content"], config, logger),
                                            config,
                                            logger,
                                        )

                                        if image_meta:
                                            metadata.update({
                                                "image_enabled": True,
                                                "image_query": image_meta.get("image_query"),
                                                "image_url": image_meta.get("image_url"),
                                                "image_provider": image_meta.get("provider"),
                                                "image_page_url": image_meta.get("page_url"),
                                                "image_author": image_meta.get("author"),
                                                "image_author_id": image_meta.get("author_id"),
                                                "image_tags": image_meta.get("tags"),
                                            })

                                            if config.get("image_storage", {}).get("enabled", True) and config.get("image_storage", {}).get("download", True):
                                                metadata["local_image_path"] = download_image(
                                                    image_meta["image_url"],
                                                    str(Path(media_dir) / day_label),
                                                    day_label,
                                                    idx,
                                                    logger,
                                                )

                                    except Exception as img_err:
                                        logger.warning("Не удалось подобрать/сохранить картинку для %s: %s", md_path, img_err)

                                    save_post_metadata(md_path, metadata, logger)

                        day_state["generation"] = {
                            "status": "done" if saved else "empty",
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
                        send_alert(config, "Ошибка генерации постов", f"Дата: {day_label}\nОшибка: {e}", logger)
            
            save_state(state_path, state)
            logger.info("get_posts.py завершён успешно")

        except Exception as e:
            logger.exception("Критическая ошибка get_posts.py: %s", e)
            send_alert(config, "Критическая ошибка get_posts.py", f"Ошибка: {e}", logger)
            raise


if __name__ == "__main__":
    asyncio.run(main())
