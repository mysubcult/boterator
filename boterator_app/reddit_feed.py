"""Reddit publisher. Separate process; reuses Boterator's image and PostgreSQL.

No Telegram polling, no AI calls, no writes to moderation tables.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InputMediaPhoto, LinkPreviewOptions

from .db import create_pool

LOG = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS reddit_feed_state (
    feed TEXT PRIMARY KEY, initialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS reddit_feed_posts (
    feed TEXT NOT NULL, post_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    status TEXT NOT NULL, operations JSONB NOT NULL DEFAULT '[]',
    next_operation INTEGER NOT NULL DEFAULT 0, retry_at TIMESTAMPTZ, message_ids JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (feed, post_id)
);
ALTER TABLE reddit_feed_state ADD COLUMN IF NOT EXISTS source_key TEXT;
CREATE INDEX IF NOT EXISTS reddit_feed_fingerprint ON reddit_feed_posts(feed, fingerprint);
"""


class SourceError(Exception):
    """Safe-to-log error, deliberately excludes URLs/response bodies with secrets."""


@dataclass(frozen=True)
class Post:
    id: str
    title: str
    url: str
    permalink: str
    photos: tuple[str, ...] = ()
    body: str = ""
    complete: bool = True
    gallery: bool = False

    @property
    def fingerprint(self):
        text = re.sub(r"\s+", " ", self.title.casefold()).strip()
        return hashlib.sha256(text.encode()).hexdigest()


def safe_url(value: str) -> str:
    value = html.unescape(value or "")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SourceError("Unsupported source URL")
    return value


def photo_url(value: str) -> str:
    value = safe_url(value)
    if urlsplit(value).scheme != "https" or urlsplit(value).hostname not in {"i.redd.it", "preview.redd.it", "i.imgur.com"}:
        raise SourceError("Unsupported image host; full post retained for review")
    return value


def from_json(data: dict) -> Post | None:
    if data.get("stickied") or data.get("over_18") or data.get("removed_by_category") or data.get("is_meta"):
        return None
    photos = []
    notes = []
    complete = True
    gallery = bool(data.get("is_gallery") or data.get("gallery_data"))
    if gallery:
        items = data.get("gallery_data", {}).get("items", [])
        complete = bool(items)
        for number, item in enumerate(items, 1):
            note = html.unescape(item.get("caption") or "")
            if item.get("outbound_url"):
                note += "\n" + safe_url(item["outbound_url"])
            if note.strip():
                notes.append(f"{number}. {note.strip()}")
            media = data.get("media_metadata", {}).get(item.get("media_id"), {})
            if media.get("status") != "valid" or media.get("e") != "Image" or not media.get("s", {}).get("u"):
                complete = False
                continue
            try:
                photos.append(photo_url(media["s"]["u"]))
            except SourceError:
                complete = False
    else:
        url = html.unescape(data.get("url_overridden_by_dest") or data.get("url") or "")
        if re.search(r"\.(jpe?g|png|webp)(?:\?|$)", url, re.I):
            try:
                photos.append(photo_url(url))
            except SourceError:
                complete = False
    return Post(
        id=data["name"], title=html.unescape(data["title"]),
        url=safe_url(data.get("url_overridden_by_dest") or data["url"]),
        permalink=safe_url("https://www.reddit.com" + data["permalink"]),
        photos=tuple(photos), body="\n\n".join(filter(None, [html.unescape(data.get("selftext") or ""), *notes])),
        complete=complete, gallery=gallery,
    )


class FeedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.href = None
        self.link = None
        self.body = []
        self.in_body = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.href = attrs.get("href")
        if tag == "div" and attrs.get("class") == "md":
            self.in_body = True
        if self.in_body and tag in {"p", "br"}:
            self.body.append("\n")

    def handle_endtag(self, tag):
        if tag == "a":
            self.href = None
        if tag == "div":
            self.in_body = False

    def handle_data(self, value):
        if value.strip() == "[link]" and self.href:
            self.link = self.href
        if self.in_body:
            self.body.append(value)


