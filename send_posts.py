#!/usr/bin/env python3
import argparse
import json
import logging
import os
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

TELEGRAM_HARD_LIMIT = 4096


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    ensure_dir(str(Path(path).parent))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def setup_logging(log_dir: str = "logs", log_level: str = "INFO"):
    ensure_dir(log_dir)
    logger = logging.getLogger("send_posts")
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "sender.log"),
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
    chat_ids = alerts.get("chat_ids") or [alerts.get("chat_id")]

    if not bot_token or not chat_ids:
        logger.warning("alerts.enabled=true, но bot_token/chat_id(s) не заполнены")
        return

    text = f"⚠️ {title}\n\n{body}"[:4000]

    for chat_id in chat_ids:
        if not chat_id:
            continue

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


def build_reply_markup(config: dict):
    cta = config.get("sender_cta", {})
    enabled = cta.get("enabled", False)
    text = cta.get("text", "").strip()
    url = cta.get("url", "").strip()

    if not enabled or not text or not url:
        return None

    return {
        "inline_keyboard": [
            [
                {
                    "text": text,
                    "url": url,
                }
            ]
        ]
    }


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode=None,
    disable_web_page_preview=False,
    reply_markup=None,
):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }

    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    return requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload,
        timeout=60,
    )


def is_permanent_telegram_error(status_code: int, response_text: str) -> bool:
    lowered = response_text.lower()
    markers = [
        "chat not found",
        "bot is not a member",
        "bot is not an administrator",
        "forbidden",
        "unauthorized",
        "invalid token",
    ]
    return status_code in {400, 401, 403} and any(marker in lowered for marker in markers)


def move_file(src: Path, dst_dir: str, logger) -> str:
    ensure_dir(dst_dir)
    dst = Path(dst_dir) / src.name
    if dst.exists():
        dst = Path(dst_dir) / f"{src.stem}_{time.strftime('%H%M%S')}{src.suffix}"
    src.replace(dst)
    logger.info("Файл перемещён: %s -> %s", src, dst)
    return str(dst)


def try_send_post(
    bot_token: str,
    chat_id: str,
    file_path: Path,
    logger,
    config: dict,
    disable_web_page_preview=False,
):
    text = file_path.read_text(encoding="utf-8").strip()
    if not text:
        return False, "empty_file"

    parts = split_text_for_telegram(text)
    reply_markup = build_reply_markup(config)

    logger.info(
        "Пытаюсь отправить пост: %s | частей: %s | кнопка: %s",
        file_path.name,
        len(parts),
        "enabled" if reply_markup else "disabled",
    )

    for part in parts:
        def markdown_request():
            return send_message(
                bot_token,
                chat_id,
                part,
                "Markdown",
                disable_web_page_preview,
                reply_markup,
            )

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
            return send_message(
                bot_token,
                chat_id,
                part,
                None,
                disable_web_page_preview,
                reply_markup,
            )

        try:
            fallback = retry_request(plain_request, logger)
        except Exception as e:
            return False, f"temporary_error: {e}"

        if not fallback.ok:
            if is_permanent_telegram_error(fallback.status_code, fallback.text):
                return False, f"permanent_error: {fallback.text[:300]}"
            return False, f"temporary_error: HTTP {fallback.status_code}: {fallback.text[:300]}"

    return True, None


def parse_args():
    parser = argparse.ArgumentParser(description="Отправка одного markdown-поста в Telegram")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--posts-dir", default=None)
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--disable-web-page-preview", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_json(args.config)
    logger = setup_logging(args.log_dir, args.log_level)
    lock_path = config.get("locks", {}).get("send_posts", "locks/send_posts.lock")

    with LockFile(lock_path):
        try:
            sender = config.get("sender", {})
            bot_token = sender.get("bot_token")
            chat_id = sender.get("chat_id")
            posts_dir = args.posts_dir or config.get("md_output_dir", "generated_posts")
            sent_dir = config.get("sent_posts_dir", "sent_posts")
            failed_dir = config.get("failed_posts_dir", "failed_posts")
            state_path = config.get("state_path", "state.json")

            if not bot_token or not chat_id:
                logger.error("В config.json отсутствует sender.bot_token или sender.chat_id")
                return 1

            posts = load_markdown_files(posts_dir)
            if not posts:
                logger.info("Нет .md файлов для отправки. Завершаю работу.")
                return 0

            next_post = posts[0]
            logger.info("Найдено постов: %s | к отправке: %s", len(posts), next_post.name)

            if args.dry_run:
                logger.info("dry-run: пост был бы отправлен: %s", next_post)
                return 0

            ok, error_kind = try_send_post(
                bot_token,
                chat_id,
                next_post,
                logger,
                config,
                args.disable_web_page_preview,
            )

            state = load_state(state_path)
            queue = state.setdefault("send_queue", [])

            if ok:
                new_path = move_file(next_post, sent_dir, logger)
                queue.append({
                    "file": new_path,
                    "status": "sent",
                    "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                save_state(state_path, state)
                return 0

            if error_kind and error_kind.startswith("permanent_error"):
                new_path = move_file(next_post, failed_dir, logger)
                queue.append({
                    "file": new_path,
                    "status": "failed_permanent",
                    "error": error_kind,
                    "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                save_state(state_path, state)
                send_alert(
                    config,
                    "Ошибка отправки поста",
                    f"Скрипт: send_posts.py\nСервер: {socket.gethostname()}\nФайл: {next_post.name}\nОшибка: {error_kind}",
                    logger,
                )
                return 1

            queue.append({
                "file": str(next_post),
                "status": "retry_later",
                "error": error_kind,
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            save_state(state_path, state)
            send_alert(
                config,
                "Временная ошибка отправки",
                f"Скрипт: send_posts.py\nСервер: {socket.gethostname()}\nФайл: {next_post.name}\nОшибка: {error_kind}",
                logger,
            )
            return 1

        except Exception as e:
            logger.exception("Критическая ошибка send_posts.py: %s", e)
            send_alert(
                config,
                "Критическая ошибка send_posts.py",
                f"Скрипт: send_posts.py\nСервер: {socket.gethostname()}\nОшибка: {e}",
                logger,
            )
            return 1


if __name__ == "__main__":
    raise SystemExit(main())