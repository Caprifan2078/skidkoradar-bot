"""Adaptors for multiple retailers.

IMPORTANT: An adaptor is not a verified retailer API connection. Only explicitly
configured upstream sources are contacted. No site scraping, captchas, or guessed stock.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from core import MCPClient, ProviderError, matches, parse_products, parse_stores


CHAIN_LABELS = {
    "pyaterochka": "Пятёрочка",
    "perekrestok": "Перекрёсток",
    "magnit": "Магнит",
    "magnit_cosmetic": "Магнит Косметик",
    "chizhik": "Чижик",
    "dixy": "Дикси",
    "lenta": "Лента",
    "vkusvill": "ВкусВилл",
    "auchan": "Ашан",
}
CHAIN_ALIASES = {
    "5ka": "pyaterochka", "пятерочка": "pyaterochka", "пятёрочка": "pyaterochka",
    "перекресток": "perekrestok", "перекрёсток": "perekrestok",
    "магнит": "magnit", "магниткосметик": "magnit_cosmetic", "магнит-косметик": "magnit_cosmetic",
    "мк": "magnit_cosmetic", "чижик": "chizhik", "дикси": "dixy",
    "лента": "lenta", "вкусвилл": "vkusvill", "ашан": "auchan"
}


def resolve_chain(text: str) -> str | None:
    key = text.strip().lower().replace(" ", "_")
    return key if key in CHAIN_LABELS else CHAIN_ALIASES.get(key.replace("_", "")) or CHAIN_ALIASES.get(key)


@dataclass
class Adapter:
    slug: str
    label: str
    kind: str  # mcp / json / offline

    @property
    def configured(self) -> bool:
        return False

    def nearest(self, lat: float, lon: float, limit: int) -> list[dict]:
        raise ProviderError("Источник магазинов не настроен")

    def search(self, store_id: str, query: str) -> list[dict]:
        raise ProviderError("Источник каталога не настроен")

    def catalog(self, store_id: str, *, cursor: str | None = None, limit: int = 60) -> tuple[list[dict], str | None]:
        raise NotImplementedError("Перечисление каталога не поддерживается источником")


class DisabledAdapter(Adapter):
    def __init__(self, slug: str, label: str):
        super().__init__(slug, label, "offline")


class PyaterochkaAdapter(Adapter):
    def __init__(self, url: str):
        super().__init__("pyaterochka", CHAIN_LABELS["pyaterochka"], "mcp")
        self.client = MCPClient(url, timeout=12)

    @property
    def configured(self) -> bool:
        return self.client.configured

    def nearest(self, lat: float, lon: float, limit: int) -> list[dict]:
        data = self.client.call("find_nearest_stores", {"latitude": lat, "longitude": lon, "limit": limit})
        return parse_stores(data)

    def search(self, store_id: str, query: str) -> list[dict]:
        data = self.client.call("search_products", {"store_id": store_id, "query": query, "limit": 30})
        return parse_products(data)


    def catalog(self, store_id: str, *, cursor: str | None = None,
                limit: int = 60) -> tuple[list[dict], str | None]:
        """Bounded, incremental category scan via the MCP server's *documented* tools.

        Two MCP tool calls per page (categories, then products).  Each call visits
        at most one category page.  This is not a one-click scan of all shelves.
        Cursor is category-index:offset and is persisted by the hunt engine.
        """
        from core import ProviderError
        import re
        limit = min(60, max(1, int(limit)))
        raw = self.client.call("list_store_categories", {"store_id": store_id})
        if not isinstance(raw, dict) or not isinstance(raw.get("categories"), list):
            raise ProviderError("Категории Пятёрочки не получены")
        categories = []
        seen = set()
        for row in raw["categories"][:500]:
            if not isinstance(row, dict):
                continue
            cat_id = str(row.get("id") or "")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cat_id) or cat_id in seen:
                continue
            seen.add(cat_id)
            categories.append(cat_id)
        if not categories:
            raise ProviderError("Категории Пятёрочки пусты")

        category_i, offset = 0, 0
        if cursor:
            m = re.fullmatch(r"(\d{1,3}):(\d{1,7})", cursor)
            if not m:
                raise ProviderError("Некорректный курсор категорий")
            category_i, offset = int(m.group(1)), int(m.group(2))
        if category_i >= len(categories):
            category_i, offset = 0, 0  # category list changed since last scan
        data = self.client.call("list_category_products", {
            "store_id": store_id, "category_id": categories[category_i],
            "limit": limit, "offset": offset,
        })
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise ProviderError("Категория Пятёрочки вернула неверный формат товаров")
        rows = data["products"][:limit]
        # We advance by the number of raw products, including those without
        # valid prices; otherwise we can keep fetching the same page forever.
        if len(rows) >= limit:
            next_i, next_offset = category_i, offset + len(rows)
        else:
            next_i, next_offset = category_i + 1, 0
        # A full category cycle ends; the next scan will restart from the first
        # category, allowing periodic revisits as prices change.
        next_cursor = f"{next_i}:{next_offset}" if next_i < len(categories) else None
        return parse_products({"products": rows}), next_cursor


class _DenyRedirect(urllib.request.HTTPRedirectHandler):
    """Don't forward the configured bridge bearer token to another host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonCatalogAdapter(Adapter):
    """Custom, operator-configured JSON catalog bridge (NOT an official retail API).

    GET {BASE_URL}/stores?lat=&lon=&limit= -> [{"id":"...","address":"..."}]
    GET {BASE_URL}/search?store_id=&query=&limit= -> [{"name":"...","price":119,"stock":2}]
    Can also return {"stores": [...]}, {"products": [...]}; stock optional.
    Set <SLUG>_SOURCE_URL and optionally <SLUG>_SOURCE_TOKEN in process environment.
    """

    def __init__(self, slug: str, label: str, base_url: str, token: str = ""):
        super().__init__(slug, label, "json")
        self.base_url = base_url.strip().rstrip("/")
        self.token = token.strip()
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"Некорректный URL источника {slug}")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"Для удалённого источника {slug} нужен HTTPS")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _get(self, path: str, params: dict[str, str | int | float]) -> Any:
        url = self.base_url + path + "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "DiscountHunterBot/0.2"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, headers=headers)
        opener = urllib.request.build_opener(_DenyRedirect)
        try:
            with opener.open(req, timeout=12) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ProviderError("Слишком большой ответ источника")
                return json.loads(raw)
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            # Never expose API keys or URLs from underlying exception messages.
            raise ProviderError(f"Ошибка источника {self.label}: {type(exc).__name__}") from exc

    def nearest(self, lat: float, lon: float, limit: int) -> list[dict]:
        return parse_stores(self._get("/stores", {"lat": lat, "lon": lon, "limit": limit}))

    def search(self, store_id: str, query: str) -> list[dict]:
        return parse_products(self._get("/search", {"store_id": store_id, "query": query, "limit": 30}))

    def catalog(self, store_id: str, *, cursor: str | None = None, limit: int = 60) -> tuple[list[dict], str | None]:
        """Optional JSON bridge endpoint, not an official retailer API.

        GET /catalog?store_id=&limit=&cursor= ->
            {"products": [...], "next_cursor": "..."} or {"products": [...]}.
        """
        params = {"store_id": store_id, "limit": min(100, max(1, limit))}
        if cursor:
            params["cursor"] = cursor[:120]
        raw = self._get("/catalog", params)
        if not isinstance(raw, dict) or not isinstance(raw.get("products"), list):
            raise ProviderError("Источник вернул некорректный ответ /catalog")
        next_cursor = raw.get("next_cursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 120):
            raise ProviderError("Некорректная пагинация каталога")
        return parse_products({"products": raw["products"][:100]}), next_cursor or None


def load_adapters(environ=None) -> dict[str, Adapter]:
    env = os.environ if environ is None else environ
    adapters: dict[str, Adapter] = {}
    for slug, label in CHAIN_LABELS.items():
        # explicit json source can be used for any chain, including 5ka
        env_slug = slug.upper()
        url = env.get(f"{env_slug}_SOURCE_URL", "")
        if url:
            adapters[slug] = JsonCatalogAdapter(slug, label, url, env.get(f"{env_slug}_SOURCE_TOKEN", ""))
        elif slug == "pyaterochka" and env.get("PYATEROCHKA_MCP_URL", ""):
            adapters[slug] = PyaterochkaAdapter(env["PYATEROCHKA_MCP_URL"])
        else:
            adapters[slug] = DisabledAdapter(slug, label)
    return adapters


@dataclass
class MultiReport:
    attempted: int
    failed: int
    skipped_unconfigured: int
    total_saved: int
    results: list[dict]
    errors_by_chain: dict[str, int]
    scan_by_chain: dict[str, int]
    elapsed: float


def scan_multi(adapters: dict[str, Adapter], stores: list[dict], query: str, *,
               selected_chains: set[str] | None = None, max_stores: int = 40,
               pace_seconds: float = 1.2, sleeper=time.sleep) -> MultiReport:
    started = time.monotonic()
    eligible = [s for s in stores if selected_chains is None or s["chain"] in selected_chains]
    results: list[dict] = []
    attempts = 0
    failed = 0
    skipped = sum(not adapters[s["chain"]].configured for s in eligible)
    # Round-robin scheduling: one crowded chain cannot consume the whole quota.
    queues: dict[str, list[dict]] = {}
    for store in eligible:
        if adapters[store["chain"]].configured:
            queues.setdefault(store["chain"], []).append(store)
    scheduled = []
    while len(scheduled) < max_stores and any(queues.values()):
        for chain in adapters:
            if queues.get(chain) and len(scheduled) < max_stores:
                scheduled.append(queues[chain].pop(0))
    error_counts: dict[str, int] = {}
    scan_counts: dict[str, int] = {}
    for store in scheduled:
        adapter = adapters[store["chain"]]
        if attempts > 0:
            sleeper(max(0.0, pace_seconds))
        attempts += 1
        scan_counts[store["chain"]] = scan_counts.get(store["chain"], 0) + 1
        try:
            products = adapter.search(store["id"], query)
        except ProviderError:
            failed += 1
            error_counts[store["chain"]] = error_counts.get(store["chain"], 0) + 1
            continue
        for p in products:
            if matches(query, p):
                results.append({**p, "chain": store["chain"], "store_id": store["id"],
                                "address": store["address"]})
    # An unconfigured source is not a failed check.
    results.sort(key=lambda p: (p["price"], p["chain"], p["address"]))
    return MultiReport(attempts, failed, skipped, len(eligible), results,
                       error_counts, scan_counts, time.monotonic() - started)


def render_multi(query: str, report: MultiReport, adapters: dict[str, Adapter]) -> str:
    chains = ", ".join(f"{adapters[c].label}: {n}" for c, n in sorted(report.scan_by_chain.items())) or "нет"
    msg = [f"🔎 {query}", f"Проверено магазинов: {report.attempted} из {report.total_saved} выбранных.",
           f"По сетям: {chains}.", f"Ошибок источников: {report.failed}; без подключения: {report.skipped_unconfigured}."]
    if report.total_saved > report.attempted + report.skipped_unconfigured:
        msg.append("⚠️ Часть сохранённых точек пропущена из-за лимита проверки или настроек.")
    if report.results:
        for p in report.results[:20]:
            stock = ("✅ источник сообщает о наличии" if p["status"] == "есть"
                     else "⛔ источник сообщает, что нет" if p["status"] == "нет"
                     else "❔ остаток не подтверждён")
            msg.append(f"\n• {adapters[p['chain']].label} — {p['name']}\n"
                       f"  {p['price']:.2f} ₽ · {p['address']}\n  {stock}")
        if len(report.results) > 20:
            msg.append(f"\n… ещё {len(report.results) - 20} совпадений не показаны")
    else:
        msg.append("Совпадений в полученных ответах нет. Это НЕ подтверждает отсутствие товара на полках.")
    msg.append("⚠️ Онлайн-каталог, доставка и реальные остатки на полке могут различаться.")
    return "\n".join(msg)[:3950]


def render_deal(deal, report: MultiReport, adapters: dict[str, Adapter]) -> str:
    """Separate matching *claimed* post price from stores charging more.

    A post is never a source of verified store stock. Claimed stock is only what
    an explicitly connected retailer data source reports at scan time.
    """
    heading = [f"🛍️ Проверка скидки: {deal.query}"]
    if deal.chain:
        heading.append(f"Сеть из поста: {adapters[deal.chain].label}")
    if deal.target_price is not None:
        heading.append(f"Цена из поста (не проверена): {deal.target_price:.2f} ₽")
    heading.append(f"Проверено: {report.attempted}/{report.total_saved} сохранённых магазинов; "
                   f"ошибок: {report.failed}; без источника: {report.skipped_unconfigured}")
    heading.append("Данные получены: " + datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"))
    if not report.attempted:
        heading.append("⛔ Нет проверенных магазинов. Настрой источник и добавь точки через геолокацию.")
        return "\n".join(heading)
    exact = []
    expensive = []
    unavailable = []
    for result in report.results:
        if result["status"] == "нет":
            unavailable.append(result)
        elif deal.target_price is None or result["price"] <= deal.target_price:
            exact.append(result)
        else:
            expensive.append(result)
    # Prefer a source reporting >0 to a mere catalog entry.
    exact.sort(key=lambda r: (r["status"] != "есть", r["price"], r["address"]))
    expensive.sort(key=lambda r: (r["status"] != "есть", r["price"], r["address"]))

    def item(r):
        stock = ("✅ источник сообщает о наличии" if r["status"] == "есть" else
                 "⛔ источник сообщает, что нет" if r["status"] == "нет" else
                 "❔ остаток не подтверждён")
        identity = "точный штрихкод" if deal.query.isdigit() else "совпадение по названию (сверь модель)"
        return (f"• {adapters[r['chain']].label} · {r['price']:.2f} ₽ · {r['address']}\n"
                f"  {r['name']}\n  {stock}; {identity}")

    if exact:
        heading.append("\n✅ Найденные предложения по цене из поста или дешевле:" if deal.target_price is not None
                       else "\nНайденные предложения (цена из поста не указана):")
        heading.extend(item(r) for r in exact[:12])
    elif deal.target_price is not None:
        heading.append("\n❌ Предложений по заявленной цене среди проверенных точек не найдено.")
    else:
        heading.append("\nСовпадений в ответах нет.")
    if expensive and deal.target_price is not None:
        heading.append("\n⚠️ Товар встречается, но дороже указанной скидки:")
        heading.extend(item(r) for r in expensive[:5])
    if unavailable:
        heading.append("\n⛔ Источник сообщает об отсутствии в этих магазинах:")
        heading.extend(item(r) for r in unavailable[:7])
    if report.attempted < report.total_saved:
        heading.append("\n⚠️ Охвачены не все сохранённые точки.")
    heading.append("\n⚠️ Складские данные/онлайн-каталог ≠ гарантия товара на полке или цены на кассе. "
                   "Не найдено ≠ отсутствует во всех магазинах. Сверь скидку перед поездкой.")
    return "\n".join(heading)[:3950]