def from_rss(raw: bytes) -> list[Post]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise SourceError("Reddit returned non-Atom content") from None
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise SourceError("Reddit returned non-Atom content")
    posts = []
    for entry in root.findall("a:entry", ns):
        parser = FeedHTML()
        parser.feed(entry.findtext("a:content", "", ns))
        link = entry.find("a:link", ns)
        if link is None:
            continue
        permalink = safe_url(link.attrib["href"])
        url = safe_url(parser.link or permalink)
        # RSS may contain only a gallery thumbnail. Never publish it as a complete gallery.
        gallery = "/gallery/" in url
        photos = ()
        if not gallery and re.search(r"\.(jpe?g|png|webp)(?:\?|$)", url, re.I):
            photos = (photo_url(url),)
        posts.append(Post(entry.findtext("a:id", "", ns), entry.findtext("a:title", "", ns),
                          url, permalink, photos, "".join(parser.body).strip(), not gallery, gallery))
    return posts


def utf16len(value):
    return len(value.encode("utf-16-le")) // 2


def split_text(value: str, limit=4000) -> list[str]:
    """Split without removing even whitespace or cutting a Unicode code point."""
    chunks = []
    while value:
        size = 0
        end = 0
        for char in value:
            width = utf16len(char)
            if size + width > limit:
                break
            end += 1
            size += width
        if end < len(value):
            boundary = max(value.rfind("\n", 0, end), value.rfind(" ", 0, end))
            if boundary > end // 2:
                end = boundary + 1
        chunks.append(value[:end])
        value = value[end:]
    return chunks


def plan(post: Post, style: str) -> list[dict]:
    text = post.title
    if post.body and post.body not in {"[removed]", "[deleted]"}:
        text += "\n\n" + post.body
    if style == "thought":
        return [{"method": "send_message", "text": html.escape(t), "preview": False} for t in split_text(text)]
    if style == "source":
        chunks = split_text(text)
        source = f'[<a href="{html.escape(post.url, quote=True)}">Source</a>]'
        ops = [{"method": "send_message", "text": html.escape(t), "preview": False} for t in chunks]
        if utf16len(chunks[-1]) + 9 <= 4096:
            ops[-1]["text"] += " " + source
            ops[-1]["preview"] = True
            ops[-1]["preview_url"] = post.url
        else:
            ops.append({"method": "send_message", "text": source, "preview": True, "preview_url": post.url})
        return ops
    if style != "photos":
        raise ValueError("Unknown feed style")
    if not post.complete or not post.photos:
        raise SourceError("Full photo set unavailable; publication held")
    ops = []
    caption = html.escape(text) if utf16len(text) <= 1024 else None
    for offset in range(0, len(post.photos), 10):
        group = post.photos[offset:offset + 10]
        cap = caption if offset == 0 else None
        if len(group) == 1:
            ops.append({"method": "send_photo", "photo": group[0], "caption": cap})
        else:
            media = [{"type": "photo", "media": url} for url in group]
            if cap:
                media[0].update(caption=cap, parse_mode="HTML")
            ops.append({"method": "send_media_group", "media": media})
    if not caption:
        ops.extend({"method": "send_message", "text": html.escape(t), "preview": False} for t in split_text(text))
    return ops


