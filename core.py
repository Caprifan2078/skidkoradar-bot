"""Store search core for a personal Telegram discount assistant.

No retailer data is fabricated: results come only from a configured MCP service.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class ProviderError(RuntimeError):
    pass


def unwrap_mcp(value: Any) -> Any:
    """Extract structuredContent or JSON/text from a standard MCP tools/call response."""
    if isinstance(value, dict) and "error" in value:
        raise ProviderError(str(value["error"])[:300])
    if isinstance(value, dict) and "result" in value:
        value = value["result"]
    if isinstance(value, dict) and value.get("isError"):
        raise ProviderError(str(value.get("content", "Ошибка источника"))[:300])
    if isinstance(value, dict) and value.get("structuredContent") is not None:
        return value["structuredContent"]
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        texts = [c.get("text") for c in value["content"] if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            combined = "\n".join(str(s) for s in texts if s)
            try:
                return json.loads(combined)
            except (ValueError, TypeError):
                return combined
    return value


class MCPClient:
    def __init__(self, url: str, timeout: int = 25):
        self.url = url.strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def call(self, tool: str, args: dict) -> Any:
        if not self.configured:
            raise ProviderError("Источник магазинов не подключён. См. README и /status.")
        body = json.dumps({"jsonrpc": "2.0", "id": int(time.time() * 1000) % 2_000_000_000,
                           "method": "tools/call", "params": {"name": tool, "arguments": args}}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                content = response.read(3_000_000).decode("utf-8", errors="replace")
                if content.startswith("event:") or content.startswith("data:"):
                    # MCP HTTP implementations may send Server-Sent Events.
                    events = [line[5:].strip() for line in content.splitlines() if line.startswith("data:")]
                    content = next((line for line in reversed(events) if line.startswith("{")), "")
                return unwrap_mcp(json.loads(content))
        except (OSError, TimeoutError, ValueError) as exc:
            # Some HTTP libraries include URL/query or proxy credentials in
            # exception strings. Never relay them into Telegram or logs.
            raise ProviderError(f"Ошибка MCP-источника: {type(exc).__name__}") from exc


def _rows(obj: Any, keys: tuple[str, ...]) -> list[dict]:
    """Conservatively extract a record list from known wrapper shapes."""
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), list):
                return [r for r in obj[k] if isinstance(r, dict)]
        for k in ("data", "result", "items", "products", "stores"):
            nested = obj.get(k)
            if nested is not None and nested is not obj:
                out = _rows(nested, keys)
                if out:
                    return out
        # Single normalized record is acceptable
        if any(k in obj for k in ("name", "title", "store_id", "id", "plu")):
            return [obj]
    return []


def parse_stores(payload: Any) -> list[dict]:
    result = []
    for row in _rows(payload, ("stores", "results", "items")):
        store_id = row.get("store_id") or row.get("sap_code") or row.get("id") or row.get("code")
        if not store_id:
            continue
        address = row.get("address") or row.get("full_address") or row.get("name") or "Адрес неизвестен"
        if isinstance(address, dict):
            address = address.get("full") or address.get("street") or "Адрес неизвестен"
        result.append({"id": str(store_id), "address": str(address)[:220]})
    return result


def _price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        return _price(value.get("value") or value.get("amount"))
    try:
        d = Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace("₽", "").replace(",", "."))
        if 0 < d < 1_000_000_000:
            return d
    except (InvalidOperation, ValueError):
        pass
    return None


def _availability(product: dict) -> str:
    # Presence in an online catalog is not evidence of in-store inventory.
    for key in ("stock", "quantity", "stock_count", "remaining", "inventory"):
        if key in product and isinstance(product[key], (int, float)) and not isinstance(product[key], bool):
            return "есть" if product[key] > 0 else "нет"
    for key in ("in_stock", "available", "is_available"):
        if key in product and isinstance(product[key], bool):
            return "есть" if product[key] else "нет"
    return "не подтверждено"


def parse_products(payload: Any) -> list[dict]:
    products = []
    for row in _rows(payload, ("products", "items", "results")):
        name = row.get("name") or row.get("title") or row.get("product_name") or ""
        if not name:
            continue
        price = _price(row.get("price") or row.get("current_price") or row.get("regular_price"))
        if not price:
            continue
        old = _price(row.get("old_price") or row.get("previous_price") or row.get("price_before_discount"))
        products.append({"name": str(name)[:200], "price": price,
                         "old_price": old if old and old > price else None,
                         "status": _availability(row),
                         "code": str(row.get("ean") or row.get("barcode") or row.get("plu") or "")[:30]})
    return products


def extract_query(post: str) -> str:
    """A cautious local parser: name or barcode from forwarded deal text."""
    cleaned = re.sub(r"https?://\S+", " ", post)
    eans = re.findall(r"(?<!\d)\d{8,14}(?!\d)", cleaned)
    for ean in eans:
        if len(ean) in (8, 12, 13, 14):
            return ean
    lines = []
    for raw in cleaned.splitlines():
        line = re.sub(r"^[\s\W_]+", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        if re.search(r"^(скидка|цена|цены|промокод|промо|пятёрочка|пятерочка|магнит|действует|ссылка|нашли|покупаем)\b", line, re.I):
            continue
        line = re.split(r"\s+[—–-]\s+(?:\d+[\s\d]*[₽р]|скидка)|\s+\d[\d\s]*(?:₽|руб(?:ля|лей)?)(?=\s|$)", line, maxsplit=1, flags=re.I)[0]
        line = re.sub(r"\b(?:скидка|вместо|цена)\b.*$", "", line, flags=re.I).strip(" .:;!—–-")
        if len(line) >= 3 and re.search(r"[а-яёa-z]", line, re.I):
            lines.append(line[:120])
    return lines[0] if lines else ""


def matches(query: str, product: dict) -> bool:
    q = query.casefold().strip()
    if q.isdigit():
        return q == str(product.get("code", ""))
    words = [w for w in re.findall(r"[а-яёa-z0-9]+", q) if len(w) >= 3]
    if not words:
        return False
    normalized = product["name"].casefold().replace("ё", "е")
    return all(word.replace("ё", "е") in normalized for word in words[:5])


@dataclass
class ScanReport:
    attempted: int
    failed: int
    matches: list[dict]
    elapsed: float


def scan(client: MCPClient, stores: list[dict], query: str, pace_seconds: float = 1.5,
         max_stores: int = 60, sleeper=time.sleep) -> ScanReport:
    """Scan only explicitly enrolled stores; no extrapolation to entire region."""
    started = time.monotonic()
    found = []
    failed = 0
    selected = stores[:max_stores]
    for ix, store in enumerate(selected):
        if ix:
            sleeper(max(0.0, pace_seconds))
        try:
            result = client.call("search_products", {"store_id": store["id"], "query": query, "limit": 20})
            for p in parse_products(result):
                if matches(query, p) and p["status"] != "нет":
                    found.append({**p, "store_id": store["id"], "address": store["address"]})
        except ProviderError:
            failed += 1
    found.sort(key=lambda f: (f["status"] != "есть", f["price"], f["address"]))
    return ScanReport(attempted=len(selected), failed=failed, matches=found, elapsed=time.monotonic()-started)


def render_report(query: str, report: ScanReport, total_stores: int) -> str:
    header = (f"🔎 {query}\nПроверено точек: {report.attempted} из {total_stores}. "
              f"Ошибок источника: {report.failed}.\n")
    if not report.matches:
        return (header + "\nПодтверждённых совпадений нет. "
                "Это НЕ означает отсутствие товара во всех магазинах: часть каталогов недоступна или товар назван иначе.")
    lines = [header]
    for m in report.matches[:15]:
        price = f"{m['price']:.2f}".replace(".", ",")
        status = "✅ Остаток указан" if m["status"] == "есть" else "❔ Есть в каталоге; остаток неизвестен"
        lines.append(f"• {m['name']} — {price} ₽\n  {m['address']}\n  {status}")
    if len(report.matches) > 15:
        lines.append(f"… ещё {len(report.matches) - 15} совпадений не показаны")
    lines.append("⚠️ Данные онлайн-каталога могут отличаться от наличия на полке. Звонок в магазин надёжнее.")
    return "\n\n".join(lines)[:3950]
