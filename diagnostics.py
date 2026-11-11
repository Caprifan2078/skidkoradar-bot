"""Limited connectivity checks: a configured URL is not proof of working catalog."""
from __future__ import annotations

from core import ProviderError


def probe_sources(adapters: dict, stores: list[dict], *, query: str = "кофе") -> list[dict]:
    """One harmless read per configured retailer with at least one saved store.

    Does not try unauthorized endpoints, does not guess a retailer's inventory.
    """
    results = []
    for chain, adapter in adapters.items():
        if not adapter.configured:
            results.append({"chain": chain, "state": "not_configured", "count": None})
            continue
        store = next((s for s in stores if s["chain"] == chain), None)
        if store is None:
            results.append({"chain": chain, "state": "no_store", "count": None})
            continue
        try:
            products = adapter.search(store["id"], query)
        except (ProviderError, OSError, TimeoutError):
            results.append({"chain": chain, "state": "error", "count": None})
        else:
            results.append({"chain": chain, "state": "responded", "count": len(products),
                            "stock_known": sum(p.get("status") in ("есть", "нет") for p in products)})
    return results


def render_diagnostics(rows: list[dict], adapters: dict) -> str:
    states = {
        "not_configured": "⚪ Источник не подключён",
        "no_store": "📍 Нужен магазин: /near",
        "error": "🔴 Источник не ответил корректно",
        "responded": "🟢 Получен ответ каталога",
    }
    text = ["🩺 Диагностика источников (пробный запрос по 1 сохранённой точке на сеть):"]
    for row in rows:
        line = f"{adapters[row['chain']].label}: {states[row['state']]}"
        if row["state"] == "responded":
            line += f", позиций: {row['count']}, статусов остатка: {row['stock_known']}"
        text.append(line)
    text.append("⚠️ Это не доказательство наличия товара на полке и не проверка всех магазинов.")
    return "\n".join(text)[:3950]