class Reddit:
    def __init__(self, session):
        self.session = session
        self.token = None
        self.blocked_until = {}
        self.expires = 0
        self.client_id = os.getenv("REDDIT_CLIENT_ID")
        self.secret = os.getenv("REDDIT_CLIENT_SECRET")
        self.headers = {"User-Agent": os.getenv("REDDIT_USER_AGENT", "linux:boterator-feed:v1.0 (by /u/mysubcult)")}

    async def request(self, method, url, **kwargs):
        if time.monotonic() < self.blocked_until.get(url, 0):
            raise SourceError("Reddit rate limit cooldown; retry later")
        try:
            async with self.session.request(method, url, **kwargs) as response:
                if response.status == 429:
                    try:
                        cooldown = max(900, int(response.headers.get("Retry-After", "900")))
                    except ValueError:
                        cooldown = 3600
                    self.blocked_until[url] = time.monotonic() + cooldown
                if response.status == 401:
                    self.expires = 0
                if response.status != 200:
                    raise SourceError(f"Reddit HTTP {response.status}; retry next poll")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 4 * 1024 * 1024:
                        raise SourceError("Reddit response exceeds size limit")
                return bytes(raw)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise SourceError("Reddit network error; retry next poll") from None

    async def oauth_headers(self):
        if not self.client_id or not self.secret:
            raise SourceError("Reddit OAuth credentials missing")
        if time.monotonic() >= self.expires:
            raw = await self.request("POST", "https://www.reddit.com/api/v1/access_token",
                                     auth=aiohttp.BasicAuth(self.client_id, self.secret),
                                     headers=self.headers, data={"grant_type": "client_credentials"})
            try:
                data = json.loads(raw)
                self.token = data["access_token"]
                self.expires = time.monotonic() + max(0, int(data["expires_in"]) - 60)
            except (KeyError, ValueError, TypeError):
                raise SourceError("Reddit OAuth authentication failed") from None
        return {**self.headers, "Authorization": f"Bearer {self.token}"}

    async def fetch(self, feed):
        params = {"limit": "25", "raw_json": "1"}
        if feed.get("period"):
            params["t"] = feed["period"]
        path = f'/r/{feed["subreddit"]}/{feed["sort"]}'
        if os.getenv("REDDIT_SOURCE", "rss") == "oauth":
            raw = await self.request("GET", "https://oauth.reddit.com" + path,
                                     headers=await self.oauth_headers(), params=params)
            try:
                children = json.loads(raw)["data"]["children"]
                return [p for c in children if (p := from_json(c["data"])) is not None]
            except (KeyError, ValueError, TypeError):
                raise SourceError("Unexpected Reddit JSON response") from None
        rss_params = {"sort": "top", "t": feed.get("period", "day")} if feed["sort"] == "top" else {}
        rss_url = feed.get("rss_url") or "https://www.reddit.com" + path + "/.rss" + ("?" + urlencode(rss_params) if rss_params else "")
        if time.monotonic() < self.blocked_until.get(rss_url, 0):
            raise SourceError("Reddit rate limit cooldown; retry later")
        raw = await asyncio.to_thread(self.read_rss, rss_url)
        return from_rss(raw)

    def read_rss(self, url):
        # This standard HTTP client is verified against all three canonical feeds
        # from the worker network. It runs in a thread to avoid blocking asyncio.
        try:
            with urlopen(Request(url, headers=self.headers), timeout=40) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise SourceError("Reddit response exceeds size limit")
                return raw
        except HTTPError as exc:
            if exc.code == 429:
                try:
                    cooldown = max(900, int(exc.headers.get("Retry-After", "900")))
                except ValueError:
                    cooldown = 3600
                self.blocked_until[url] = time.monotonic() + cooldown
            raise SourceError(f"Reddit HTTP {exc.code}; retry next poll") from None
        except (URLError, TimeoutError, OSError):
            raise SourceError("Reddit network error; retry next poll") from None


def load_config(path):
    config = json.loads(Path(path).read_text())
    feeds = config["feeds"]
    if not feeds or len({f["name"] for f in feeds}) != len(feeds):
        raise ValueError("Feed names must be unique")
    for f in feeds:
        if not re.fullmatch(r"[A-Za-z0-9_]+", f["subreddit"]):
            raise ValueError("Invalid subreddit")
        if f["sort"] not in {"hot", "top"} or f["style"] not in {"thought", "source", "photos"}:
            raise ValueError("Invalid feed mode")
        if f.get("period") not in {None, "day", "week", "month", "year", "all"}:
            raise ValueError("Invalid top period")
        if f.get("rss_url"):
            url = urlsplit(f["rss_url"])
            if url.scheme != "https" or url.netloc != "www.reddit.com" or not url.path.endswith(".rss"):
                raise ValueError("RSS must be a public Reddit HTTPS feed")
        if not re.fullmatch(r"@[A-Za-z0-9_]+", f["channel"]):
            raise ValueError("Invalid channel")
    return config


async def bot_for(conn, feed):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        token = await conn.fetchval("SELECT token FROM registered_bots WHERE id=$1 AND active=TRUE", feed["bot_id"])
    if not token:
        raise SourceError("Publishing bot unavailable")
    return Bot(token)


async def check_bot(bot, feed):
    me = await bot.get_me()
    member = await bot.get_chat_member(feed["channel"], me.id)
    if member.status not in {"creator", "administrator"} or (member.status == "administrator" and not member.can_post_messages):
        raise SourceError("Bot needs channel publishing permission")
    return me.username


