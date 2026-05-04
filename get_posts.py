
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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


def get_lock_path(config: dict, primary_key: str, fallback_key: str | None, default: str) -> str:
    locks = config.get("locks", {})
    return locks.get(primary_key) or (locks.get(fallback_key) if fallback_key else None) or default


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


class OpenRouterAPIError(RuntimeError):
    def __init__(self, message: str, *, diagnostic_path: str | None = None, status_code: int | None = None, response_excerpt: str | None = None):
        super().__init__(message)
        self.diagnostic_path = diagnostic_path
        self.status_code = status_code
        self.response_excerpt = response_excerpt


def clip_text(value: str | None, limit: int = 1500) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def save_openrouter_diagnostic(config: dict, logger, kind: str, payload: dict) -> str:
    raw_dir = get_paths(config).get("raw_ai_dir", "raw_ai_responses")
    ensure_dir(raw_dir)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = Path(raw_dir) / f"openrouter_{kind}_{stamp}.json"
    save_json(str(path), payload)
    logger.error("Диагностика OpenRouter сохранена: %s", path)
    return str(path)


def call_openrouter(config: dict, prompt_text: str, source_text: str, logger, system_prompt: str | None = None) -> str:
    openrouter = config["openrouter"]
    payload = {
        "model": openrouter.get("model", "google/gemini-2.5-flash"),
        "temperature": openrouter.get("temperature", 0.4),
        "max_tokens": openrouter.get("max_tokens", 4000),
        "messages": [
            {
                "role": "system",
                "content": system_prompt or (
                    'Ты редактор финансового Telegram-канала. '
                    'Группируй входящие новости по смыслу и не растягивай слабый день на лишние посты. '
                    'Верни строго JSON вида {"posts":[{"title":"...","content":"..."}]}. Без пояснений.'
                )
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
    attempts = int(openrouter.get("response_attempts", openrouter.get("attempts", 3)) or 3)
    delays = tuple(openrouter.get("response_retry_delays", [5, 15, 30])) or (5, 15, 30)

    last_error = None
    for attempt in range(1, attempts + 1):
        response = None
        try:
            response = retry_request(do_request, logger)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if not str(content).strip():
                raise ValueError("OpenRouter вернул пустой content")
            return content
        except Exception as e:
            last_error = e
            response_text = getattr(response, "text", "") if response is not None else ""
            status_code = getattr(response, "status_code", None) if response is not None else None
            diagnostic_payload = {
                "kind": "chat_completions_error",
                "attempt": attempt,
                "attempts": attempts,
                "status_code": status_code,
                "content_type": (response.headers.get("Content-Type") if response is not None and getattr(response, "headers", None) else None),
                "model": openrouter.get("model"),
                "base_url": openrouter.get("base_url"),
                "app_name": openrouter.get("app_name"),
                "error_type": type(e).__name__,
                "error_message": str(e),
                "response_excerpt": clip_text(response_text, 3000),
                "prompt_excerpt": clip_text(prompt_text, 2000),
                "system_prompt_excerpt": clip_text(system_prompt, 1200),
                "source_excerpt": clip_text(source_text, 3000),
                "request_payload_preview": {
                    "temperature": payload.get("temperature"),
                    "max_tokens": payload.get("max_tokens"),
                    "messages_count": len(payload.get("messages", [])),
                },
            }
            diagnostic_path = save_openrouter_diagnostic(config, logger, "chat", diagnostic_payload)
            if attempt >= attempts:
                raise OpenRouterAPIError(
                    f"OpenRouter не вернул корректный ответ после {attempts} попыток: {e}",
                    diagnostic_path=diagnostic_path,
                    status_code=status_code,
                    response_excerpt=clip_text(response_text, 500),
                ) from e
            delay = delays[min(attempt - 1, len(delays) - 1)]
            logger.warning(
                "OpenRouter attempt %s/%s завершился ошибкой: %s. Повтор через %s сек. Диагностика: %s",
                attempt,
                attempts,
                e,
                delay,
                diagnostic_path,
            )
            time.sleep(delay)
    raise last_error


def parse_ai_posts(raw: str):
    data = json.loads(extract_json_block(raw))
    out = []
    for item in data.get("posts", []):
        title = str(item.get("title", "")).strip() or "Пост"
        content = str(item.get("content", "")).strip()
        if content:
            out.append({"title": title, "content": content})
    return out


def normalize_post_title(title: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "").strip())
    title = re.sub(r"^(?:📌\s*)+", "", title).strip()
    return title or "Пост"


def ensure_required_post_format(post: dict) -> dict:
    title = normalize_post_title(post.get("title", "Пост"))
    content = str(post.get("content", "")).strip()
    if re.search(r"^\s*(?:📌\s*)+", content, flags=re.MULTILINE):
        content = re.sub(r"^\s*(?:📌\s*)+", f"📌 {title}", content, count=1, flags=re.MULTILINE)
    else:
        content = f"📌 {title}\n\n{content}".strip()

    section_patterns = [
        r"📌\s+.+",
        r"📰\s*Новость\s*:",
        r"📊\s*Выжимка\s*:",
        r"📉\s*Влияние на рынок\s*:",
        r"💡\s*Как это использовать\s*:",
        r"⚠️\s*Дисклеймер\s*:",
    ]
    missing_sections = [pattern for pattern in section_patterns if not re.search(pattern, content, flags=re.IGNORECASE)]
    if missing_sections:
        raise ValueError(f"Пост не прошёл форматную проверку, отсутствуют обязательные блоки: {len(missing_sections)}")

    return {"title": title, "content": content}


def analyze_day_signal(day_payload: dict, config: dict) -> dict:
    validation = config.get("validation", {})
    messages_count = int(day_payload.get("export_info", {}).get("messages_count", 0) or 0)
    unique_texts = []
    total_chars = 0
    for channel in day_payload.get("channels", []):
        for message in channel.get("messages", []):
            text = str(message.get("text") or "").strip()
            if not text:
                continue
            unique_texts.append(text)
            total_chars += len(text)
    min_posts = int(validation.get("min_posts", 3))
    max_posts = int(validation.get("max_posts", 5))
    dense_day = messages_count >= max_posts or total_chars >= 5000 or len(unique_texts) >= max_posts
    recommended_posts = max_posts if dense_day else min_posts
    return {
        "messages_count": messages_count,
        "source_chars": total_chars,
        "dense_day": dense_day,
        "recommended_posts": recommended_posts,
    }


def build_post_count_guidance(day_payload: dict, config: dict) -> str:
    signal = analyze_day_signal(day_payload, config)
    validation = config.get("validation", {})
    min_posts = int(validation.get("min_posts", 3))
    max_posts = int(validation.get("max_posts", 5))
    if signal["dense_day"]:
        return (
            f"Верни от {min_posts} до {max_posts} постов. "
            f"Сегодня данных много: ориентируйся на {max_posts} постов, если это действительно оправдано смыслом. "
            "Не дроби одну тему на несколько слабых постов."
        )
    return (
        f"Верни от {min_posts} до {max_posts} постов. "
        f"Сегодня данных немного: собери {min_posts} сильных поста, объединяя близкие по смыслу новости. "
        "Не растягивай день на лишние темы."
    )


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
        normalized = ensure_required_post_format(post)
        content = normalized["content"]
        if len(content) < min_chars:
            continue
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "\n\n[Пост был сокращён автоматически]"
        valid.append({"title": normalized["title"], "content": content})
    if not valid:
        raise ValueError("После валидации не осталось корректных постов")
    return valid


def estimate_visual_relevance(post: dict) -> int:
    text = f"{post.get('title', '')}\n{post.get('content', '')}".lower()
    keyword_weights = {
        "ipo": 6,
        "листинг": 6,
        "мосбир": 6,
        "бирж": 5,
        "дивид": 5,
        "акци": 4,
        "облигац": 5,
        "банк": 4,
        "сбер": 4,
        "доходност": 4,
        "ставк": 4,
        "китай": 3,
        "технолог": 4,
        "nvidia": 5,
        "gold": 3,
        "нефть": 3,
        "газ": 3,
        "валют": 3,
    }
    score = 0
    for keyword, weight in keyword_weights.items():
        if keyword in text:
            score += weight
    return score


def fallback_image_queries(post: dict) -> list[str]:
    text = f"{post.get('title', '')}\n{post.get('content', '')}".lower()
    if any(token in text for token in ["ipo", "листинг", "мосбир", "бирж"]):
        return [
            "stock exchange trading screen ipo",
            "technology company ipo trading floor",
            "traders watching exchange board",
        ]
    if any(token in text for token in ["дивид", "банк", "сбер"]):
        return [
            "bank dividend stock chart",
            "shareholders meeting finance chart",
            "bank earnings trading screen",
        ]
    if any(token in text for token in ["облигац", "офз", "yield", "доходност"]):
        return [
            "government bonds yield chart",
            "fixed income desk trading screen",
            "bond market finance chart",
        ]
    return [
        "financial market analysis news",
        "stock exchange trading screen",
        "investment market data monitor",
    ]


def reorder_posts_for_daily_photo(posts: list[dict], config: dict, logger) -> list[dict]:
    rules = get_image_rules(config)
    if not rules["daily_photo_enabled"]:
        return posts
    target_index = rules["daily_photo_index"] - 1
    if target_index < 0 or target_index >= len(posts):
        return posts

    limit = rules["daily_photo_max_chars"]
    ranked_candidates = []
    for idx, post in enumerate(posts):
        content_len = len(post["content"].strip())
        if content_len > limit:
            continue
        ranked_candidates.append((estimate_visual_relevance(post), -content_len, idx))

    if not ranked_candidates:
        return posts

    ranked_candidates.sort(reverse=True)
    best_idx = ranked_candidates[0][2]
    if best_idx == target_index:
        return posts

    posts[target_index], posts[best_idx] = posts[best_idx], posts[target_index]
    logger.info(
        "Пост #%s переставлен в daily photo slot: %s -> %s",
        rules["daily_photo_index"],
        best_idx + 1,
        rules["daily_photo_index"],
    )
    return posts


def rewrite_photo_slot_post(post: dict, config: dict, logger, limit: int) -> dict:
    target_limit = limit
    content = post["content"]
    prompt = (
        f"Если это можно сделать без потери смысла, перепиши финансовый Telegram-пост в короткий вариант до {target_limit} символов. "
        "Если для сохранения смысла нужен более длинный текст — верни исходный пост без сокращения. "
        "Нельзя обрывать фразы, нельзя терять главный смысл, нельзя выдумывать факты. "
        "Сохрани стиль исходного канала: эмодзи в заголовке и названиях блоков обязательны. "
        "Сохрани структуру: 📌 заголовок, 📰 новость, 📉 влияние на рынок, 💡 как использовать, ⚠️ дисклеймер. "
        "Верни только готовый текст поста без JSON и без markdown-блоков."
    )
    raw = call_openrouter(config, prompt, content, logger, system_prompt=prompt)
    rewritten = extract_json_block(raw).strip()
    if len(rewritten) <= limit:
        return {"title": post["title"], "content": rewritten}
    logger.info("Photo-пост оставлен текстовым: разумное сокращение не уложилось в лимит (%s > %s)", len(rewritten), limit)
    return post


def enforce_daily_photo_post(posts: list[dict], config: dict, logger) -> list[dict]:
    rules = get_image_rules(config)
    if not rules["daily_photo_enabled"]:
        return posts
    posts = reorder_posts_for_daily_photo(posts, config, logger)
    index = rules["daily_photo_index"] - 1
    if index < 0 or index >= len(posts):
        return posts
    limit = rules["daily_photo_max_chars"]
    post = posts[index]
    content = post["content"].strip()
    if len(content) <= limit:
        return posts
    try:
        rewritten = rewrite_photo_slot_post(post, config, logger, limit)
    except Exception as e:
        logger.warning("Не удалось переписать photo slot пост #%s: %s", rules["daily_photo_index"], e)
        return posts
    if len(rewritten["content"]) <= limit:
        posts[index] = rewritten
        logger.info("Пост #%s переписан AI для daily photo slot: %s -> %s символов", rules["daily_photo_index"], len(content), len(rewritten["content"]))
    else:
        logger.warning("Пост #%s остался длиннее лимита photo slot: %s > %s", rules["daily_photo_index"], len(rewritten["content"]), limit)
    return posts


def get_image_rules(config: dict) -> dict:
    validation = config.get("validation", {})
    image_search = config.get("image_search", {})
    caption_limit = int(validation.get("photo_caption_limit", image_search.get("photo_caption_limit", 1000)))
    daily_photo = image_search.get("daily_photo_post", {})
    return {
        "short_posts_only": image_search.get("short_posts_only", True),
        "max_chars": int(image_search.get("max_post_chars", caption_limit)),
        "min_chars": int(image_search.get("min_post_chars", 0)),
        "daily_photo_enabled": daily_photo.get("enabled", True),
        "daily_photo_index": int(daily_photo.get("index", 3)),
        "daily_photo_max_chars": int(daily_photo.get("max_post_chars", caption_limit)),
    }


def get_image_skip_reason(post_text: str, config: dict, post_index: int | None = None) -> str | None:
    image_search = config.get("image_search", {})
    if not image_search.get("enabled", False):
        return "image_search_disabled"

    rules = get_image_rules(config)
    length = len(post_text.strip())
    if (
        rules["daily_photo_enabled"]
        and post_index == rules["daily_photo_index"]
        and length <= rules["daily_photo_max_chars"]
    ):
        return None
    if rules["short_posts_only"] and length > rules["max_chars"]:
        return f"post_too_long_for_image:{length}>{rules['max_chars']}"
    if rules["min_chars"] and length < rules["min_chars"]:
        return f"post_too_short_for_image:{length}<{rules['min_chars']}"
    return None


def save_raw_ai_response(raw_dir: str, day_label: str, content: str) -> str:
    ensure_dir(raw_dir)
    path = Path(raw_dir) / f"{day_label}_raw_ai_response.json"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return str(path)


def generate_valid_posts(config: dict, prompt_text: str, source_text: str, day_payload: dict, day_label: str, raw_dir: str, logger):
    last_error = None
    last_raw_path = None
    guidance = build_post_count_guidance(day_payload, config)
    for attempt in range(1, 3):
        effective_prompt = prompt_text + "\n\n" + guidance
        if attempt > 1:
            effective_prompt = (
                effective_prompt
                + "\n\nКРИТИЧЕСКИ ВАЖНО: верни только полностью валидный JSON без markdown-блоков. "
                + "Не обрывай строки. Нужно вернуть от 3 до 5 полноценных постов. "
                + "Если тем мало — собери 3 сильных поста, объединив связанные новости, но не выдумывай факты."
            )
        raw = call_openrouter(config, effective_prompt, source_text, logger)
        suffix = "" if attempt == 1 else f"_attempt_{attempt}"
        last_raw_path = save_raw_ai_response(raw_dir, f"{day_label}{suffix}", raw)
        try:
            posts = parse_ai_posts(raw)
            return posts, last_raw_path
        except json.JSONDecodeError as e:
            last_error = e
            logger.warning("AI вернул невалидный JSON за %s, попытка %s/2: %s", day_label, attempt, e)
    raise last_error


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


def normalize_image_query(raw: str) -> str:
    query = re.sub(r"[^A-Za-z0-9 ,&/-]+", " ", raw.strip().replace("\n", " "))
    query = re.sub(r"\s+", " ", query).strip(" ,-/")[:100]
    return query


def build_image_queries(post: dict, config: dict, logger) -> list[str]:
    post_text = f"{post.get('title', '').strip()}\n{post.get('content', '').strip()}".strip()
    prompt = (
        "Извлеки визуальный смысл финансового поста и сформулируй 3 разных поисковых запроса на английском для Pixabay. "
        "Каждый запрос должен быть конкретным, 4-8 слов, без кавычек и без нумерации. "
        "Запросы должны отличаться визуальным объектом или ракурсом, а не только перестановкой слов. "
        "Избегай общих формулировок вроде business, finance, money, stock market без уточнения. "
        "Не ищи буквальные русские названия компаний; ищи тематическую рыночную иллюстрацию. "
        "Если тема IPO/listing/биржа — используй объекты вроде trading screen, stock exchange board, tech office, traders. "
        "Если тема дивидендов — используй dividend, bank, shareholders, stock chart. "
        "Если тема облигаций — используй government bonds, yield chart, fixed income desk. "
        "Верни только 3 строки, по одному запросу на строку."
    )
    try:
        raw = call_openrouter(config, prompt, post_text, logger, system_prompt=prompt)
        queries = []
        for line in raw.splitlines():
            query = normalize_image_query(line)
            if query:
                queries.append(query)
        generic_queries = {"business", "finance", "money", "stock market", "financial market", "it company ipo"}
        unique_queries = []
        for query in queries:
            if query.lower() in generic_queries:
                continue
            if query.lower() not in {item.lower() for item in unique_queries}:
                unique_queries.append(query)
        if unique_queries:
            return unique_queries[:3]
    except Exception as e:
        logger.warning("AI не смог построить image queries, используем fallback: %s", e)
    return fallback_image_queries(post)


def score_pixabay_hit(hit: dict, query: str, recent_signatures: dict | None = None) -> tuple:
    downloads = int(hit.get("downloads", 0) or 0)
    likes = int(hit.get("likes", 0) or 0)
    comments = int(hit.get("comments", 0) or 0)
    width = int(hit.get("imageWidth", 0) or 0)
    height = int(hit.get("imageHeight", 0) or 0)
    tags = (hit.get("tags") or "").lower()
    query_words = {word for word in re.findall(r"[a-z]{4,}", query.lower()) if word not in {"market", "finance", "financial", "company"}}
    overlap_score = sum(1 for word in query_words if word in tags)
    author_penalty = 0
    tag_penalty = 0
    image_penalty = 0
    if recent_signatures:
        image_id = str(hit.get("id") or hit.get("pageURL") or "")
        author_penalty = recent_signatures.get("author_penalties", {}).get(str(hit.get("user_id") or ""), 0)
        tag_penalty = recent_signatures.get("tag_penalties", {}).get(tags, 0) if tags else 0
        image_penalty = recent_signatures.get("image_penalties", {}).get(image_id, 0) if image_id else 0
    return (overlap_score * 30 + downloads + likes * 3 + comments * 2 - author_penalty - tag_penalty - image_penalty, width * height)


def is_generic_pixabay_hit(hit: dict, query: str) -> bool:
    tags = (hit.get("tags") or "").lower()
    page_url = (hit.get("pageURL") or "").lower()
    haystack = f"{tags} {page_url}"
    generic_terms = {
        "coffee", "кофе", "cup", "чашка", "pen", "ручка", "post-it", "notebook",
        "блокнот", "office", "офис", "desk", "стол", "meeting", "совещание",
        "handshake", "рукопожатие", "laptop", "ноутбук"
    }
    strong_terms = {
        "stock", "stocks", "exchange", "trading", "chart", "market", "ipo", "shares",
        "dividend", "bond", "yield", "bank", "finance", "investment", "software",
        "technology", "data", "screen", "биржа", "акции", "график", "рынок"
    }
    has_generic = any(term in haystack for term in generic_terms)
    has_strong = any(term in haystack for term in strong_terms)
    query_words = {w for w in re.findall(r"[a-z]{4,}", query.lower()) if w not in {"company", "financial", "market"}}
    overlaps_query = any(word in haystack for word in query_words)
    return has_generic and not (has_strong or overlaps_query)


def collect_recent_image_signatures(config: dict) -> dict:
    paths = get_paths(config)
    meta_roots = [
        paths.get("md_output_dir", "generated_posts"),
        paths.get("sent_posts_dir", "sent_posts"),
        paths.get("failed_posts_dir", "failed_posts"),
    ]
    digest_dir = paths.get("digest_dir", "generated_digests")
    signatures = {
        "author_ids": set(),
        "tags": set(),
        "image_ids": set(),
        "author_penalties": {},
        "tag_penalties": {},
        "image_penalties": {},
    }
    recent_meta_files = []
    for root_dir in meta_roots:
        root = Path(root_dir)
        if not root.exists():
            continue
        recent_meta_files.extend(root.rglob("*.meta.json"))
    digest_root = Path(digest_dir)
    if digest_root.exists():
        recent_meta_files.extend(digest_root.glob("*_digest.json"))

    def path_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    for index, meta_path in enumerate(sorted(recent_meta_files, key=path_mtime, reverse=True)[:40]):
        try:
            payload = load_json(str(meta_path))
        except Exception:
            continue
        author_id = str(payload.get("image_author_id") or "").strip()
        tags = str(payload.get("image_tags") or "").strip().lower()
        image_id = str(payload.get("image_id") or payload.get("image_page_url") or payload.get("image_url") or "").strip()
        if author_id:
            signatures["author_ids"].add(author_id)
        if tags:
            signatures["tags"].add(tags)
        if image_id:
            signatures["image_ids"].add(image_id)

        if index < 3:
            image_weight = 1000
            author_weight = 180
            tag_weight = 160
        elif index < 8:
            image_weight = 220
            author_weight = 80
            tag_weight = 70
        else:
            image_weight = 80
            author_weight = 30
            tag_weight = 25

        if image_id:
            signatures["image_penalties"][image_id] = max(signatures["image_penalties"].get(image_id, 0), image_weight)
        if author_id:
            signatures["author_penalties"][author_id] = max(signatures["author_penalties"].get(author_id, 0), author_weight)
        if tags:
            signatures["tag_penalties"][tags] = max(signatures["tag_penalties"].get(tags, 0), tag_weight)
    return signatures


def search_image_metadata(queries: list[str], config: dict, logger) -> dict | None:
    image_search = config.get("image_search", {})
    if not image_search.get("enabled", False):
        return None
    if image_search.get("provider", "").lower() != "pixabay":
        raise ValueError("Поддержан только Pixabay")
    recent_signatures = collect_recent_image_signatures(config)
    orientations = []
    preferred_orientation = image_search.get("orientation", "horizontal")
    for value in [preferred_orientation, "horizontal", "vertical"]:
        if value and value not in orientations:
            orientations.append(value)

    best_hit = None
    best_query = None
    best_orientation = None
    for query in queries:
        for orientation in orientations:
            params = {
                "key": image_search.get("api_key"),
                "q": query,
                "lang": image_search.get("lang", "ru"),
                "image_type": image_search.get("image_type", "photo"),
                "orientation": orientation,
                "category": image_search.get("category", "business"),
                "safesearch": str(image_search.get("safesearch", True)).lower(),
                "order": image_search.get("order", "popular"),
                "page": 1,
                "per_page": max(int(image_search.get("per_page", 5)), 15),
            }

            def do_request():
                return requests.get("https://pixabay.com/api/", params=params, timeout=60)

            response = retry_request(do_request, logger)
            response.raise_for_status()
            data = response.json()
            hits = data.get("hits", []) or []
            filtered_hits = [hit for hit in hits if not is_generic_pixabay_hit(hit, query)]
            if not filtered_hits:
                continue
            filtered_hits = sorted(filtered_hits, key=lambda hit: score_pixabay_hit(hit, query, recent_signatures), reverse=True)
            candidate = filtered_hits[0]
            candidate_score = score_pixabay_hit(candidate, query, recent_signatures)
            if best_hit is None or candidate_score > score_pixabay_hit(best_hit, best_query or query, recent_signatures):
                best_hit = candidate
                best_query = query
                best_orientation = orientation

    if not best_hit:
        logger.warning("Pixabay не дал релевантных картинок для запросов: %s", "; ".join(queries))
        return None

    best = best_hit
    image_url = best.get("largeImageURL") or best.get("webformatURL")
    if not image_url:
        return None
    return {
        "provider": "pixabay",
        "image_query": best_query,
        "image_queries": queries,
        "image_orientation": best_orientation,
        "image_url": image_url,
        "page_url": best.get("pageURL"),
        "author": best.get("user"),
        "author_id": best.get("user_id"),
        "tags": best.get("tags"),
        "image_id": best.get("id"),
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
    lock_path = get_lock_path(config, "get_posts", "collect", "locks/get_posts.lock")

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

                            posts_raw, raw_path = generate_valid_posts(
                                config,
                                prompt_text,
                                build_ai_payload(payload),
                                payload,
                                day_label,
                                paths.get("raw_ai_dir", "raw_ai_responses"),
                                logger,
                            )

                            posts = enforce_daily_photo_post(validate_posts(posts_raw, config), config, logger)
                            saved = save_markdown_posts(posts, md_output_dir, day_label, logger)

                            for idx, (md_path, post) in enumerate(zip(saved, posts), start=1):
                                image_rules = get_image_rules(config)
                                metadata = {
                                    "image_enabled": False,
                                    "image_policy": "daily_photo_slot" if idx == image_rules["daily_photo_index"] else "short_post_only",
                                    "image_query": None,
                                    "image_queries": [],
                                    "image_url": None,
                                    "local_image_path": None,
                                    "image_skipped_reason": None,
                                    "post_chars": len(post["content"].strip()),
                                    "photo_slot_requested": idx == image_rules["daily_photo_index"],
                                    "photo_slot_ready": idx == image_rules["daily_photo_index"] and len(post["content"].strip()) <= image_rules["daily_photo_max_chars"],
                                }

                                skip_reason = "skip_images_cli" if args.skip_images else get_image_skip_reason(post["content"], config, idx)
                                if skip_reason:
                                    metadata["image_skipped_reason"] = skip_reason
                                    logger.info("Картинка не нужна для %s: %s", md_path, skip_reason)
                                    save_post_metadata(md_path, metadata, logger)
                                    continue

                                try:
                                    image_queries = build_image_queries(post, config, logger)
                                    metadata["image_queries"] = image_queries
                                    image_meta = search_image_metadata(image_queries, config, logger)

                                    if image_meta:
                                        metadata.update({
                                            "image_enabled": True,
                                            "image_query": image_meta.get("image_query"),
                                            "image_queries": image_meta.get("image_queries", image_queries),
                                            "image_url": image_meta.get("image_url"),
                                            "image_provider": image_meta.get("provider"),
                                            "image_page_url": image_meta.get("page_url"),
                                            "image_author": image_meta.get("author"),
                                            "image_author_id": image_meta.get("author_id"),
                                            "image_tags": image_meta.get("tags"),
                                            "image_id": image_meta.get("image_id"),
                                            "image_orientation": image_meta.get("image_orientation"),
                                        })

                                        if config.get("image_storage", {}).get("enabled", True) and config.get("image_storage", {}).get("download", True):
                                            metadata["local_image_path"] = download_image(
                                                image_meta["image_url"],
                                                str(Path(media_dir) / day_label),
                                                day_label,
                                                idx,
                                                logger,
                                            )
                                    else:
                                        metadata["image_skipped_reason"] = "no_relevant_image_found"

                                except Exception as img_err:
                                    metadata["image_skipped_reason"] = f"image_error:{img_err}"
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
                        diagnostic_path = getattr(e, "diagnostic_path", None)
                        day_state["generation"] = {
                            "status": "failed",
                            "posts_created": 0,
                            "md_paths": [],
                            "raw_ai_path": None,
                            "error": str(e),
                            "diagnostic_path": diagnostic_path,
                        }
                        logger.exception("Ошибка генерации за %s: %s", day_label, e)
                        alert_lines = [f"Дата: {day_label}", f"Ошибка: {e}"]
                        if diagnostic_path:
                            alert_lines.append(f"Диагностика: {diagnostic_path}")
                        if getattr(e, "status_code", None):
                            alert_lines.append(f"HTTP: {e.status_code}")
                        if getattr(e, "response_excerpt", None):
                            alert_lines.append(f"Ответ: {e.response_excerpt}")
                        send_alert(config, "Ошибка генерации постов", "\n".join(alert_lines), logger)
            
            save_state(state_path, state)
            failed_days = [label for label, payload in state.get("days", {}).items() if payload.get("generation", {}).get("status") == "failed"]
            if failed_days:
                logger.warning("get_posts.py завершён с ошибками генерации: %s", ", ".join(failed_days))
            else:
                logger.info("get_posts.py завершён успешно")

        except Exception as e:
            logger.exception("Критическая ошибка get_posts.py: %s", e)
            send_alert(config, "Критическая ошибка get_posts.py", f"Ошибка: {e}", logger)
            raise


if __name__ == "__main__":
    asyncio.run(main())
