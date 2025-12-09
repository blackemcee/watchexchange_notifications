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
# TELEGRAM
# -----------------------------

last_update_id = None


def handle_text_message(chat_id: int, text: str):
    """
    Обработка текстовых сообщений:
    - команды: /start, /help, /settings, /keywords, /authors
    - режим ожидания: ввод keywords/authors после явного запроса
      (мы оставим режим, но без кнопок — можно будет использовать дальше, если захочешь)
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

        welcome_message = (
            "👋 <b>Welcome!</b>\n\n"
            "This bot sends you alerts about new posts from Reddit subreddits such as r/Watchexchange.\n"
            "You choose what you want to follow using two independent filters.\n\n"

            "━━━━━━━━━━━━━━━━━━\n"
            "🔍 <b>HOW FILTERS WORK</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "You will receive a post if <b>ANY</b> of these is true:\n\n"
            "1️⃣ The author is in your tracked authors list\n"
            "2️⃣ The title contains one of your keywords\n\n"
            "These filters work independently (logical OR):\n\n"
            "• Only authors set → you get all posts from them\n"
            "• Only keywords set → you get all posts containing them\n"
            "• Both set → you get everything matching either filter\n"
            "• Both empty → you receive nothing\n\n"

            "━━━━━━━━━━━━━━━━━━\n"
            "⚙️ <b>SETTING YOUR FILTERS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "📌 Set or replace keywords:\n"
            "<code>/keywords seiko, omega, tudor</code>\n\n"
            "🗑️ Clear all keywords:\n"
            "<code>/keywords clear</code>\n\n"
            "👤 Set tracked authors:\n"
            "<code>/authors WatchTrader247, DealsAreLife, TimepieceWizard</code>\n\n"
            "🗑️ Clear tracked authors:\n"
            "<code>/authors clear</code>\n\n"
            "📋 View your current settings:\n"
            "<code>/settings</code>\n\n"

            "━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>TIPS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "• Keywords are case-insensitive\n"
            "• Add as many keywords or authors as you want\n"
            "• Bot checks Reddit every 1–2 minutes\n"
            "• You can use only keywords, only authors, or both\n"
            "• If no alerts arrive, check your settings with /settings\n\n"

            "━━━━━━━━━━━━━━━━━━\n"
            "☕ <b>APPRECIATE THE BOT?</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "This bot is free to use and has no ads.\n"
            "If it helps you save time or catch good offers,\n"
            "you can support the project by buying me a coffee:\n\n"
            "👉 https://www.buymeacoffee.com/blackemcee\n\n"
            "Totally optional — but very appreciated ❤️\n\n"

            "━━━━━━━━━━━━━━━━━━\n"
            "✓ You're all set!\n"
            "Use <code>/help</code> anytime to see available commands."
        )

        bot.send_message(
            chat_id=chat_id,
            text=welcome_message,
            parse_mode="HTML"
        )
        return

    if text.startswith("/help"):
        bot.send_message(
            chat_id=chat_id,
            text=(
                "Commands:\n"
                "/start    — register or show welcome\n"
                "/keywords seiko, omega, tudor — set keywords\n"
                "/keywords clear              — remove all keywords\n"
                "/authors ParentalAdvice, AudaciousCo — set tracked authors\n"
                "/authors clear                        — remove all authors\n"
                "/settings — show your current settings"
            )
        )
        user_cfg["mode"] = None
        save_users(users)
        return

    if text.startswith("/settings"):
        kw = ", ".join(user_cfg.get("keywords", [])) or "none"
        au = ", ".join(user_cfg.get("tracked_users", [])) or "none"
        bot.send_message(
            chat_id=chat_id,
            text=(
                "📋 Your current settings:\n\n"
                f"Keywords: {kw}\n"
                f"Tracked authors: {au}\n\n"
                "Use /keywords and /authors to modify them."
            )
        )
        user_cfg["mode"] = None
        save_users(users)
        return

    if text.startswith("/keywords"):
        rest = text[len("/keywords"):].strip()
        # /keywords clear
        if rest.lower() == "clear":
            user_cfg["keywords"] = []
            user_cfg["mode"] = None
            save_users(users)
            bot.send_message(
                chat_id=chat_id,
                text="🗑️ All keywords removed."
            )
            return

        if not rest:
            bot.send_message(
                chat_id=chat_id,
                text=(
                    "Usage:\n"
                    "/keywords seiko, omega, tudor\n"
                    "/keywords clear"
                )
            )
            return

        kws = [k.lower() for k in parse_csv_list(rest)]
        user_cfg["keywords"] = kws
        user_cfg["mode"] = None
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Keywords updated: {', '.join(kws) if kws else 'none'}"
        )
        return

    if text.startswith("/authors"):
        rest = text[len("/authors"):].strip()
        # /authors clear
        if rest.lower() == "clear":
            user_cfg["tracked_users"] = []
            user_cfg["mode"] = None
            save_users(users)
            bot.send_message(
                chat_id=chat_id,
                text="🗑️ All tracked authors removed."
            )
            return

        if not rest:
            bot.send_message(
                chat_id=chat_id,
                text=(
                    "Usage:\n"
                    "/authors ParentalAdvice, AudaciousCo\n"
                    "/authors clear"
                )
            )
            return

        auths = [u.lower() for u in parse_csv_list(rest)]
        user_cfg["tracked_users"] = auths
        user_cfg["mode"] = None
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Tracked authors updated: {', '.join(auths) if auths else 'none'}"
        )
        return

    # ----- режимы (если вдруг решишь использовать дальше) -----
    if mode == "await_keywords":
        kws = [k.lower() for k in parse_csv_list(text)]
        user_cfg["keywords"] = kws
        user_cfg["mode"] = None
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Keywords updated: {', '.join(kws) if kws else 'none'}"
        )
        return

    if mode == "await_authors":
        auths = [u.lower() for u in parse_csv_list(text)]
        user_cfg["tracked_users"] = auths
        user_cfg["mode"] = None
        save_users(users)
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ Tracked authors updated: {', '.join(auths) if auths else 'none'}"
        )
        return

    # ----- всё остальное -----
    bot.send_message(
        chat_id=chat_id,
        text="I didn't understand that. Use /help to see available commands."
    )


def poll_telegram_updates():
    """
    Периодически опрашиваем Telegram, чтобы:
    - регистрировать новых пользователей (/start)
    - обновлять их настройки (/keywords, /authors)
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

    # 1) быстро обрабатываем команды/сообщения
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