async def send(bot, channel, op):
    kwargs = {k: v for k, v in op.items() if k not in {"method", "preview", "preview_url"}}
    if op["method"] == "send_media_group":
        kwargs["media"] = [InputMediaPhoto(**item) for item in kwargs["media"]]
    else:
        kwargs["parse_mode"] = "HTML"
    if op["method"] == "send_message":
        kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=not op["preview"], url=op.get("preview_url"))
    result = await getattr(bot, op["method"])(chat_id=channel, **kwargs)
    return [m.message_id for m in (result if isinstance(result, list) else [result])]


async def deliver(conn, bot, feed, row):
    name, pid = feed["name"], row["post_id"]
    for index in range(row["next_operation"], len(row["operations"])):
        # Commit intent BEFORE the network call. A crash/timeout is ambiguous and
        # must not cause automatic reposting. Operator inspects the channel first.
        await conn.execute("UPDATE reddit_feed_posts SET status='sending',updated_at=NOW() WHERE feed=$1 AND post_id=$2", name, pid)
        try:
            ids = await send(bot, feed["channel"], row["operations"][index])
        except TelegramRetryAfter as exc:
            await conn.execute("UPDATE reddit_feed_posts SET status='pending',retry_at=NOW()+($3::int * interval '1 second'),updated_at=NOW() WHERE feed=$1 AND post_id=$2", name, pid, max(1, exc.retry_after))
            LOG.warning("%s rate limited for %ss; resume next poll", name, exc.retry_after)
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            await conn.execute("UPDATE reddit_feed_posts SET status='failed',updated_at=NOW() WHERE feed=$1 AND post_id=$2", name, pid)
            LOG.error("%s %s rejected by Telegram; inspect permissions/media before retry", name, pid)
            return
        except Exception as exc:
            LOG.error("%s %s delivery uncertain (%s); manual review required", name, pid, type(exc).__name__)
            return
        await conn.execute("""UPDATE reddit_feed_posts SET next_operation=$3,
            message_ids=message_ids || $4::jsonb, status=$5,
            operations=CASE WHEN $5='sent' THEN '[]'::jsonb ELSE operations END,
            retry_at=NULL,updated_at=NOW() WHERE feed=$1 AND post_id=$2""",
                           name, pid, index + 1, ids, "sent" if index + 1 == len(row["operations"]) else "pending")
    LOG.info("%s %s published", name, pid)


async def cycle(conn, reddit, feed, mode):
    name = feed["name"]
    if mode == "run":
        ambiguous = await conn.fetchval("SELECT COUNT(*) FROM reddit_feed_posts WHERE feed=$1 AND status IN ('sending','failed')", name)
        if ambiguous:
            LOG.error("%s paused: %s uncertain/failed publications need review", name, ambiguous)
            return
    posts = await reddit.fetch(feed)
    if not posts:
        raise SourceError("Empty feed: refusing to initialize or publish")
    LOG.info("%s: %d posts read", name, len(posts))
    if mode == "preview":
        for post in posts[:3]:
            try:
                ops = plan(post, feed["style"])
                print(json.dumps({"feed": name, "post_id": post.id, "channel": feed["channel"], "operations": ops}, ensure_ascii=False))
            except SourceError as exc:
                print(json.dumps({"feed": name, "post_id": post.id, "held": str(exc)}, ensure_ascii=False))
        return
    source_key = f"{feed.get('subreddit', name)}:{feed.get('sort', 'hot')}:{feed.get('period', '')}"
    initialized = await conn.fetchval("SELECT source_key FROM reddit_feed_state WHERE feed=$1", name)
    if initialized != source_key:
        async with conn.transaction():
            for p in posts:
                await conn.execute("""INSERT INTO reddit_feed_posts(feed,post_id,fingerprint,status)
                    VALUES($1,$2,$3,'baseline') ON CONFLICT DO NOTHING""", name, p.id, p.fingerprint)
            await conn.execute("""INSERT INTO reddit_feed_state(feed,source_key) VALUES($1,$2)
                ON CONFLICT(feed) DO UPDATE SET source_key=EXCLUDED.source_key""", name, source_key)
        LOG.info("%s initialized without sending historical posts", name)
        return
    if mode == "init":
        LOG.info("%s already initialized; baseline unchanged", name)
        return
    # Journal every new RSS entry BEFORE attempting Telegram delivery. A failed
    # operation must not lose the other entries when they leave the RSS window.
    for p in posts:
        known = await conn.fetchrow("SELECT status FROM reddit_feed_posts WHERE feed=$1 AND post_id=$2", name, p.id)
        if known and known["status"] != "held":
            continue
        try:
            operations = plan(p, feed["style"])
        except SourceError as exc:
            await conn.execute("""INSERT INTO reddit_feed_posts(feed,post_id,fingerprint,status)
                VALUES($1,$2,$3,'held') ON CONFLICT DO NOTHING""", name, p.id, p.fingerprint)
            LOG.warning("%s %s held: %s", name, p.id, exc)
            continue
        await conn.execute("""INSERT INTO reddit_feed_posts(feed,post_id,fingerprint,status,operations)
            VALUES($1,$2,$3,'pending',$4) ON CONFLICT (feed,post_id) DO UPDATE
            SET status='pending',operations=EXCLUDED.operations,updated_at=NOW()
            WHERE reddit_feed_posts.status='held'""", name, p.id, p.fingerprint, operations)
    pending = await conn.fetch("SELECT * FROM reddit_feed_posts WHERE feed=$1 AND status='pending' ORDER BY created_at,post_id", name)
    if not pending:
        return
    bot = await bot_for(conn, feed)
    try:
        await check_bot(bot, feed)
        for row in pending:
            if row["retry_at"] and row["retry_at"].timestamp() > time.time():
                return
            await deliver(conn, bot, feed, row)
            status = await conn.fetchval("SELECT status FROM reddit_feed_posts WHERE feed=$1 AND post_id=$2", name, row["post_id"])
            if status != "sent":
                return
            await asyncio.sleep(2)
    finally:
        await bot.session.close()


