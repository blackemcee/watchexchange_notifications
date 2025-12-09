import time
import json
import os
import re
import feedparser
import requests
from bs4 import BeautifulSoup
from telegram import Bot
import logging

# -----------------------------
# LOGGING
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("watchbot")

# -----------------------------
# CONFIG - ENV VARS
# -----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Интервал проверки Reddit (секунды)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

# RSS-лента
RSS_URL = os.getenv(
    "RSS_FEED",
    "https://old.reddit.com/r/Watchexchange/new/.rss",
)

# Значения по умолчанию для НОВЫХ пользователей
DEFAULT_KEYWORDS = os.getenv("DEFAULT_KEYWORDS", "seiko")
DEFAULT_TRACKED_USERS = os.getenv(
    "DEFAULT_TRACKED_USERS",
    "ParentalAdvice,AudaciousCo"
)

log.info(f"RSS_URL = {RSS_URL}")
log.info(f"Default keywords: {DEFAULT_KEYWORDS}")
log.info(f"Default tracked users: {DEFAULT_TRACKED_USERS}")

bot = Bot(token=TELEGRAM_TOKEN)

# -----------------------------
# STORAGE (на Volume)
# -----------------------------
DATA_DIR = "/mnt/data"
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")


def ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:
        log.error(f"Error creating data directory: {e}")


def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
            seen = set(data)
            log.info(f"Loaded seen_posts: {len(seen)} items")
            return seen
    except FileNotFoundError:
        log.info("seen.json not found, starting with empty set")
        return set()
    except Exception as e:
        log.error(f"Error loading seen.json: {e}")
        return set()


def save_seen(seen):
    try:
        ensure_data_dir()
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen), f)
        log.info(f"Saved seen_posts: {len(seen)} items")
    except Exception as e:
        log.error(f"Error saving seen.json: {e}")


def load_users():
    """
    users.json формат:
    {
      "123456789": {
          "keywords": ["seiko", "omega"],
          "tracked_users": ["parentaladvice", "audaciousco"]
      },
      ...
    }
    """
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
            # нормализуем авторов в lower
            for chat_id, cfg in data.items():
                cfg["keywords"] = [k.lower() for k in cfg.get("keywords", [])]
                cfg["tracked_users"] = [u.lower() for u in cfg.get("tracked_users", [])]
            log.info(f"Loaded users: {len(data)}")
            return data
    except FileNotFoundError:
        log.info("users.json not found, starting with empty users")
        return {}
    except Exception as e:
        log.error(f"Error loading users.json: {e}")
        return {}


def save_users(users):
    try:
        ensure_data_dir()
        with open(USERS_FILE, "w") as f:
            json.dump(users, f)
        log.info(f"Saved users: {len(users)}")
    except Exception as e:
        log.error(f"Error saving users.json: {e}")


seen_posts = load_seen()
users = load_users()

# -----------------------------
# HELPERS (Reddit / HTML)
# -----------------------------


