"""Independent retailer-feed price hunting; Telegram channels are NOT a dependency.

Only configured catalog sources are queried. Observations and price changes are not
proof of physical shelf inventory or a market-wide discount.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import re
import time

from core import ProviderError, matches

DEFAULT_TERMS = ("кофе", "шампунь", "шоколад", "набор")


def product_identity(product: dict) -> str:
    """Store/chain-scoped key: prefer barcode; exact normalized name otherwise."""
    code = re.sub(r"\D", "", str(product.get("code", "")))
    if 8 <= len(code) <= 14:
        raw = "ean:" + code
    else:
        raw = "name:" + re.sub(r"\s+", " ", str(product.get("name", "")).casefold().replace("ё", "е")).strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pct_off(price: Decimal, reference: Decimal) -> int:
    if reference <= 0 or price >= reference:
        return 0
    return int(((reference - price) * Decimal(100) / reference).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class HuntDeal:
    chain: str
    address: str
    store_id: str
    product_name: str
    price: Decimal
    baseline: Decimal
    pct: int
    evidence: str                 # history | retailer_old_price
    stock: str                    # есть | не подтверждено
    code: str = ""


@dataclass
class HuntReport:
    stores_checked: int = 0
    requests: int = 0
    errors: int = 0
    products_seen: int = 0
    deals: list[HuntDeal] = field(default_factory=list)
    unsupported_catalogs: int = 0
    available_stores: int = 0


def hunt_prices(db, adapters: dict, user: int, *, selected: set[str] | None = None,
                max_stores: int = 4, max_requests: int = 16,
                pause_seconds: float = 1.0, sleeper=time.sleep,
                now: int | None = None) -> HuntReport:
    """Find material price drops for saved stores. All HTTP happens in adapters.

    For adapters with category browsing, try up to two pages of catalog;
    persist the per-store category cursor between scans. If unavailable,
    search user watch words. Round-robin across stores over successive scans.
    Hard caps limit risk to the source and server; no silent full-city claims.
    """
    report = HuntReport()
    now = int(time.time()) if now is None else now
    stores = [s for s in db.stores(user, chains=selected)
              if s["chain"] in adapters and adapters[s["chain"]].configured]
    report.available_stores = len(stores)
    if not stores:
        return report
    terms = db.price_terms(user)
    if not terms:
        terms = list(DEFAULT_TERMS)
    term_start = db.next_hunt_term_offset(user, len(terms), 4)
    chosen_terms = (terms[term_start:] + terms[:term_start])[:4]
    threshold = db.monitor_settings(user)["threshold"]
    # Persist a cursor so the same first four saved stores aren't always scanned.
    start = db.next_hunt_store_offset(user, len(stores), max_stores)
    ordered = stores[start:] + stores[:start]
    products_in_scan: set[tuple[str, str, str]] = set()
    candidates: list[HuntDeal] = []
    for store in ordered[:max(1, min(max_stores, len(stores)))]:
        if report.requests >= max_requests:
            break
        adapter = adapters[store["chain"]]
        report.stores_checked += 1
        items: list[dict] = []
        catalog_available = False
        cursor = db.catalog_cursor(user, store["chain"], store["id"])
        # Adapters may expose paged catalogs, including category browsing via MCP.
        for page in range(2):
            if report.requests >= max_requests:
                break
            if report.requests:
                sleeper(max(0.0, pause_seconds))
            report.requests += 1
            try:
                page_items, next_cursor = adapter.catalog(store["id"], cursor=cursor, limit=60)
                catalog_available = True
                items.extend(page_items)
                if next_cursor == cursor and next_cursor is not None:
                    # A broken provider must not cause endless rescanning.
                    report.errors += 1
                    break
                db.catalog_cursor_save(user, store["chain"], store["id"], next_cursor)
                cursor = next_cursor
                if not next_cursor:
                    break
            except (ProviderError, NotImplementedError):
                # A bridge without /catalog can still search known terms.
                if page == 0:
                    report.unsupported_catalogs += 1
                else:
                    report.errors += 1
                break
        if not catalog_available:
            for term in chosen_terms:
                if report.requests >= max_requests:
                    break
                if report.requests:
                    sleeper(max(0.0, pause_seconds))
                report.requests += 1
                try:
                    products = adapter.search(store["id"], term)
                    items.extend(p for p in products if matches(term, p))
                except ProviderError:
                    report.errors += 1
        for product in items[:180]:
            if product.get("status") == "нет":
                continue  # We must not advertise an explicitly unavailable item.
            price = product.get("price")
            if not isinstance(price, Decimal) or price <= 0:
                continue
            key = product_identity(product)
            observation_key = (store["chain"], store["id"], key)
            if observation_key in products_in_scan:
                continue
            products_in_scan.add(observation_key)
            report.products_seen += 1
            reference, evidence = db.price_reference(user, store["chain"], store["id"], key, now=now)
            claimed_old = product.get("old_price")
            # Prefer independently observed historic prices over advertised old prices.
            if reference is None and isinstance(claimed_old, Decimal) and claimed_old > price:
                reference, evidence = claimed_old, "retailer_old_price"
            discount = pct_off(price, reference) if reference is not None else 0
            db.price_observe(user, store["chain"], store["id"], key, price, now=now)
            if discount < threshold:
                continue
            candidates.append(HuntDeal(
                chain=store["chain"], address=store["address"], store_id=store["id"],
                product_name=str(product.get("name", ""))[:150], price=price,
                baseline=reference, pct=discount, evidence=evidence,
                stock=product.get("status", "не подтверждено"), code=product.get("code", "")))
    candidates.sort(key=lambda d: (-d.pct, d.price, d.chain, d.address))
    # Mark only deals actually offered for display, so lower-ranked offers are
    # eligible in future rounds rather than silently lost behind a display cap.
    for d in candidates:
        if len(report.deals) >= 8:
            break
        key = product_identity({"code": d.code, "name": d.product_name})
        if db.price_new_alert(user, d.chain, d.store_id, key, d.price, now=now):
            report.deals.append(d)
    return report


def render_hunt(report: HuntReport, adapters: dict, *, max_deals: int = 8) -> str:
    lines = ["🔍 Самостоятельный поиск скидок в каталогах магазинов",
             f"Обойдено сохранённых точек: {report.stores_checked}/{report.available_stores}.",
             f"Проверок каталога/поиска: {report.requests}; товаров: {report.products_seen}; ошибок: {report.errors}."]
    if not report.available_stores:
        lines.append("⛔ Нет точек с подключёнными каталогами. Нужен источник данных и /near или /store. /status — подключения.")
    elif not report.deals:
        lines.append("Подходящих новых скидок в этой выборке не найдено. Это НЕ значит, что их нет в других магазинах.")
        lines.append("Для выявления падений цены нужно накопить историю; каталог может сообщить старую цену сразу.")
    else:
        lines.append(f"🔥 Новых сигналов: {len(report.deals)} (показано до {max_deals})")
        for d in report.deals[:max_deals]:
            baseline_label = "история наших замеров" if d.evidence == "history" else "старая цена из каталога (не проверена)"
            status = "источник сообщает о наличии" if d.stock == "есть" else "наличие не подтверждено"
            lines.append(f"\n• −{d.pct}% · {adapters[d.chain].label} · {d.product_name}\n"
                         f"  {d.price} ₽ вместо {d.baseline} ₽ ({baseline_label})\n"
                         f"  {d.address} · {status}")
    if report.unsupported_catalogs:
        lines.append(f"⚠️ {report.unsupported_catalogs} точек без обхода каталога: проверялись только поисковые слова.")
    lines.append("Данные интернет-каталога ≠ гарантированный остаток на полке. Время проверки — сейчас.")
    return "\n".join(lines)[:3950]