async def amain(args):
    config = load_config(args.config)
    mode = args.mode
    if mode == "run" and os.getenv("REDDIT_PUBLISH_ENABLED") != "1":
        raise SourceError("Set REDDIT_PUBLISH_ENABLED=1 only after IFTTT is disabled and channels checked")
    pool = None
    conn = None
    try:
        if mode != "preview":
            pool = await create_pool(os.environ["DATABASE_URL"])
            conn = await pool.acquire()
            if mode in {"init", "run"}:
                if not await conn.fetchval("SELECT pg_try_advisory_lock(74236918024)"):
                    raise SourceError("Another feed publisher is running")
                await conn.execute(SCHEMA)
        if mode == "status":
            if not await conn.fetchval("SELECT to_regclass('reddit_feed_posts')"):
                print("Publisher not initialized")
                return
            for row in await conn.fetch("SELECT feed,status,COUNT(*) AS count FROM reddit_feed_posts GROUP BY feed,status ORDER BY feed,status"):
                print(json.dumps(dict(row)))
            for row in await conn.fetch("SELECT feed,post_id,status,next_operation,message_ids FROM reddit_feed_posts WHERE status IN ('held','sending','failed','pending') ORDER BY updated_at DESC LIMIT 50"):
                print(json.dumps(dict(row)))
            return
        if mode == "check":
            for f in config["feeds"]:
                bot = await bot_for(conn, f)
                try:
                    LOG.info("%s: @%s can publish to %s", f["name"], await check_bot(bot, f), f["channel"])
                finally:
                    await bot.session.close()
            return
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
            reddit = Reddit(session)
            while True:
                started = time.monotonic()
                failures = 0
                for index, feed in enumerate(config["feeds"]):
                    if index:
                        await asyncio.sleep(60)
                    try:
                        await cycle(conn, reddit, feed, mode)
                    except SourceError as exc:
                        failures += 1
                        LOG.error("%s: %s", feed["name"], exc)
                    except Exception as exc:
                        failures += 1
                        LOG.error("%s: %s (details suppressed to protect credentials)", feed["name"], type(exc).__name__)
                if mode != "run":
                    if failures:
                        raise SourceError(f"{failures} feed(s) failed")
                    break
                await asyncio.sleep(max(1, max(900, config.get("poll_seconds", 900)) - (time.monotonic() - started)))
    finally:
        if pool:
            if conn:
                await pool.release(conn)
            await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preview", "check", "status", "init", "run"])
    parser.add_argument("--config", default="reddit-feeds.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        LOG.error("Publisher stopped: %s", str(exc) if isinstance(exc, SourceError) else type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