def fetch_feed(url: str):
    """RSS через requests + нормальный UA."""
    try:
        if not url:
            log.error("RSS_URL is empty!")
            return feedparser.parse("")

        headers = {
            "User-Agent": "WatchExchangeTelegramBot/0.1 (by u/Vast_Requirement8134)"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        log.info(f"RSS HTTP status={resp.status_code}, length={len(resp.text)}")
        resp.raise_for_status()

        feed = feedparser.parse(resp.text)
        if getattr(feed, "bozo", 0):
            log.warning(
                f"Feedparser bozo={feed.bozo}, exception={getattr(feed, 'bozo_exception', None)}"
            )
        return feed
    except Exception as e:
        log.error(f"Error fetching RSS: {e}")
        return feedparser.parse("")


def extract_first_image_from_html(html: str):
    """Берём первую <img> из HTML summary RSS (маленький превьюшный thumbnail)."""
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img")
    if img and img.get("src"):
        src = img["src"].replace("&amp;", "&")
        if src.startswith("//"):
            src = "https:" + src
        return src
    return None


def extract_post_id(link: str) -> str:
    """ID поста из URL /comments/<id>/."""
    if not link:
        return ""
    match = re.search(r"/comments/([a-z0-9]+)/", link)
    if match:
        return match.group(1)
    return link.strip()


def normalize_author(raw_author: str) -> str:
    """Приводим автора к 'vast_requirement8134' формату."""
    if not raw_author:
        return ""

    a = raw_author.strip()

    m = re.search(r"u/([A-Za-z0-9_-]+)", a)
    if m:
        return m.group(1).lower()

    a = a.lower()
    a = a.replace("/u/", "").replace("u/", "").strip()

    return a


def escape_html(text: str) -> str:
    """Экранируем для HTML parse_mode."""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# -----------------------------
# TELEGRAM: обработка команд
# -----------------------------

last_update_id = None


def parse_csv_list(s: str):
    """
    Превращаем строку 'seiko, omega; tudor' -> ['seiko', 'omega', 'tudor']
    """
    parts = s.replace(";", ",").split(",")
    result = []
    for p in parts:
        x = p.strip().strip(" '\"")
        if x:
            result.append(x)
    return result


def handle_command(chat_id: int, text: str):
    """
    Обработка команд:
    /start
    /keywords ...
    /authors ...
    /help
    """
    global users

    chat_id_str = str(chat_id)
    text = text.strip()

    if text.startswith("/start"):
        # регистрируем или обновляем пользователя
        if chat_id_str not in users:
            default_keywords = [k.lower() for k in parse_csv_list(DEFAULT_KEYWORDS)]
            default_authors = [u.lower() for u in parse_csv_list(DEFAULT_TRACKED_USERS)]
            users[chat_id_str] = {
                "keywords": default_keywords,
                "tracked_users": default_authors
            }
            save_users(users)
            bot.send_message(
                chat_id=chat_id,
                text=(
                    "👋 Hi! I've registered you.\n\n"
                    f"Default keywords: {', '.join(users[chat_id_str]['keywords']) or 'none'}\n"
                    f"Default tracked users: {', '.join(users[chat_id_str]['tracked_users']) or 'none'}\n\n"
                    "You can change them with:\n"
                    "/keywords seiko, omega, tudor\n"
                    "/authors ParentalAdvice, AudaciousCo\n"
                    "/settings to see current config."
                )
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text=(
                    "You're already registered.\n"
                    "Use /settings to see your current config."
                )
            )
        return

    if text.startswith("/help"):
        bot.send_message(
            chat_id=chat_id,
            text=(
                "Commands:\n"
                "/start - register or show welcome\n"
                "/keywords seiko, omega - set keywords\n"
                "/authors ParentalAdvice, AudaciousCo - set tracked authors\n"
                "/settings - show current settings"
            )
        )
        return

    if text.startswith("/settings"):
        cfg = users.get(chat_id_str)
        if not cfg:
            bot.send_message(
                chat_id=chat_id,
                text="You are not registered yet. Send /start first."
            )
            return

        kw = ", ".join(cfg.get("keywords", [])) or "none"
        au = ", ".join(cfg.get("tracked_users", [])) or "none"
        bot.send_message(
            chat_id=chat_id,
            text=(
                "📋 Your current settings:\n\n"
                f"Keywords: {kw}\n"
                f"Tracked authors: {au}\n\n"
                "Use /keywords and /authors to change them."
            )
        )
        return

    if text.startswith("/keywords"):
        rest = text[len("/keywords"):].strip()
        if not rest:
            bot.send_message(
                chat_id=chat_id,
                text="Usage: /keywords seiko, omega, tudor"
            )
            return

        kws = [k.lower() for k in parse_csv_list(rest)]
        if chat_id_str not in users:
            users[chat_id_str] = {"keywords": [], "tracked_users": []}
        users[chat_id_str]["keywords"] = kws
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Keywords updated: {', '.join(kws) if kws else 'none'}"
        )
        return

    if text.startswith("/authors"):
        rest = text[len("/authors"):].strip()
        if not rest:
            bot.send_message(
                chat_id=chat_id,
                text="Usage: /authors ParentalAdvice, AudaciousCo"
            )
            return

        auths = [u.lower() for u in parse_csv_list(rest)]
        if chat_id_str not in users:
            users[chat_id_str] = {"keywords": [], "tracked_users": []}
        users[chat_id_str]["tracked_users"] = auths
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Tracked authors updated: {', '.join(auths) if auths else 'none'}"
        )
        return

    # нераспознанная команда
    bot.send_message(
        chat_id=chat_id,
        text="Unknown command. Use /help to see available commands."
    )


