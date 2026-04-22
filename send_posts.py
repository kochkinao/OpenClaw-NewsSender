
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
import socket
from subprocess import run

TELEGRAM_HARD_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


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


def try_send_post(bot_token: str, chat_id: str, file_path: Path, logger, config: dict, env_cfg: dict, disable_web_page_preview=False):
    text = file_path.read_text(encoding="utf-8").strip()
    if not text:
        return False, "empty_file"

    reply_markup = build_reply_markup(config, env_cfg)
    metadata = load_post_metadata(file_path)
    local_image_path = metadata.get("local_image_path")
    image_url = metadata.get("image_url")

    image_source = None
    if local_image_path and Path(local_image_path).exists():
        image_source = local_image_path
    elif image_url:
        image_source = image_url

    logger.info("Пытаюсь отправить пост: %s | image: %s | кнопка: %s", file_path.name, "yes" if image_source else "no", "enabled" if reply_markup else "disabled")

    if image_source:
        caption, remainder_parts = split_caption_and_remainder(text)

        def photo_request():
            return send_photo(bot_token, chat_id, image_source, caption, "Markdown", reply_markup)

        try:
            photo_response = retry_request(photo_request, logger)
        except Exception as e:
            logger.warning("sendPhoto не удался: %s", e)
            photo_response = None

        if photo_response is not None and photo_response.ok:
            for part in remainder_parts:
                def msg_req():
                    return send_message(bot_token, chat_id, part, "Markdown", disable_web_page_preview, reply_markup)
                try:
                    response = retry_request(msg_req, logger)
                    if response.ok:
                        continue
                except Exception as e:
                    logger.warning("Дополнительная часть после photo не ушла в Markdown: %s", e)

                def plain_req():
                    return send_message(bot_token, chat_id, part, None, disable_web_page_preview, reply_markup)
                try:
                    fallback = retry_request(plain_req, logger)
                except Exception as e:
                    return False, f"temporary_error: {e}"
                if not fallback.ok:
                    if is_permanent_telegram_error(fallback.status_code, fallback.text):
                        return False, f"permanent_error: {fallback.text[:300]}"
                    return False, f"temporary_error: HTTP {fallback.status_code}: {fallback.text[:300]}"
            return True, None

        status = photo_response.status_code if photo_response is not None else 0
        body = photo_response.text if photo_response is not None else "no response"
        logger.warning("sendPhoto не сработал, fallback на text. status=%s body=%s", status, body)
        if is_permanent_telegram_error(status, body):
            return False, f"permanent_error: {body[:300]}"

    parts = split_text_for_telegram(text)
    for part in parts:
        def markdown_request():
            return send_message(bot_token, chat_id, part, "Markdown", disable_web_page_preview, reply_markup)
        try:
            response = retry_request(markdown_request, logger)
        except Exception as e:
            logger.warning("Markdown-отправка не удалась: %s", e)
            response = None
        if response is not None and response.ok:
            continue

        status = response.status_code if response is not None else 0
        body = response.text if response is not None else "no response"
        if is_permanent_telegram_error(status, body):
            return False, f"permanent_error: {body[:300]}"

        def plain_request():
            return send_message(bot_token, chat_id, part, None, disable_web_page_preview, reply_markup)
        try:
            fallback = retry_request(plain_request, logger)
        except Exception as e:
            return False, f"temporary_error: {e}"
        if not fallback.ok:
            if is_permanent_telegram_error(fallback.status_code, fallback.text):
                return False, f"permanent_error: {fallback.text[:300]}"
            return False, f"temporary_error: HTTP {fallback.status_code}: {fallback.text[:300]}"

    return True, None


def should_send_story(config: dict, env_name: str, post_filename: str, state: dict) -> bool:
    stories = config.get("content", {}).get("stories", {})
    if not stories.get("enabled", False):
        return False
    if not stories.get("use_first_post_only", True):
        return True
    day_label = post_filename[:10]
    if "_01_" not in post_filename:
        return False
    day_state = state.get("days", {}).get(day_label, {})
    story_state = day_state.get("story", {})
    return not story_state.get(f"sent_{env_name}", False)


def trigger_story(config_path: str, post_file: str, env_name: str, logger):
    story_script = str(Path(config_path).with_name("send_story.py"))
    cmd = [sys.executable, story_script, "--config", config_path, "--post-file", post_file, "--env", env_name]
    logger.info("Запускаю story trigger: %s", " ".join(cmd))
    result = run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        logger.info("Story trigger выполнен успешно: %s", result.stdout.strip())
    else:
        logger.error("Story trigger завершился с ошибкой: %s | %s", result.stdout, result.stderr)


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
    lock_path = config.get("locks", {}).get("send_posts", "locks/send_posts.lock")

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

            if not bot_token or not chat_id:
                logger.error("Не заполнены sender_bot_token или environments.%s.channel_chat_id", env_name)
                return 1

            posts = load_markdown_files(posts_dir)
            if not posts:
                logger.info("Нет .md файлов для отправки.")
                return 0

            next_post = posts[0]
            logger.info("Env=%s | Найдено постов: %s | к отправке: %s", env_name, len(posts), next_post.name)

            if args.dry_run:
                logger.info("dry-run: пост был бы отправлен: %s", next_post)
                return 0

            ok, error_kind = try_send_post(bot_token, chat_id, next_post, logger, config, env_cfg, args.disable_web_page_preview)
            state = load_state(state_path)
            queue = state.setdefault("send_queue", [])

            if ok:
                moved = move_associated_files(next_post, sent_posts_dir, sent_media_dir, logger)
                queue.append({"file": moved["md"], "status": "sent", "env": env_name, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                save_state(state_path, state)

                if should_send_story(config, env_name, Path(moved["md"]).name, state):
                    trigger_story(args.config, moved["md"], env_name, logger)
                return 0

            if error_kind and error_kind.startswith("permanent_error"):
                moved = move_associated_files(next_post, failed_posts_dir, failed_media_dir, logger)
                queue.append({"file": moved["md"], "status": "failed_permanent", "env": env_name, "error": error_kind, "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                save_state(state_path, state)
                send_alert(config, "Ошибка отправки поста", f"Скрипт: send_posts.py\nСервер: {socket.gethostname()}\nEnv: {env_name}\nФайл: {next_post.name}\nОшибка: {error_kind}", logger)
                return 1

            queue.append({"file": str(next_post), "status": "retry_later", "env": env_name, "error": error_kind, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            save_state(state_path, state)
            send_alert(config, "Временная ошибка отправки", f"Скрипт: send_posts.py\nСервер: {socket.gethostname()}\nEnv: {env_name}\nФайл: {next_post.name}\nОшибка: {error_kind}", logger)
            return 1

        except Exception as e:
            logger.exception("Критическая ошибка send_posts.py: %s", e)
            send_alert(config, "Критическая ошибка send_posts.py", f"Ошибка: {e}", logger)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
