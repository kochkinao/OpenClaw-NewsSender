
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from html import escape
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from get_posts import build_image_queries, download_image, search_image_metadata


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
import socket

TELEGRAM_HARD_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
DEFAULT_PHOTO_POST_LIMIT = 1000


def load_state(path: str):
    return load_json(path) if os.path.exists(path) else {"days": {}, "send_queue": [], "updated_at": None}


def save_state(path: str, state: dict):
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_json(path, state)


def load_markdown_files(posts_dir: str):
    path = Path(posts_dir)
    return sorted([p for p in path.glob("*.md") if p.is_file()], key=lambda p: p.name) if path.exists() else []


def split_text_for_telegram(text: str, limit: int = TELEGRAM_HARD_LIMIT):
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return [p for p in parts if p]


def split_caption_and_remainder(text: str):
    text = text.strip()
    if len(text) <= TELEGRAM_CAPTION_LIMIT:
        return text, []
    caption = text[:TELEGRAM_CAPTION_LIMIT]
    cut = caption.rfind("\n\n")
    if cut == -1:
        cut = caption.rfind("\n")
    if cut == -1:
        cut = caption.rfind(" ")
    if cut == -1:
        cut = TELEGRAM_CAPTION_LIMIT
    caption = text[:cut].strip()
    remainder = text[cut:].strip()
    return caption, split_text_for_telegram(remainder)


def build_reply_markup(config: dict, env_cfg: dict):
    cta = env_cfg.get("cta") or config.get("content", {}).get("cta", {})
    enabled = cta.get("enabled", False)
    text = cta.get("text", "").strip()
    url = cta.get("url", "").strip()
    if not enabled or not text or not url:
        return None
    return {"inline_keyboard": [[{"text": text, "url": url}]]}


def send_message(bot_token: str, chat_id: str, text: str, parse_mode=None, disable_web_page_preview=False, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": disable_web_page_preview}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json=payload, timeout=60)


def send_photo(bot_token: str, chat_id: str, photo, caption: str, parse_mode=None, reply_markup=None):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    if isinstance(photo, str) and photo.startswith("http"):
        payload = {"chat_id": chat_id, "photo": photo, "caption": caption}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return requests.post(url, json=payload, timeout=60)
    with open(photo, "rb") as f:
        data = {"chat_id": chat_id, "caption": caption}
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        files = {"photo": f}
        return requests.post(url, data=data, files=files, timeout=60)


def extract_message_id(response) -> int | None:
    if response is None or not response.ok:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    result = payload.get("result") or {}
    return result.get("message_id")


def get_photo_post_limit(config: dict) -> int:
    validation = config.get("validation", {})
    image_search = config.get("image_search", {})
    return int(validation.get("photo_caption_limit", image_search.get("max_post_chars", DEFAULT_PHOTO_POST_LIMIT)))


def shorten_text(value: str | None, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def format_context_lines(context: dict) -> str:
    lines = []
    for key, value in context.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(item) for item in value)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def build_post_link(chat_id: str | None, message_id: int | None) -> str | None:
    if not chat_id or not message_id:
        return None
    chat_id = str(chat_id).strip()
    if chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}/{message_id}"
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", chat_id):
        return f"https://t.me/{chat_id}/{message_id}"
    return None


def strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


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
    digest_dir = get_paths(config).get("digest_dir") or get_paths(config).get("log_dir", "logs")
    ensure_dir(digest_dir)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = Path(digest_dir) / f"openrouter_{kind}_{stamp}.json"
    save_json(str(path), payload)
    logger.error("Диагностика OpenRouter сохранена: %s", path)
    return str(path)


