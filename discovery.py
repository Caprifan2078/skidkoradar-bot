"""Bounded public Telegram channel scanning; no Telegram user session or bypass.

Data from untrusted third-party posts is NEVER treated as verified price/stock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
import re
import urllib.request

from deals import Deal, parse_deal

DEFAULT_CHANNELS = ("tspyaterochka", "magnit_retail", "dixy_russia", "lentacom")
CHAIN_BY_CHANNEL = {"tspyaterochka": "pyaterochka", "magnit_retail": "magnit", "dixy_russia": "dixy", "lentacom": "lenta"}
_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def normalize_channel(raw: str) -> str | None:
    text = (raw or "").strip()
    text = re.sub(r"^https?://(?:www\.)?t\.me/(?:s/)?", "", text, flags=re.I)
    if text.startswith("@"):
        text = text[1:]
    return text.lower() if _CHANNEL_RE.fullmatch(text) else None


@dataclass(frozen=True)
class ChannelPost:
    channel: str
    post_id: str
    url: str
    text: str
    timestamp: int | None
    references: tuple[str, ...] = ()


class TelegramPublicHTML(HTMLParser):
    """Extract post text only from /s/ public channel HTML, not page-wide adverts."""
    def __init__(self, channel: str):
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.posts: list[ChannelPost] = []
        self.depth = 0
        self.text_depth = 0
        self.cur: dict | None = None
        self.chunks: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        # A Telegram post root has data-post='channel/123'.
        data_post = attrs.get("data-post", "")
        m = re.fullmatch(re.escape(self.channel) + r"/(\d+)", data_post, re.I)
        if self.cur is None and m:
            self.cur = {"post_id": m.group(1), "date": None}
            self.depth = 1
            self.text_depth = 0
            self.chunks = []
            self.links = []
            return
        if self.cur is None:
            return
        self.depth += 1
        classes = attrs.get("class", "").split()
        if "tgme_widget_message_text" in classes and self.text_depth == 0:
            self.text_depth = 1
        elif self.text_depth:
            self.text_depth += 1
            if tag in {"br", "p", "div", "li"}:
                self.chunks.append("\n")
        if tag == "a" and self.text_depth and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "time" and attrs.get("datetime"):
            self.cur["date"] = attrs["datetime"]
        if tag in {"br", "img", "hr", "meta", "input", "source", "wbr"}:
            self.depth -= 1
            if self.text_depth:
                self.text_depth -= 1

    def handle_endtag(self, tag):
        if self.cur is None:
            return
        if self.text_depth:
            self.text_depth -= 1
            if tag in {"p", "div", "li"}:
                self.chunks.append("\n")
        self.depth -= 1
        if self.depth <= 0:
            text = re.sub(r"\n{3,}", "\n\n", "".join(self.chunks)).strip()
            ts = None
            if self.cur.get("date"):
                try:
                    ts = int(datetime.fromisoformat(self.cur["date"].replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass
            if text:
                self.posts.append(ChannelPost(self.channel, self.cur["post_id"],
                                              f"https://t.me/{self.channel}/{self.cur['post_id']}",
                                              text[:4000], ts, tuple(self.links[:30])))
            self.cur = None
            self.depth = self.text_depth = 0

    def handle_data(self, data):
        if self.cur is not None and self.text_depth:
            self.chunks.append(data)


def parse_public_page(channel: str, html: str) -> list[ChannelPost]:
    parser = TelegramPublicHTML(channel)
    parser.feed(html)
    parser.close()
    return parser.posts[-35:]


def fetch_public_posts(channel: str, *, timeout: float = 14) -> list[ChannelPost]:
    if normalize_channel(channel) != channel:
        raise ValueError("Некорректный публичный канал")
    req = urllib.request.Request(f"https://t.me/s/{channel}", headers={
        "User-Agent": "Mozilla/5.0 (compatible; PersonalDiscountBot/0.5; +https://telegram.org/)",
        "Accept": "text/html",
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("Страница канала слишком велика")
    html = raw.decode("utf-8", errors="replace")
    return parse_public_page(channel, html)


@dataclass(frozen=True)
class DealCandidate:
    post: ChannelPost
    deal: Deal
    discount_pct: int


def qualify(post: ChannelPost, threshold: int, *, now: int | None = None,
            max_age_hours: int = 72) -> DealCandidate | None:
    """A high-percent **claim**, not a verified saving. Ignore promo/marketing spam."""
    if now is not None and post.timestamp is not None and now - post.timestamp > max_age_hours * 3600:
        return None
    text = post.text
    if any(term in text.lower() for term in (
        "промокод", "кешбэк", "кэшбэк", "доставк", "розыгрыш",
        "скидка на заказ", "скидки на заказ", "при покупке от",
    )):
        return None
    # One item with one current and one old price is required. A multi-item
    # list is too ambiguous to rank reliably.
    deal = parse_deal(text)
    if not deal.query or len(deal.query) < 5 or len(deal.query) > 155:
        return None
    if deal.target_price is None or deal.old_price is None:
        return None
    if deal.old_price <= deal.target_price:
        return None
    # Decimal, not binary floating point, to avoid wrong boundary behavior.
    percent = int(((deal.old_price - deal.target_price) * Decimal(100) / deal.old_price)
                  .quantize(Decimal('1')))
    if percent < threshold:
        return None
    if deal.chain is None and post.channel in CHAIN_BY_CHANNEL:
        deal = Deal(deal.query, CHAIN_BY_CHANNEL[post.channel], deal.target_price, deal.old_price)
    return DealCandidate(post, deal, percent)


# Public channel links seen inside genuine monitored posts, not arbitrary URLs.
_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})(?:/\d+)?(?:[/?#]|\b)", re.I)
_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})(?![\w])")

def referenced_channels(posts: list[ChannelPost], *, limit: int = 20) -> list[str]:
    """Discover handles only; candidates are not trusted until separately checked.

    This does not search all of Telegram. It follows public channel references.
    """
    found: list[str] = []
    for post in posts[-35:]:
        for raw in (post.text, *post.references):
            matches = [m.group(1) for m in _LINK_RE.finditer(raw)]
            # A @mention in a post is only a weak lead. Require feed verification.
            matches.extend(m.group(1) for m in _MENTION_RE.finditer(raw))
            for candidate in matches:
                ch = normalize_channel(candidate)
                if ch and ch != post.channel and not ch.endswith("bot") and ch not in found:
                    found.append(ch)
                    if len(found) >= limit:
                        return found
    return found