def poll_telegram_updates():
    """
    Периодически опрашиваем Telegram, чтобы:
    - регистрировать новых пользователей (/start)
    - обновлять их настройки (/keywords, /authors)
    """
    global last_update_id, users

    try:
        kwargs = {}
        if last_update_id is not None:
            kwargs["offset"] = last_update_id + 1

        updates = bot.get_updates(timeout=5, **kwargs)

        for upd in updates:
            last_update_id = upd.update_id
            if upd.message and upd.message.text:
                chat_id = upd.message.chat.id
                text = upd.message.text
                log.info(f"Got Telegram message from {chat_id}: {text}")
                handle_command(chat_id, text)
    except Exception as e:
        log.error(f"Error polling Telegram updates: {e}")


# -----------------------------
# MAIN LOOP
# -----------------------------
log.info("Multi-user WatchExchange bot started (RSS mode)!")

while True:
    try:
        # 1) сначала обрабатываем команды от пользователей
        poll_telegram_updates()

        # 2) затем проверяем Reddit
        feed = fetch_feed(RSS_URL)
        log.info(f"Fetched feed with {len(feed.entries)} entries")

        for entry in feed.entries:
            link = getattr(entry, "link", "") or ""
            post_id = extract_post_id(link)

            raw_author = entry.get("author", "") or ""
            author_norm = normalize_author(raw_author)

            title = getattr(entry, "title", "") or ""
            title_lower = title.lower()
            summary = entry.summary

            # если пост уже видели — пропускаем для всех
            if post_id in seen_posts:
                continue

            # ищем превьюшку
            image_url = extract_first_image_from_html(summary)

            author_html = escape_html(author_norm or "unknown")
            title_html = escape_html(title)

            # 3) решаем, кому из users слать этот пост
            for chat_id_str, cfg in users.items():
                chat_id = int(chat_id_str)
                user_keywords = cfg.get("keywords", [])
                user_authors = cfg.get("tracked_users", [])

                author_ok = author_norm in user_authors
                keyword_ok = any(kw in title_lower for kw in user_keywords)

                if not (author_ok or keyword_ok):
                    continue

                if author_ok and keyword_ok:
                    source_label = "tracked author + keyword match"
                elif author_ok:
                    source_label = "tracked author"
                else:
                    matched = [kw for kw in user_keywords if kw in title_lower]
                    source_label = f"keyword match: {', '.join(matched) or 'unknown'}"

                source_html = escape_html(source_label)

                message = (
                    f"🕵️ New post ({source_html})\n\n"
                    f"<b>Author:</b> {author_html}\n\n"
                    f"<b>{title_html}</b>\n"
                    f'<a href="{link}">Open post</a>'
                )

                try:
                    if image_url:
                        bot.send_photo(
                            chat_id=chat_id,
                            photo=image_url,
                            caption=message,
                            parse_mode="HTML",
                        )
                    else:
                        bot.send_message(
                            chat_id=chat_id,
                            text=message,
                            parse_mode="HTML",
                        )
                    log.info(
                        f"Sent post {post_id} to {chat_id} "
                        f"(author_ok={author_ok}, keyword_ok={keyword_ok})"
                    )
                except Exception as e:
                    log.error(f"Error sending message to {chat_id}: {e}")

            # отметим пост как увиденный (чтобы второй раз никому не слать)
            seen_posts.add(post_id)
            save_seen(seen_posts)

    except Exception as e:
        log.error(f"Error in main loop: {e}")
        time.sleep(10)

    time.sleep(CHECK_INTERVAL)