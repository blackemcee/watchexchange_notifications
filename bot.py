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
CHAT_ID = os.getenv("CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

# RSS-лента: можно переопределить через ENV RSS_FEED
RSS_URL = os.getenv(
    "RSS_FEED",
    "https://old.reddit.com/r/Watchexchange/new/.rss",
)

# 0 -> игнорируем KEYWORDS, только tracked users
# 1 -> tracked users + посты, где в заголовке есть KEYWORDS
ENABLE_KEYWORD_FILTER = int(os.getenv("ENABLE_KEYWORD_FILTER", "0"))

# KEYWORDS из ENV: "seiko,omega"
raw_keywords = os.getenv("KEYWORDS", "")

KEYWORDS = set()
for part in raw_keywords.replace(";", ",").split(","):
    kw = part.strip().strip(" '\"").lower()
    if kw:
        KEYWORDS.add(kw)

# TRACKED_USERS из ENV: "ParentalAdvice,AudaciousCo,Vast_Requirement8134"
raw_tracked = os.getenv("TRACKED_USERS", "")

TRACKED_USERS_NORMALIZED = set()
for part in raw_tracked.replace(";", ",").split(","):
    u = part.strip().strip(" '\"").lower()
    if u:
        TRACKED_USERS_NORMALIZED.add(u)

log.info(f"RSS_URL = {RSS_URL}")
log.info(f"Tracked users (normalized): {TRACKED_USERS_NORMALIZED}")
log.info(f"Keyword filter: {ENABLE_KEYWORD_FILTER}, keywords={KEYWORDS}")

bot = Bot(token=TELEGRAM_TOKEN)

# -----------------------------
# SEEN STORAGE (на Volume)
# -----------------------------
SEEN_FILE = "/mnt/data/seen.json"


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
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen), f)
        log.info(f"Saved seen_posts: {len(seen)} items")
    except Exception as e:
        log.error(f"Error saving seen.json: {e}")


seen_posts = load_seen()

# -----------------------------
# HELPERS
# -----------------------------


def fetch_feed(url: str):
    """
    Забираем RSS через requests с нормальным User-Agent,
    чтобы Reddit не отдавал мусор, и логируем статус.
    """
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
    """
    Стабильный ID поста из URL вида:
    https://www.reddit.com/r/test/comments/abc123/title/
    Если не нашли — возвращаем сам линк.
    """
    if not link:
        return ""
    match = re.search(r"/comments/([a-z0-9]+)/", link)
    if match:
        return match.group(1)
    return link.strip()


def normalize_author(raw_author: str) -> str:
    """
    '/u/Vast_Requirement8134' -> 'vast_requirement8134'
    'u/Vast_Requirement8134'  -> 'vast_requirement8134'
    'Vast_Requirement8134'    -> 'vast_requirement8134'
    'Username (u/Username)'   -> вытаскиваем u/Username
    """
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


log.info("Bot started (RSS mode)!")

# -----------------------------
# MAIN LOOP
# -----------------------------
while True:
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

            # Фильтр по ключевым словам в заголовке
            title_matches_keyword = any(kw in title_lower for kw in KEYWORDS)

            author_ok = author_norm in TRACKED_USERS_NORMALIZED
            keyword_ok = ENABLE_KEYWORD_FILTER == 1 and title_matches_keyword

            log.info(
                f"ENTRY post_id={post_id}, raw_author='{raw_author}', "
                f"author_norm='{author_norm}', title='{title}', "
                f"author_ok={author_ok}, keyword_ok={keyword_ok}, "
                f"title_matches_keyword={title_matches_keyword}"
            )

            # защитa от дублей
            if post_id in seen_posts:
                continue

            # Если ни tracked user, ни keyword — пропускаем
            if not (author_ok or keyword_ok):
                continue

            summary = entry.summary
            image_url = extract_first_image_from_html(summary)

            if author_ok and keyword_ok:
                source_label = "tracked user + keyword match"
            elif author_ok:
                source_label = "tracked user"
            else:
                matched = [kw for kw in KEYWORDS if kw in title_lower]
                source_label = f"keyword match: {','.join(matched) or 'unknown'}"

            author_html = escape_html(author_norm or "unknown")
            title_html = escape_html(title)
            source_html = escape_html(source_label)

            message = (
                f"🕵️ New post ({source_html})\n\n"
                f"<b>Author:</b> {author_html}\n\n"
                f"<b>{title_html}</b>\n"
                f'<a href="{link}">Open post</a>'
            )

            if image_url:
                bot.send_photo(
                    chat_id=CHAT_ID,
                    photo=image_url,
                    caption=message,
                    parse_mode="HTML",
                )
            else:
                bot.send_message(
                    chat_id=CHAT_ID,
                    text=message,
                    parse_mode="HTML",
                )

            log.info(
                f"Sent post {post_id} from {author_norm} "
                f"(author_ok={author_ok}, keyword_ok={keyword_ok}, image={'yes' if image_url else 'no'})"
            )

            seen_posts.add(post_id)
            save_seen(seen_posts)

    except Exception as e:
        log.error(f"Error in main loop: {e}")
        time.sleep(10)

    time.sleep(CHECK_INTERVAL)