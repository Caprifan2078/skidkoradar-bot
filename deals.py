"""Deal parser for forwarded Telegram posts. No external AI or fabricated facts.

A claimed price from a forwarded post is only a *target*, not evidence that any
particular store honors it. We display it as such throughout the bot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from core import extract_query


@dataclass(frozen=True)
class Deal:
    query: str
    chain: str | None = None
    target_price: Decimal | None = None
    old_price: Decimal | None = None


# Ordering matters: "Магнит Косметик" should not be mistaken for "Магнит".
_CHAIN_PATTERNS = (
    ("magnit_cosmetic", r"\bмагнит\s*[-–]?\s*косметик\b|\bmagnit\s*cosmetic\b"),
    ("pyaterochka", r"\bпят[её]рочк[аеиу]\b|\b5\s*ка\b|\b5ka\b"),
    ("perekrestok", r"\bперекр[её]сток\b"),
    ("magnit", r"\bмагнит(?:е|а|у|ом)?\b|\bmagnit\b"),
    ("chizhik", r"\bчижик\b"),
    ("dixy", r"\bдикси\b"),
    ("lenta", r"\bлент[аеыу]\b"),
    ("vkusvill", r"\bвкусвилл\b"),
    ("auchan", r"\bашан\b"),
)

_PRICE_PATTERN = re.compile(
    r"(?<![\d])(?:\d{1,6}(?:[ \u00a0]\d{3})*|\d{1,6})"
    r"(?:[.,]\d{1,2})?\s*(?:₽|руб(?:лей|ля|ль|\.)?|р\.(?![А-Яа-я]))",
    flags=re.I,
)


def _money(text: str) -> Decimal | None:
    try:
        value = Decimal(re.sub(r"[^\d.,]", "", text).replace(",", "."))
        return value if Decimal("0") < value < Decimal("10000000") else None
    except (InvalidOperation, ValueError):
        return None


def parse_deal(post: str) -> Deal:
    """Extract an approximate product query, retailer hint and claimed sale price.

    Never interpret claims in a post as verified stock or price. If price is
    ambiguous, prefer no target price to an incorrect price.
    """
    text = re.sub(r"https?://\S+", " ", (post or ""))[:5000]
    chain = None
    for slug, pattern in _CHAIN_PATTERNS:
        if re.search(pattern, text, re.I):
            chain = slug
            break
    first_line = text.splitlines()[0] if text.splitlines() else ""
    # Remove leading source identifiers when joined with the product name.
    query_text = text
    if chain:
        for slug, pattern in _CHAIN_PATTERNS:
            query_text = re.sub(r"^\s*[🔥🛒‼️❗📣💥✨\s]*" + pattern + r"\s*[:!—–\-.,]*\s*",
                                "", query_text, count=1, flags=re.I)
            if query_text != text:
                break
    query = extract_query(query_text)
    # "вместо 2999 ₽" is an old price, never the deal target.
    preceding = re.split(r"\bвместо\b|\bбыло\s*[:—-]?", text, maxsplit=1, flags=re.I)
    prices = [_money(m.group(0)) for m in _PRICE_PATTERN.finditer(preceding[0])]
    prices = [p for p in prices if p is not None]
    target = prices[0] if len(prices) == 1 else None
    old = None
    if len(preceding) > 1:
        m = _PRICE_PATTERN.search(preceding[1])
        if m:
            old = _money(m.group(0))
    return Deal(query=query, chain=chain, target_price=target, old_price=old)
