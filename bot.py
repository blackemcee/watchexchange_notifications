import time
import json
import os
import re
import feedparser
import requests
from bs4 import BeautifulSoup
from telegram import Bot, ReplyKeyboardMarkup
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
CHECK_INTERVAL_RSS = int(os.getenv("CHECK_INTERVAL", "60"))

# Как часто опрашивать Telegram (секунды)
TELEGRAM_POLL_INTERVAL = float(os.getenv("TELEGRAM_POLL_INTERVAL", "2"))

# RSS-лента
RSS_URL = os.getenv(
    "RSS_FEED",
    "https://old.reddit.com/r/Watchexchange/new/.rss",
)

log.info(f"RSS_URL = {RSS_URL}")
log.info(f"CHECK_INTERVAL_RSS = {CHECK_INTERVAL_RSS}")
log.info(f"TELEGRAM_POLL_INTERVAL = {TELEGRAM_POLL_INTERVAL}")

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
          "tracked_users": ["parentaladvice", "audaciousco"],
          "mode": null | "await_keywords" | "await_authors"
      },
      ...
    }
    """
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
            # нормализуем
            for chat_id, cfg in data.items():
                cfg["keywords"] = [k.lower() for k in cfg.get("keywords", [])]
                cfg["tracked_users"] = [u.lower() for u in cfg.get("tracked_users", [])]
                cfg["mode"] = cfg.get("mode")
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


# -----------------------------
# TELEGRAM UI
# -----------------------------

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["➕ Keywords", "➕ Authors"],
            ["📋 Settings"],
        ],
        resize_keyboard=True
    )


last_update_id = None


def handle_text_message(chat_id: int, text: str):
    """
    Обработка ВСЕХ текстовых сообщений:
    - команды (/start, /help, /keywords, /authors, /settings)
    - нажатия кнопок (➕ Keywords / ➕ Authors / 📋 Settings)
    - ввод значений в "режиме ожидания" (mode)
    """
    global users
    chat_id_str = str(chat_id)
    text = text.strip()

    # гарантируем, что user-структура есть
    if chat_id_str not in users:
        users[chat_id_str] = {
            "keywords": [],
            "tracked_users": [],
            "mode": None,
        }

    user_cfg = users[chat_id_str]
    mode = user_cfg.get("mode")

    # ----- команды -----
    if text.startswith("/start"):
        user_cfg.setdefault("keywords", [])
        user_cfg.setdefault("tracked_users", [])
        user_cfg["mode"] = None
        save_users(users)

        kw = ", ".join(user_cfg["keywords"]) or "none"
        au = ", ".join(user_cfg["tracked_users"]) or "none"

        bot.send_message(
            chat_id=chat_id,
            text=(
                "👋 Hi! I've registered you.\n\n"
                f"Keywords: {kw}\n"
                f"Tracked authors: {au}\n\n"
                "Use the buttons below or commands:\n"
                "/keywords seiko, omega\n"
                "/authors ParentalAdvice, AudaciousCo\n"
                "/settings - show current settings."
            ),
            reply_markup=main_keyboard()
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
                "/settings - show your current settings\n\n"
                "Or use the buttons below."
            ),
            reply_markup=main_keyboard()
        )
        return

    if text.startswith("/settings") or text == "📋 Settings":
        kw = ", ".join(user_cfg.get("keywords", [])) or "none"
        au = ", ".join(user_cfg.get("tracked_users", [])) or "none"
        bot.send_message(
            chat_id=chat_id,
            text=(
                "📋 Your current settings:\n\n"
                f"Keywords: {kw}\n"
                f"Tracked authors: {au}\n\n"
                "Use ➕ Keywords / ➕ Authors to update them."
            ),
            reply_markup=main_keyboard()
        )
        user_cfg["mode"] = None
        save_users(users)
        return

    if text.startswith("/keywords"):
        rest = text[len("/keywords"):].strip()
        if not rest:
            bot.send_message(
                chat_id=chat_id,
                text="Usage: /keywords seiko, omega, tudor",
                reply_markup=main_keyboard()
            )
            return

        kws = [k.lower() for k in parse_csv_list(rest)]
        user_cfg["keywords"] = kws
        user_cfg["mode"] = None
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Keywords updated: {', '.join(kws) if kws else 'none'}",
            reply_markup=main_keyboard()
        )
        return

    if text.startswith("/authors"):
        rest = text[len("/authors"):].strip()
        if not rest:
            bot.send_message(
                chat_id=chat_id,
                text="Usage: /authors ParentalAdvice, AudaciousCo",
                reply_markup=main_keyboard()
            )
            return

        auths = [u.lower() for u in parse_csv_list(rest)]
        user_cfg["tracked_users"] = auths
        user_cfg["mode"] = None
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Tracked authors updated: {', '.join(auths) if auths else 'none'}",
            reply_markup=main_keyboard()
        )
        return

    # ----- кнопки -----
    if text == "➕ Keywords":
        user_cfg["mode"] = "await_keywords"
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=(
                "✍️ Send a list of keywords separated by commas.\n"
                "Example:\n"
                "`seiko, grand seiko, omega`"
            ),
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        return

    if text == "➕ Authors":
        user_cfg["mode"] = "await_authors"
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=(
                "✍️ Send a list of Reddit usernames separated by commas.\n"
                "Example:\n"
                "`ParentalAdvice, AudaciousCo`"
            ),
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        return

    # ----- режим ожидания ввода -----
    if mode == "await_keywords":
        kws = [k.lower() for k in parse_csv_list(text)]
        user_cfg["keywords"] = kws
        user_cfg["mode"] = None
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Keywords updated: {', '.join(kws) if kws else 'none'}",
            reply_markup=main_keyboard()
        )
        return

    if mode == "await_authors":
        auths = [u.lower() for u in parse_csv_list(text)]
        user_cfg["tracked_users"] = auths
        user_cfg["mode"] = None
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Tracked authors updated: {', '.join(auths) if auths else 'none'}",
            reply_markup=main_keyboard()
        )
        return

    # ----- если это не команда, не кнопка и не режим -----
    bot.send_message(
        chat_id=chat_id,
        text="I didn't understand that. Use /help or the buttons below.",
        reply_markup=main_keyboard()
    )


def poll_telegram_updates():
    """
    Периодически опрашиваем Telegram, чтобы:
    - регистрировать новых пользователей (/start)
    - обновлять их настройки (/keywords, /authors, кнопки)
    """
    global last_update_id

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
                handle_text_message(chat_id, text)
    except Exception as e:
        log.error(f"Error polling Telegram updates: {e}")


# -----------------------------
# MAIN LOOP
# -----------------------------
log.info("Multi-user WatchExchange bot started (RSS mode)!")

last_rss_check = 0

while True:
    now = time.time()

    # 1) быстро обрабатываем команды/кнопки
    poll_telegram_updates()

    # 2) раз в CHECK_INTERVAL_RSS дергаем Reddit
    if now - last_rss_check >= CHECK_INTERVAL_RSS:
        try:
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

                image_url = extract_first_image_from_html(summary)

                author_html = escape_html(author_norm or "unknown")
                title_html = escape_html(title)

                # решаем, кому слать
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

                seen_posts.add(post_id)
                save_seen(seen_posts)

            last_rss_check = now

        except Exception as e:
            log.error(f"Error in RSS loop: {e}")

    # 3) небольшой sleep, чтобы не жечь CPU
    time.sleep(TELEGRAM_POLL_INTERVAL)