def call_openrouter(config: dict, prompt_text: str, source_text: str, logger, system_prompt: str | None = None) -> str:
    openrouter = config["openrouter"]
    payload = {
        "model": openrouter.get("model", "google/gemini-2.5-flash"),
        "temperature": openrouter.get("temperature", 0.4),
        "max_tokens": min(int(openrouter.get("max_tokens", 4000)), 2500),
        "messages": [
            {
                "role": "system",
                "content": system_prompt or "Ты редактор финансового Telegram-канала. Верни только итоговый текст без пояснений.",
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
            }
            diagnostic_path = save_openrouter_diagnostic(config, logger, "digest", diagnostic_payload)
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


def is_permanent_telegram_error(status_code: int, response_text: str) -> bool:
    lowered = response_text.lower()
    markers = ["chat not found", "bot is not a member", "bot is not an administrator", "forbidden", "unauthorized", "invalid token"]
    return status_code in {400, 401, 403} and any(marker in lowered for marker in markers)


def move_file(src: Path, dst_dir: str, logger) -> str:
    ensure_dir(dst_dir)
    dst = Path(dst_dir) / src.name
    if dst.exists():
        dst = Path(dst_dir) / f"{src.stem}_{time.strftime('%H%M%S')}{src.suffix}"
    src.replace(dst)
    logger.info("Файл перемещён: %s -> %s", src, dst)
    return str(dst)


def load_post_metadata(md_path: Path) -> dict:
    meta_path = md_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    try:
        return load_json(str(meta_path))
    except Exception:
        return {}


def move_associated_files(src_md: Path, target_posts_dir: str, media_target_dir: str | None, logger):
    moved = {"md": None, "meta": None, "media": None}
    moved["md"] = move_file(src_md, target_posts_dir, logger)
    meta_path = src_md.with_suffix(".meta.json")
    meta_payload = None
    if meta_path.exists():
        try:
            meta_payload = load_json(str(meta_path))
        except Exception:
            meta_payload = None
        moved["meta"] = move_file(meta_path, target_posts_dir, logger)

    if media_target_dir and meta_payload:
        local_image_path = meta_payload.get("local_image_path")
        if local_image_path and Path(local_image_path).exists():
            moved["media"] = move_file(Path(local_image_path), media_target_dir, logger)
            if moved["meta"]:
                new_meta = load_json(moved["meta"])
                new_meta["local_image_path"] = moved["media"]
                save_json(moved["meta"], new_meta)

    return moved


def save_publication_metadata(meta_path: str | None, publication: dict) -> None:
    if not meta_path:
        return
    payload = load_json(meta_path) if Path(meta_path).exists() else {}
    payload.setdefault("publication", {}).update(publication)
    save_json(meta_path, payload)


def extract_post_title(post_text: str, fallback: str = "Пост") -> str:
    for line in post_text.splitlines():
        line = line.strip()
        if not line:
            continue
        return line.replace("📌", "").strip(" -—\t")[:120] or fallback
    return fallback


def strip_leading_symbols(text: str) -> str:
    return re.sub(r"^[^\wА-Яа-яЁё]+", "", text, flags=re.UNICODE).strip()


def format_russian_date(date_obj) -> str:
    months = {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    }
    return f"{date_obj.day} {months[date_obj.month]} {date_obj.year} года"


def normalize_digest_title(title: str) -> str:
    title = strip_leading_symbols(title.replace("📌", "").strip())
    title = re.sub(r"^(новость|что случилось|пост)\s*:\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title)
    return title.strip(" .:-") or "Важная новость"


def inject_link_into_one_word(title: str, link: str) -> str:
    words = title.split()
    if not words:
        return f'<a href="{escape(link, quote=True)}">подробнее</a>'

    preferred_index = len(words) // 2
    stop_words = {
        "и", "в", "во", "на", "по", "с", "со", "для", "от", "до", "из", "к", "ко",
        "у", "о", "об", "под", "при", "но", "или", "а", "не", "что", "это",
    }
    keyword_roots = [
        "индекс", "инвест", "дивид", "доход", "крипт", "минфин", "сбер", "офз",
        "банк", "акц", "облигац", "ipo", "бирж", "технолог", "выруч", "рын",
        "налог", "недвиж", "ии", "nvidia", "amazon", "positive", "cyан", "циан",
    ]

    ranked = []
    for idx, word in enumerate(words):
        clean_word = re.sub(r"[^\wА-Яа-яЁё-]+", "", word, flags=re.UNICODE)
        lowered = clean_word.lower()
        if not lowered or lowered in stop_words:
            continue
        score = 0
        if any(root in lowered for root in keyword_roots):
            score += 100
        if len(clean_word) >= 10:
            score += 20
        elif len(clean_word) >= 7:
            score += 12
        elif len(clean_word) >= 5:
            score += 6
        if re.search(r"[A-ZА-ЯЁ]", clean_word):
            score += 12
        score -= abs(idx - preferred_index)
        ranked.append((score, idx))

    if ranked:
        ranked.sort(reverse=True)
        chosen_index = ranked[0][1]
    else:
        chosen_index = min(preferred_index, len(words) - 1)

    escaped_words = [escape(word) for word in words]
    escaped_words[chosen_index] = f'<a href="{escape(link, quote=True)}">{escaped_words[chosen_index]}</a>'
    return " ".join(escaped_words)


def should_send_digest(config: dict, env_name: str, post_filename: str, state: dict) -> bool:
    daily_digest = config.get("content", {}).get("daily_digest", {})
    if not daily_digest.get("enabled", False):
        return False
    if "_01_" not in post_filename:
        return False
    day_label = post_filename[:10]
    digest_state = state.get("days", {}).get(day_label, {}).get("digest", {})
    if not digest_state.get(f"attempted_{env_name}", False):
        return True
    status = digest_state.get(f"status_{env_name}")
    reason = digest_state.get(f"reason_{env_name}")
    if status == "skipped" and reason in {"no_source_posts", "no_source_posts_in_lookback"}:
        return True
    return False


def collect_digest_source_posts(state: dict, day_label: str, env_name: str, max_items: int) -> list[dict]:
    by_file = {}
    for entry in state.get("send_queue", []):
        if entry.get("status") != "sent" or entry.get("env") != env_name:
            continue
        file_name = Path(str(entry.get("file") or "")).name
        if not file_name.startswith(day_label):
            continue
        by_file[file_name] = entry

    items = []
    for file_name in sorted(by_file):
        entry = by_file[file_name]
        file_path = Path(str(entry.get("file") or ""))
        text = ""
        if file_path.exists():
            text = file_path.read_text(encoding="utf-8").strip()
        title = extract_post_title(text, fallback=file_path.stem if file_path.stem else file_name)
        link = build_post_link(entry.get("chat_id"), entry.get("message_id"))
        items.append({
            "file_name": file_name,
            "file_path": str(file_path),
            "title": title,
            "text": text,
            "link": link,
            "message_id": entry.get("message_id"),
        })
    return items[:max_items]


def find_digest_source_day(state: dict, target_day_label: str, env_name: str, lookback_days: int = 7) -> str | None:
    target_date = datetime.strptime(target_day_label, "%Y-%m-%d").date()
    for offset in range(1, max(lookback_days, 1) + 1):
        candidate = (target_date - timedelta(days=offset)).isoformat()
        if collect_digest_source_posts(state, candidate, env_name, 1):
            return candidate
    return None


def extract_digest_summary_fallback(item: dict) -> str:
    text = strip_markdown_for_story(item.get("text") or "")
    lines = [line.strip(" -—\t") for line in text.splitlines() if line.strip()]
    title = normalize_digest_title(item.get("title") or "")
    content_lines = []
    for line in lines[1:]:
        lowered = line.lower()
        if lowered.startswith(("новость:", "выжимка:", "влияние на рынок:", "как это использовать:", "дисклеймер:")):
            continue
        if line.startswith(("📰", "📊", "📉", "💡", "⚠️")):
            line = strip_leading_symbols(line)
        line = re.sub(r"^(новость|выжимка|влияние на рынок|как это использовать|дисклеймер)\s*:\s*", "", line, flags=re.IGNORECASE)
        if line:
            content_lines.append(line)
    summary_body = ""
    if content_lines:
        summary_body = normalize_digest_title(shorten_text(" ".join(content_lines), 120))
    if summary_body and not summary_body.lower().startswith(title.lower()):
        return f"{title}: {summary_body}"
    if summary_body:
        return summary_body
    return title


def extract_json_block(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    return raw.strip()


def parse_digest_summary_json(raw: str, expected_count: int) -> list[str]:
    payload = json.loads(extract_json_block(raw))
    items = payload.get("items", [])
    result = []
    for item in items:
        summary = normalize_digest_title(str(item.get("summary", "")).strip())
        if summary:
            result.append(summary)
    if len(result) != expected_count:
        raise ValueError(f"AI вернул {len(result)} digest summaries вместо {expected_count}")
    return result


def build_digest_summaries(items: list[dict], config: dict, logger) -> tuple[list[str], str]:
    source_lines = []
    for idx, item in enumerate(items, start=1):
        source_lines.append(f"[{idx}] Заголовок: {item['title']}")
        source_lines.append(shorten_text(item.get("text") or item["title"], 1600))
        source_lines.append("")

    prompt = (
        "Ты делаешь короткий утренний дайджест по уже опубликованным постам Telegram-канала. "
        "Для каждого поста верни ОДНУ короткую строку на русском языке: это должна быть нормальная человеческая формулировка сути новости. "
        "Не используй шаблоны вроде 'Новость', 'Что случилось', 'Пост'. "
        "Не пиши вводные слова. Не ставь кавычки. Не добавляй ссылки. "
        "Каждая строка должна быть 5-14 слов, по возможности в одно предложение. "
        "Верни строго JSON вида {\"items\":[{\"summary\":\"...\"}]} в том же порядке."
    )

    try:
        raw = call_openrouter(config, prompt, "\n".join(source_lines), logger, system_prompt=prompt)
        return parse_digest_summary_json(raw, len(items)), "ai"
    except Exception as e:
        logger.warning("AI не смог сделать digest summaries, fallback на локальную выжимку: %s", e)
        return [extract_digest_summary_fallback(item) for item in items], "fallback"


def build_digest_lines(items: list[dict], summaries: list[str]) -> tuple[list[str], list[str]]:
    telegram_lines = []
    plain_lines = []
    for item, summary in zip(items, summaries):
        line_text = normalize_digest_title(summary)
        if item.get("link"):
            telegram_lines.append(f"👉 {inject_link_into_one_word(line_text, item['link'])}")
            plain_lines.append(f"👉 {line_text}\nПодробнее: {item['link']}")
        else:
            telegram_lines.append(f"👉 {escape(line_text)}")
            plain_lines.append(f"👉 {line_text}")
    return telegram_lines, plain_lines


def get_digest_dir(config: dict) -> str:
    paths = get_paths(config)
    return paths.get("digest_dir", "generated_digests")


def save_digest_artifacts(config: dict, target_day_label: str, telegram_message: str, plain_message: str, meta: dict, logger) -> dict:
    digest_dir = Path(get_digest_dir(config))
    ensure_dir(str(digest_dir))
    txt_path = digest_dir / f"{target_day_label}_digest.txt"
    telegram_path = digest_dir / f"{target_day_label}_digest.telegram.html"
    json_path = digest_dir / f"{target_day_label}_digest.json"
    txt_path.write_text(plain_message.strip() + "\n", encoding="utf-8")
    telegram_path.write_text(telegram_message.strip() + "\n", encoding="utf-8")
    payload = dict(meta)
    payload["telegram_message"] = telegram_message
    payload["plain_message"] = plain_message
    save_json(str(json_path), payload)
    logger.info("Digest сохранён: %s", txt_path)
    return {"text_path": str(txt_path), "telegram_path": str(telegram_path), "json_path": str(json_path)}


def prepare_digest_image(config: dict, target_day_label: str, items: list[dict], logger) -> dict | None:
    image_search = config.get("image_search", {})
    if not image_search.get("enabled", False):
        return None
    digest_post = {
        "title": "Digest financial market news",
        "content": "\n".join(normalize_digest_title(item["title"]) for item in items[:6]),
    }
    queries = build_image_queries(digest_post, config, logger)
    image_meta = search_image_metadata(queries, config, logger)
    if not image_meta:
        return None

    local_image_path = None
    if config.get("image_storage", {}).get("enabled", True) and config.get("image_storage", {}).get("download", True):
        digest_media_dir = str(Path(get_digest_dir(config)) / target_day_label)
        local_image_path = download_image(image_meta["image_url"], digest_media_dir, target_day_label, 0, logger)

    return {
        "image_enabled": True,
        "image_query": image_meta.get("image_query"),
        "image_queries": image_meta.get("image_queries", queries),
        "image_url": image_meta.get("image_url"),
        "image_provider": image_meta.get("provider"),
        "image_page_url": image_meta.get("page_url"),
        "image_author": image_meta.get("author"),
        "image_author_id": image_meta.get("author_id"),
        "image_tags": image_meta.get("tags"),
        "image_id": image_meta.get("image_id"),
        "local_image_path": local_image_path,
    }


def build_daily_digest_message(config: dict, state: dict, env_name: str, target_day_label: str, logger) -> tuple[dict | None, dict]:
    daily_digest = config.get("content", {}).get("daily_digest", {})
    source_day_label = find_digest_source_day(
        state,
        target_day_label,
        env_name,
        int(daily_digest.get("lookback_days", 7) or 7),
    )
    if not source_day_label:
        return None, {"source_day_label": None, "items_count": 0, "items": []}

    items = collect_digest_source_posts(state, source_day_label, env_name, int(daily_digest.get("max_items", 10) or 10))
    meta = {"source_day_label": source_day_label, "items_count": len(items), "items": items}
    if not items:
        return None, meta

    intro = daily_digest.get("intro", "").strip() or "Важные финансовые новости, которые вы могли пропустить вчера"
    source_date = datetime.strptime(source_day_label, "%Y-%m-%d").date()
    header = f"{intro}, {format_russian_date(source_date)}:"
    summaries, summary_source = build_digest_summaries(items, config, logger)
    telegram_lines, plain_lines = build_digest_lines(items, summaries)
    telegram_message = f"{escape(header)}\n\n" + "\n\n".join(telegram_lines)
    plain_message = f"{header}\n\n" + "\n\n".join(plain_lines)
    meta["summary_source"] = summary_source
    meta["summaries"] = summaries
    image_meta = prepare_digest_image(config, target_day_label, items, logger)
    if image_meta:
        meta["image"] = image_meta
    payload = {
        "telegram_message": telegram_message[:3900],
        "plain_message": plain_message[:3900],
        "image": image_meta,
    }
    return payload, meta


def mark_digest_result(state: dict, env_name: str, day_label: str, status: str, **extra) -> None:
    digest_state = state.setdefault("days", {}).setdefault(day_label, {}).setdefault("digest", {})
    suffix = f"_{env_name}"
    for key in [item for item in list(digest_state.keys()) if item.endswith(suffix)]:
        digest_state.pop(key, None)
    digest_state[f"attempted_{env_name}"] = True
    digest_state[f"status_{env_name}"] = status
    digest_state[f"updated_at_{env_name}"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    for key, value in extra.items():
        digest_state[f"{key}_{env_name}"] = value


def send_digest_message(bot_token: str, chat_id: str, digest_payload: dict, logger, disable_web_page_preview: bool = False) -> tuple[bool, dict]:
    text = digest_payload["telegram_message"]
    image_meta = digest_payload.get("image") or {}
    details = {"stage": "digest", "chars": len(strip_html_tags(text)), "chat_id": chat_id}
    image_source = None
    if image_meta.get("local_image_path") and Path(image_meta["local_image_path"]).exists():
        image_source = image_meta["local_image_path"]
    elif image_meta.get("image_url"):
        image_source = image_meta["image_url"]
    details["image_source"] = image_source
    details["image_query"] = image_meta.get("image_query")

    try:
        if image_source:
            response = retry_request(
                lambda: send_photo(bot_token, chat_id, image_source, text, "HTML", None),
                logger,
            )
            details["mode"] = "photo"
        else:
            response = retry_request(
                lambda: send_message(bot_token, chat_id, text, "HTML", disable_web_page_preview, None),
                logger,
            )
            details["mode"] = "text"
    except Exception as e:
        details["exception"] = str(e)
        return False, details

    details["status_code"] = response.status_code
    details["response_text"] = shorten_text(response.text, 700)
    if not response.ok:
        return False, details
    details["message_id"] = extract_message_id(response)
    return True, details


def should_publish_vk_digest(config: dict) -> bool:
    targets = {str(item).lower() for item in config.get("publish_targets", [])}
    if "vk" not in targets:
        return False
    vk_cfg = config.get("vk", {})
    group_id = int(vk_cfg.get("group_id") or 0)
    if group_id in {0, 123456789}:
        return False
    return bool(vk_cfg.get("access_token") and group_id)


def vk_api_call(config: dict, method: str, params: dict, logger) -> dict:
    vk_cfg = config.get("vk", {})
    request_params = dict(params)
    request_params["access_token"] = vk_cfg.get("access_token")
    request_params["v"] = vk_cfg.get("api_version", "5.199")

    def do_request():
        return requests.post(f"https://api.vk.com/method/{method}", data=request_params, timeout=120)

    response = retry_request(do_request, logger)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"VK API {method}: {data['error']}")
    return data.get("response", {})


def upload_vk_wall_photo(config: dict, image_path: str, logger) -> str:
    vk_cfg = config.get("vk", {})
    group_id = int(vk_cfg.get("group_id"))
    upload_server = vk_api_call(config, "photos.getWallUploadServer", {"group_id": group_id}, logger)
    upload_url = upload_server.get("upload_url")
    if not upload_url:
        raise RuntimeError("VK не вернул upload_url для wall photo")

    with open(image_path, "rb") as image_file:
        upload_response = retry_request(lambda: requests.post(upload_url, files={"photo": image_file}, timeout=180), logger)
    upload_response.raise_for_status()
    upload_payload = upload_response.json()
    if upload_payload.get("error"):
        raise RuntimeError(f"VK upload error: {upload_payload['error']}")

    saved = vk_api_call(
        config,
        "photos.saveWallPhoto",
        {
            "group_id": group_id,
            "photo": upload_payload.get("photo"),
            "server": upload_payload.get("server"),
            "hash": upload_payload.get("hash"),
        },
        logger,
    )
    if not saved:
        raise RuntimeError("VK не вернул данные сохранённого фото")
    photo = saved[0]
    return f"photo{photo['owner_id']}_{photo['id']}"


def send_digest_to_vk(config: dict, digest_payload: dict, logger) -> tuple[bool, dict]:
    vk_cfg = config.get("vk", {})
    details = {
        "stage": "digest_vk",
        "group_id": vk_cfg.get("group_id"),
        "chars": len(digest_payload.get("plain_message", "")),
    }
    message = digest_payload.get("plain_message", "").strip()
    if not message:
        details["exception"] = "empty_digest_message"
        return False, details

    params = {
        "owner_id": -abs(int(vk_cfg.get("group_id"))),
        "from_group": 1,
        "message": message[:4000],
    }

    image_meta = digest_payload.get("image") or {}
    image_path = image_meta.get("local_image_path")
    details["image_path"] = image_path
    details["image_query"] = image_meta.get("image_query")
    try:
        if image_path and Path(image_path).exists():
            attachment = upload_vk_wall_photo(config, image_path, logger)
            params["attachments"] = attachment
            details["attachment"] = attachment
            details["mode"] = "photo"
        else:
            details["mode"] = "text"
        response = vk_api_call(config, "wall.post", params, logger)
        details["post_id"] = response.get("post_id")
        return True, details
    except Exception as e:
        details["exception"] = str(e)
        return False, details


def try_send_post(bot_token: str, chat_id: str, file_path: Path, logger, config: dict, env_cfg: dict, disable_web_page_preview=False):
    text = file_path.read_text(encoding="utf-8").strip()
    metadata = load_post_metadata(file_path)
    details = {
        "file": str(file_path),
        "file_name": file_path.name,
        "chat_id": chat_id,
        "post_chars": len(text),
        "image_enabled": bool(metadata.get("image_enabled", False)),
        "image_query": metadata.get("image_query"),
        "image_queries": metadata.get("image_queries"),
        "image_skipped_reason": metadata.get("image_skipped_reason"),
        "photo_slot_requested": metadata.get("photo_slot_requested"),
        "photo_slot_ready": metadata.get("photo_slot_ready"),
    }
    if not text:
        details["stage"] = "read_post"
        return False, "empty_file", details

    reply_markup = build_reply_markup(config, env_cfg)
    image_enabled = bool(metadata.get("image_enabled", False))
    local_image_path = metadata.get("local_image_path") if image_enabled else None
    image_url = metadata.get("image_url") if image_enabled else None

    image_source = None
    if local_image_path and Path(local_image_path).exists():
        image_source = local_image_path
    elif image_url:
        image_source = image_url
    details["local_image_path"] = local_image_path
    details["image_url"] = image_url
    details["resolved_image_source"] = image_source

    photo_post_limit = get_photo_post_limit(config)
    if image_source and len(text) > photo_post_limit:
        logger.info(
            "Картинка проигнорирована: текст длиннее лимита photo-поста (%s > %s), пост уйдет текстом",
            len(text),
            photo_post_limit,
        )
        details["image_dropped_reason"] = f"caption_limit:{len(text)}>{photo_post_limit}"
        image_source = None

    logger.info("Пытаюсь отправить пост: %s | image: %s | кнопка: %s", file_path.name, "yes" if image_source else "no", "enabled" if reply_markup else "disabled")

    if image_source:
        caption = text[:TELEGRAM_CAPTION_LIMIT].strip()
        details["stage"] = "send_photo"
        details["caption_chars"] = len(caption)

        def photo_request():
            return send_photo(bot_token, chat_id, image_source, caption, "Markdown", reply_markup)

        try:
            photo_response = retry_request(photo_request, logger)
        except Exception as e:
            logger.warning("sendPhoto не удался: %s", e)
            details["photo_exception"] = str(e)
            photo_response = None

        if photo_response is not None and photo_response.ok:
            message_id = extract_message_id(photo_response)
            details["status_code"] = photo_response.status_code
            details["message_id"] = message_id
            return True, None, {"message_ids": [message_id] if message_id else [], "mode": "photo", "debug": details}

        status = photo_response.status_code if photo_response is not None else 0
        body = photo_response.text if photo_response is not None else "no response"
        details["photo_status_code"] = status
        details["photo_response_text"] = shorten_text(body, 700)
        logger.warning("sendPhoto не сработал, fallback на text. status=%s body=%s", status, body)
        if is_permanent_telegram_error(status, body):
            details["stage"] = "send_photo"
            return False, "permanent_error", details

    parts = split_text_for_telegram(text)
    message_ids = []
    details["text_parts"] = len(parts)
    details["stage"] = "send_text_markdown"
    for part in parts:
        def markdown_request():
            return send_message(bot_token, chat_id, part, "Markdown", disable_web_page_preview, reply_markup)
        try:
            response = retry_request(markdown_request, logger)
        except Exception as e:
            logger.warning("Markdown-отправка не удалась: %s", e)
            details["markdown_exception"] = str(e)
            response = None
        if response is not None and response.ok:
            message_ids.append(extract_message_id(response))
            continue

        status = response.status_code if response is not None else 0
        body = response.text if response is not None else "no response"
        details["markdown_status_code"] = status
        details["markdown_response_text"] = shorten_text(body, 700)
        if is_permanent_telegram_error(status, body):
            return False, "permanent_error", details

        def plain_request():
            return send_message(bot_token, chat_id, part, None, disable_web_page_preview, reply_markup)
        details["stage"] = "send_text_plain"
        try:
            fallback = retry_request(plain_request, logger)
        except Exception as e:
            details["plain_exception"] = str(e)
            return False, "temporary_error", details
        if not fallback.ok:
            details["plain_status_code"] = fallback.status_code
            details["plain_response_text"] = shorten_text(fallback.text, 700)
            if is_permanent_telegram_error(fallback.status_code, fallback.text):
                return False, "permanent_error", details
            return False, "temporary_error", details
        message_ids.append(extract_message_id(fallback))

    details["message_ids"] = [mid for mid in message_ids if mid]
    return True, None, {"message_ids": [mid for mid in message_ids if mid], "mode": "text", "debug": details}


def build_send_alert_body(script_name: str, env_name: str, debug: dict, extra: dict | None = None) -> str:
    context = {
        "script": script_name,
        "server": socket.gethostname(),
        "env": env_name,
    }
    context.update(debug or {})
    if extra:
        context.update(extra)
    return format_context_lines(context)


def strip_markdown_for_story(text: str) -> str:
    text = re.sub(r"[*_`#>\[\]()]+", "", text)
    text = re.sub(r"━━━━━━━━━━━━━━━.*", "", text, flags=re.S)
    text = re.sub(r"⚠️\s*Дисклеймер:.*", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_args():
    parser = argparse.ArgumentParser(description="Отправка одного markdown-поста в Telegram")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--env", choices=["test", "prod"], default=None)
    parser.add_argument("--posts-dir", default=None)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--disable-web-page-preview", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_json(args.config)
    paths = get_paths(config)
    runtime = config.get("runtime", {})
    logger = setup_logging("send_posts", args.log_dir or paths.get("log_dir", "logs"), args.log_level or runtime.get("log_level", "INFO"), "sender.log")
    lock_path = get_lock_path(config, "send_posts", "publish_telegram", "locks/send_posts.lock")

    with LockFile(lock_path):
        try:
            env_name = get_env_name(config, args.env)
            env_cfg = get_env_config(config, args.env)
            bot_token = config.get("bots", {}).get("sender_bot_token")
            chat_id = env_cfg.get("channel_chat_id")

            posts_dir = args.posts_dir or paths.get("md_output_dir", "generated_posts")
            sent_posts_dir = paths.get("sent_posts_dir", "sent_posts")
            failed_posts_dir = paths.get("failed_posts_dir", "failed_posts")
            sent_media_dir = paths.get("sent_media_dir", "sent_media")
            failed_media_dir = paths.get("failed_media_dir", "failed_media")
            state_path = paths.get("state_path", "state.json")
            state = load_state(state_path)

            if not bot_token or not chat_id:
                logger.error("Не заполнены sender_bot_token или environments.%s.channel_chat_id", env_name)
                send_alert(
                    config,
                    "Ошибка конфигурации send_posts.py",
                    build_send_alert_body("send_posts.py", env_name, {"chat_id": chat_id, "sender_bot_token_present": bool(bot_token)}),
                    logger,
                )
                return 1

            posts = load_markdown_files(posts_dir)
            if not posts:
                logger.info("Нет .md файлов для отправки.")
                return 0

            next_post = posts[0]
            logger.info("Env=%s | Найдено постов: %s | к отправке: %s", env_name, len(posts), next_post.name)

            if args.dry_run:
                if should_send_digest(config, env_name, next_post.name, state):
                    logger.info("dry-run: перед первым постом был бы собран и отправлен digest за предыдущий день")
                logger.info("dry-run: пост был бы отправлен: %s", next_post)
                return 0

            if should_send_digest(config, env_name, next_post.name, state):
                day_label = next_post.name[:10]
                digest_payload, digest_meta = build_daily_digest_message(config, state, env_name, day_label, logger)
                if not digest_payload:
                    skip_reason = "no_source_posts" if digest_meta.get("source_day_label") else "no_source_posts_in_lookback"
                    mark_digest_result(state, env_name, day_label, "skipped", source_day=digest_meta.get("source_day_label"), reason=skip_reason)
                    save_state(state_path, state)
                    if digest_meta.get("source_day_label"):
                        logger.info("Digest пропущен: нет отправленных постов за %s", digest_meta.get("source_day_label"))
                    else:
                        logger.info("Digest пропущен: в окне поиска не найдено ни одного предыдущего дня с отправленными постами")
                else:
                    artifact_paths = save_digest_artifacts(
                        config,
                        day_label,
                        digest_payload["telegram_message"],
                        digest_payload["plain_message"],
                        digest_meta,
                        logger,
                    )
                    ok_digest, digest_result = send_digest_message(bot_token, chat_id, digest_payload, logger, args.disable_web_page_preview)
                    if ok_digest:
                        vk_result = None
                        vk_ok = False
                        if should_publish_vk_digest(config):
                            vk_ok, vk_result = send_digest_to_vk(config, digest_payload, logger)
                        mark_digest_result(
                            state,
                            env_name,
                            day_label,
                            "sent",
                            source_day=digest_meta.get("source_day_label"),
                            items_count=digest_meta.get("items_count"),
                            summary_source=digest_meta.get("summary_source"),
                            message_id=digest_result.get("message_id"),
                            text_path=artifact_paths.get("text_path"),
                            telegram_path=artifact_paths.get("telegram_path"),
                            json_path=artifact_paths.get("json_path"),
                            vk_status="sent" if vk_ok else ("failed" if vk_result else "skipped"),
                            vk_post_id=(vk_result or {}).get("post_id") if vk_result else None,
                            vk_error=(vk_result or {}).get("exception") if vk_result and not vk_ok else None,
                        )
                        save_state(state_path, state)
                        logger.info("Digest отправлен перед первым постом | day=%s | source_day=%s", day_label, digest_meta.get("source_day_label"))
                        if vk_result and not vk_ok:
                            send_alert(
                                config,
                                "Ошибка публикации digest в VK",
                                build_send_alert_body("send_posts.py", env_name, vk_result, digest_meta | {"day_label": day_label}),
                                logger,
                            )
                            logger.warning("Digest отправлен в Telegram, но не опубликован в VK | day=%s", day_label)
                    else:
                        mark_digest_result(
                            state,
                            env_name,
                            day_label,
                            "failed",
                            source_day=digest_meta.get("source_day_label"),
                            items_count=digest_meta.get("items_count"),
                            summary_source=digest_meta.get("summary_source"),
                            error=digest_result.get("exception") or digest_result.get("response_text") or "unknown_error",
                            text_path=artifact_paths.get("text_path"),
                            telegram_path=artifact_paths.get("telegram_path"),
                            json_path=artifact_paths.get("json_path"),
                        )
                        save_state(state_path, state)
                        send_alert(
                            config,
                            "Ошибка отправки digest",
                            build_send_alert_body("send_posts.py", env_name, digest_result, digest_meta | {"day_label": day_label}),
                            logger,
                        )
                        logger.warning("Digest не отправлен, но лента продолжит работу | day=%s", day_label)

            ok, error_kind, send_result = try_send_post(bot_token, chat_id, next_post, logger, config, env_cfg, args.disable_web_page_preview)
            queue = state.setdefault("send_queue", [])
            debug = send_result.get("debug", send_result) if isinstance(send_result, dict) else {}

            if ok:
                moved = move_associated_files(next_post, sent_posts_dir, sent_media_dir, logger)
                message_ids = send_result.get("message_ids") or []
                publication = {
                    "env": env_name,
                    "chat_id": chat_id,
                    "message_id": message_ids[0] if message_ids else None,
                    "message_ids": message_ids,
                    "mode": send_result.get("mode"),
                    "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                save_publication_metadata(moved.get("meta"), publication)
                queue.append({"file": moved["md"], "status": "sent", **publication, "debug": debug})
                save_state(state_path, state)
                return 0

            if error_kind == "permanent_error":
                moved = move_associated_files(next_post, failed_posts_dir, failed_media_dir, logger)
                queue.append({"file": moved["md"], "status": "failed_permanent", "env": env_name, "error": error_kind, "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "debug": debug})
                save_state(state_path, state)
                send_alert(
                    config,
                    "Ошибка отправки поста",
                    build_send_alert_body("send_posts.py", env_name, debug, {"error_kind": error_kind}),
                    logger,
                )
                return 1

            queue.append({"file": str(next_post), "status": "retry_later", "env": env_name, "error": error_kind, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "debug": debug})
            save_state(state_path, state)
            send_alert(
                config,
                "Временная ошибка отправки",
                build_send_alert_body("send_posts.py", env_name, debug, {"error_kind": error_kind or "temporary_error"}),
                logger,
            )
            return 1

        except Exception as e:
            logger.exception("Критическая ошибка send_posts.py: %s", e)
            send_alert(
                config,
                "Критическая ошибка send_posts.py",
                build_send_alert_body("send_posts.py", locals().get("env_name", config.get("env", "test")), {"exception": str(e)}),
                logger,
            )
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
