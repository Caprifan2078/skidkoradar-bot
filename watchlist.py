"""Rotating, bounded checks for user-selected product-price targets.

Only a source's explicit positive stock signal is used for availability alerts.
A retailer's catalog result is not a guarantee of physical shelf stock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import time

from core import ProviderError, matches
from pricehunt import product_identity


@dataclass
class WatchReport:
    planned: int = 0
    attempted: int = 0
    skipped: int = 0
    errors: int = 0
    unknown: int = 0
    unavailable: int = 0
    offers: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)


def check_watches(db, adapters, user: int, *, selected: set[str] | None = None,
                  max_requests: int = 12, pause_seconds: float = 1.0,
                  sleeper=time.sleep, now: int | None = None) -> WatchReport:
    """Round-robin over (saved store, tracked product) pairs; at most max_requests.

    In a given cycle each pair is checked once and only configured sources are
    contacted. Failed requests are reported, not treated as product absence.
    """
    now = int(time.time()) if now is None else now
    selected = set(adapters) if selected is None else set(selected)
    jobs = []
    skipped = 0
    stores = db.stores(user, chains=selected)
    watches = db.watches(user)
    for w in watches:
        for store in stores:
            if w["chain"] and w["chain"] != store["chain"]:
                continue
            adapter = adapters.get(store["chain"])
            if not adapter or not adapter.configured:
                skipped += 1
                continue
            jobs.append((w, store, adapter))
    report = WatchReport(planned=len(jobs), skipped=skipped)
    if not jobs:
        return report
    limit = max(1, min(max_requests, 24, len(jobs)))
    start = db.next_watch_offset(user, len(jobs), limit)
    ordered = (jobs[start:] + jobs[:start])[:limit]
    for watch, store, adapter in ordered:
        if report.attempted:
            sleeper(max(0.0, pause_seconds))
        report.attempted += 1
        try:
            products = adapter.search(store["id"], watch["query"])
        except (ProviderError, TimeoutError, OSError):
            report.errors += 1
            continue
        for product in products:
            if not matches(watch["query"], product):
                continue
            price = product.get("price")
            if not isinstance(price, Decimal) or price <= 0 or price > Decimal(watch["max_price"]):
                continue
            status = product.get("status", "не подтверждено")
            if status == "нет":
                report.unavailable += 1
                continue
            if status != "есть":
                report.unknown += 1
                continue
            offer = {"watch_id": watch["id"], "chain": store["chain"],
                     "address": store["address"], "store_id": store["id"],
                     "name": product["name"], "price": price,
                     "target": Decimal(watch["max_price"]), "code": product.get("code", "")}
            offer["product_key"] = product_identity(product)
            report.offers.append(offer)
    report.offers.sort(key=lambda d: (d["price"], d["address"]))
    # Don't mark undisplayed alerts as delivered. The next cycle can show them.
    for offer in report.offers:
        if len(report.alerts) >= 12:
            break
        if db.watch_new_alert(user, offer["watch_id"], offer["chain"], offer["store_id"],
                              offer["product_key"], str(offer["price"]), now=now):
            report.alerts.append(offer)
    return report


def render_watches(report: WatchReport, adapters, *, manual: bool = True) -> str:
    lines = ["🎯 Проверка товаров по заданной цене",
             f"Проверено пар товар–магазин: {report.attempted}/{report.planned}; "
             f"ошибок: {report.errors}; неподключённых: {report.skipped}."]
    if report.unknown:
        lines.append(f"❔ {report.unknown} подходящих по цене предложений, но остаток неизвестен.")
    if report.unavailable:
        lines.append(f"⛔ {report.unavailable} предложений с нулевым остатком по данным источника.")
    if report.offers if manual else report.alerts:
        for offer in (report.offers if manual else report.alerts)[:12]:
            lines.append(f"\n✅ {adapters[offer['chain']].label}: {offer['name']}\n"
                         f"{offer['price']:.2f} ₽ (цель ≤ {offer['target']:.2f} ₽)\n"
                         f"{offer['address']}\nИсточник сообщает о наличии.")
    else:
        lines.append("Новых подтверждённых предложений в проверенной выборке нет." if not manual else
                     "Предложений с положительным остатком по заданной цене не найдено в проверенной выборке.")
    if report.planned > report.attempted:
        lines.append("⏳ Остальные пары будут проверены в следующих проходах.")
    lines.append("⚠️ Наличие в каталоге не гарантирует товар на полке. Запрос выполнен сейчас; "
                 "фактическое время обновления остатков магазином неизвестно.")
    return "\n".join(lines)[:3950]
