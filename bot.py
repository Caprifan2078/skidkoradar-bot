#!/usr/bin/env python3
"""Telegram discount hunter with retailer adapters, using only Python stdlib."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from core import ProviderError, extract_query
from deals import Deal, parse_deal
from discovery import DEFAULT_CHANNELS, fetch_public_posts, normalize_channel, qualify, referenced_channels
from decimal import Decimal
from providers import CHAIN_LABELS, Adapter, load_adapters, render_multi, render_deal, resolve_chain, scan_multi
from storage import Storage
from pricehunt import hunt_prices, render_hunt, DEFAULT_TERMS
from watchlist import check_watches, render_watches
from diagnostics import probe_sources, render_diagnostics
from scheduler import BackgroundScheduler, ScheduledJob

LOG = logging.getLogger("skidki_bot")


def load_env(path=".env"):
    if not Path(path).exists():
        return
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def method(self, method: str, data: dict) -> object:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + method, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=42) as resp:
                obj = json.load(resp)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            # urllib exceptions embed the full URL, including the bot token.
            # Do not attach the cause or print its message in logs.
            raise RuntimeError(f"Telegram API request failed: {type(exc).__name__}") from None
        if not obj.get("ok"):
            raise RuntimeError(f"Telegram: {obj.get('description', 'неизвестная ошибка')}")
        return obj["result"]

    def send(self, chat_id: int, text: str, *, ask_location: bool = False):
        data = {"chat_id": chat_id, "text": text[:4000],
                "link_preview_options": {"is_disabled": True}}
        if ask_location:
            data["reply_markup"] = {"keyboard": [[{"text": "📍 Отправить геопозицию", "request_location": True}]],
                                    "one_time_keyboard": True, "resize_keyboard": True}
        return self.method("sendMessage", data)


HELP = ("🛍️ Охотник за скидками v0.8 — скидки из каталогов и каналов\n\n"
        "Перешли публикацию из публичного канала: добавлю его в мониторинг,\n"
        "а также распознаю сеть, товар и цену из поста.\n"
        "В ответе сравню цену из поста с предложениями по магазинам.\n"
        "При первом поиске пришли геопозицию, чтобы выбрать магазины.\n"
        "Поиск работает только по точкам с настроенным источником цен и остатков.\n\n"
        "/scan — сейчас проверить каналы на скидки\n"
        "/hunt — искать скидки в подключённых каталогах магазинов\n"
        "/watchdeal Магнит: кофе 199 ₽ — следить за товаром до заданной цены\n"
        "/watchlist — мои отслеживаемые товары\n"
        "/unwatchdeal 1 — убрать позицию из наблюдения\n"
        "/checkwatch — проверить товары по магазинам сейчас\n"
        "/track кофе — добавить слово для поиска (до 12)\n"
        "/untrack кофе — удалить слово\n"
        "/tracks — мои поисковые слова\n"
        "/sources — какие каналы читаю\n"
        "/watch @channel — добавить публичный канал\n"
        "/unwatch @channel — исключить канал\n"
        "/autochannels on/off — автоматический поиск новых публичных каналов\n"
        "/alerts 70 — порог скидки (30–95%)\n"
        "/pause и /resume — приостановить/включить уведомления\n"
        "/chains — сети, подключения и включение\n"
        "/on magnit — включить сеть; /off magnit — выключить\n"
        "/near 55.75 37.61 10 — найти ближайшие магазины разных сетей\n"
        "/region 55.75 37.61 5 — выборка точек в радиусе\n"
        "/store magnit 123 Москва, ул. Тестовая — вручную добавить магазин\n"
        "/stores — сохранённые точки\n"
        "/clearstores [сеть] — удалить все или сеть\n"
        "/search молоко — общий поиск по активным сетям\n"
        "/deal Магнит: кофе 199 ₽ вместо 499 ₽ — проверить скидку\n"
        "/status — состояние источников и ограничения\n"
        "/coverage — реальные последние проверки и охват\n"
        "/doctor — пробная проверка подключённых каталогов\n"
        "/id — личный Telegram ID\n\n"
        "⚠️ Ни один неофициальный источник не гарантирует все магазины и остатки на полках.")


class Bot:
    def __init__(self, tg: Telegram, adapters: dict[str, Adapter], db: Storage, *,
                 max_stores: int = 40, pace: float = 1.2, allowed: set[int] | None = None,
                 discovery_fetcher=None):
        self.tg, self.adapters, self.db = tg, adapters, db
        self.max_stores, self.pace = max_stores, pace
        # None is deliberately permitted only for direct library/test use.
        # Production main() ALWAYS supplies an explicit set: empty means deny.
        self.allowed = None if allowed is None else set(allowed)
        self.discovery_fetcher = discovery_fetcher or fetch_public_posts

    def send(self, chat: int, message: str, *, ask_location: bool = False):
        self.tg.send(chat, message, ask_location=ask_location)

    def _active_chains(self, uid: int) -> set[str]:
        return self.db.enabled(uid, tuple(self.adapters))

    def handle(self, msg: dict):
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if chat.get("type") != "private" or not isinstance(sender.get("id"), int):
            return
        uid, cid = int(sender["id"]), int(chat["id"])
        if self.allowed is not None and uid not in self.allowed:
            if (msg.get("text") or "").strip().split(" ", 1)[0].lower() == "/id":
                self.send(cid, f"Твой Telegram ID: {uid}. Добавь его в ALLOWED_USER_IDS на сервере.")
            else:
                self.send(cid, "⛔ Доступ закрыт. Для настройки отправь /id и добавь ID в ALLOWED_USER_IDS.")
            return
        self.db.ensure_monitor(uid, DEFAULT_CHANNELS)
        # A forwarded channel post is the quickest way to subscribe from a phone.
        # No private channel IDs, chat tokens or raw forwarded posts are persisted.
        origin = msg.get("forward_origin") or {}
        forwarded_chat = origin.get("chat", {}) if origin.get("type") == "channel" else {}
        if not forwarded_chat:
            forwarded_chat = msg.get("forward_from_chat") or {}
        forwarded_username = normalize_channel((forwarded_chat.get("username") or ""))
        if forwarded_username and forwarded_username not in self.db.monitor_channels(uid):
            if self.db.monitor_add_channel(uid, forwarded_username):
                self.send(cid, f"📡 Канал @{forwarded_username} добавлен в мониторинг. /sources — список.")
            else:
                self.send(cid, "⚠️ Достигнут лимит 24 каналов. /unwatch @канал — удалить старый.")
        loc = msg.get("location")
        if loc:
            self._near(uid, cid, loc.get("latitude"), loc.get("longitude"), 10)
            pending = self.db.pop_pending(uid)
            if pending:
                price = Decimal(pending["target_price"]) if pending["target_price"] else None
                self._search(uid, cid, pending["query"],
                             deal=Deal(query=pending["query"], chain=pending["chain"], target_price=price))
            if self.db.pop_pending_hunt(uid):
                self.hunt_user(uid, manual=True)
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            self.send(cid, "Пришли текст или подпись к скидке: фото без текста пока не распознаю.")
            return
        cmd, _, rest = text.partition(" ")
        cmd, rest = cmd.split("@", 1)[0].lower(), rest.strip()
        if cmd in ("/start", "/help"):
            self.send(cid, HELP)
        elif cmd == "/sources":
            cfg = self.db.monitor_settings(uid)
            channels = self.db.monitor_channels(uid)
            self.send(cid, f"📡 Мониторинг: {'включён' if cfg['enabled'] else 'на паузе'} · "
                       f"порог {cfg['threshold']}% · автообнаружение: {'да' if cfg['auto_channels'] else 'нет'}\nКаналы: " +
                       (", ".join("@" + ch for ch in channels) if channels else "не выбраны") +
                       "\nПроверка периодическая, если бот запущен на сервере. "
                       "Публичная веб-лента может быть недоступна.")
        elif cmd == "/watch":
            ch = normalize_channel(rest)
            if ch is None:
                self.send(cid, "Формат: /watch @public_channel (публичный канал с username)")
            elif self.db.monitor_add_channel(uid, ch):
                self.send(cid, f"✅ @{ch} добавлен. Проверить сейчас: /scan. Это не гарантирует доступность его веб-ленты.")
            else:
                self.send(cid, "Не больше 24 каналов на пользователя. Удали старый: /unwatch @канал")
        elif cmd == "/unwatch":
            ch = normalize_channel(rest)
            if not ch:
                self.send(cid, "Формат: /unwatch @public_channel")
            else:
                self.db.monitor_remove_channel(uid, ch)
                self.send(cid, f"✅ @{ch} исключён из мониторинга.")
        elif cmd == "/autochannels":
            if rest.lower() not in ("on", "off"):
                self.send(cid, "Формат: /autochannels on или /autochannels off")
            else:
                enabled = rest.lower() == "on"
                self.db.monitor_set(uid, auto_channels=enabled)
                self.send(cid, "✅ Автообнаружение связанных публичных каналов " +
                          ("включено" if enabled else "выключено") +
                          ". Добавление только после проверки публикаций в канале.")
        elif cmd == "/alerts":
            try:
                threshold = int(rest)
                if not 30 <= threshold <= 95:
                    raise ValueError()
            except ValueError:
                self.send(cid, "Формат: /alerts 70 (целое число 30–95)")
            else:
                self.db.monitor_set(uid, threshold=threshold)
                self.send(cid, f"✅ Буду сообщать о заявленных скидках от {threshold}% при появлении новых постов.")
        elif cmd == "/pause":
            self.db.monitor_set(uid, enabled=False)
            self.send(cid, "🔕 Автоуведомления отключены. Для возобновления: /resume")
        elif cmd == "/resume":
            self.db.monitor_set(uid, enabled=True)
            self.send(cid, "🔔 Автоуведомления включены. Для проверки сейчас: /scan")
        elif cmd == "/hunt":
            self.hunt_user(uid, manual=True)
        elif cmd == "/watchdeal":
            parsed = parse_deal(rest)
            if not parsed.query or parsed.target_price is None:
                self.send(cid, "Формат: /watchdeal Магнит: кофе 199 ₽ (можно вставить текст поста со скидкой).")
            elif self.db.watch_add(uid, parsed.chain, parsed.query, parsed.target_price):
                chain_label = CHAIN_LABELS.get(parsed.chain, "Все подключённые сети")
                self.send(cid, f"🎯 Отслеживаю: {parsed.query} · {chain_label} · до {parsed.target_price} ₽. "
                               "Когда источник сообщит о наличии, пришлю адрес. /watchlist")
            else:
                self.send(cid, "Не удалось добавить: проверь товар, цену или лимит 20 позиций.")
        elif cmd == "/watchlist":
            watches = self.db.watches(uid)
            if not watches:
                self.send(cid, "Список пуст. Пример: /watchdeal Магнит: кофе 199 ₽")
            else:
                lines = ["🎯 Наблюдаемые товары:"]
                for w in watches:
                    lines.append(f"#{w['id']} {CHAIN_LABELS.get(w['chain'], 'Все сети')}: {w['query']} ≤ {w['max_price']} ₽")
                self.send(cid, "\n".join(lines) + "\nУдалить: /unwatchdeal ID")
        elif cmd == "/unwatchdeal":
            if not rest.isdigit() or not self.db.watch_remove(uid, int(rest)):
                self.send(cid, "Укажи ID из /watchlist. Пример: /unwatchdeal 1")
            else:
                self.send(cid, "✅ Позиция удалена из наблюдения.")
        elif cmd == "/checkwatch":
            self.watch_user(uid, manual=True)
        elif cmd == "/tracks":
            terms = self.db.price_terms(uid)
            self.send(cid, "🔎 Ключевые слова: " + (", ".join(terms) if terms else
                ", ".join(DEFAULT_TERMS) + " (по умолчанию)") +
                "\nПри подключённом /catalog бот просматривает каталог, а не только эти слова.")
        elif cmd == "/track":
            if self.db.price_add_term(uid, rest):
                self.send(cid, f"✅ Добавлено: {rest[:60]}. Проверка: /hunt")
            else:
                self.send(cid, "Формат: /track название товара (3–60 символов, не более 12 слов)")
        elif cmd == "/untrack":
            if not rest:
                self.send(cid, "Формат: /untrack кофе")
            else:
                self.db.price_remove_term(uid, rest)
                self.send(cid, f"✅ Удалено: {rest[:60]}")
        elif cmd == "/scan":
            self.scan_user(uid, manual=True)
        elif cmd == "/id":
            self.send(cid, f"Твой Telegram ID: {uid}")
        elif cmd == "/doctor":
            self.send(cid, render_diagnostics(probe_sources(self.adapters, self.db.stores(uid)), self.adapters))
        elif cmd == "/coverage":
            coverage = self.db.scan_coverage(uid)
            lines = ["📊 Последние выполненные проверки (только сохранённые точки):"]
            names = {"channels": "TG-каналы", "watches": "Целевые цены",
                     "catalogs": "Самостоятельный поиск", "search": "Поиск товара"}
            for row in coverage:
                from datetime import datetime, timezone
                seen = datetime.fromtimestamp(row["updated_at"], timezone.utc).strftime('%d.%m %H:%M UTC')
                lines.append(f"• {names[row['kind']]}: {row['checked']}/{row['total']} проверено; "
                             f"ошибок {row['errors']}; совпадений {row['findings']} · {seen}")
            if not coverage:
                lines.append("Ещё нет завершённых проверок. Используй /scan или /search.")
            lines.append("⚠️ Это охват сохранённых точек/каналов, не всех магазинов города. "
                         "Дата проверки ≠ время обновления остатков продавцом.")
            self.send(cid, "\n".join(lines))
        elif cmd in ("/chains", "/status"):
            lines = ["🔌 Источники сетей:"]
            selected = self._active_chains(uid)
            for slug, adapter in self.adapters.items():
                label = "🟢 источник задан (не проверен)" if adapter.configured else "⚪ источник не подключён"
                if slug not in selected:
                    label = "🔕 отключена пользователем"
                count = len(self.db.stores(uid, chains={slug}))
                lines.append(f"{adapter.label} [{slug}]: {label} · точек: {count}")
            lines.append(f"\nЗа один запрос до {self.max_stores} точек, только сохранённые.\n"
                         "Заданный URL ещё не гарантирует исправную связь. Каталог /catalog необязателен.\n"
                         "Настройка источников: README, переменные окружения сервера.")
            self.send(cid, "\n".join(lines))
        elif cmd in ("/on", "/off"):
            slug = resolve_chain(rest)
            if not slug:
                self.send(cid, "Неверная сеть. Напиши /chains и используй её код, например /on magnit")
                return
            self.db.set_chain(uid, slug, cmd == "/on")
            state = "включена" if cmd == "/on" else "выключена"
            self.send(cid, f"✅ {CHAIN_LABELS[slug]} {state}. Это не подключает источник автоматически — см. /status.")
        elif cmd == "/stores":
            stores = self.db.stores(uid)
            lines = [f"Сохранено магазинов: {len(stores)}"]
            lines.extend(f"{CHAIN_LABELS.get(s['chain'],s['chain'])}: {s['id']} — {s['address']}" for s in stores[:35])
            if len(stores) > 35:
                lines.append("… показаны первые 35")
            self.send(cid, "\n".join(lines))
        elif cmd == "/clearstores":
            slug = resolve_chain(rest) if rest else None
            if rest and not slug:
                self.send(cid, "Неизвестная сеть. /chains — список.")
                return
            self.db.clear(uid, slug)
            self.send(cid, f"✅ Удалены сохранённые точки: {CHAIN_LABELS[slug] if slug else 'всех сетей'}.")
        elif cmd == "/store":
            slug_text, _, remainder = rest.partition(" ")
            slug = resolve_chain(slug_text)
            store_id, _, address = remainder.strip().partition(" ")
            if not slug or not store_id or not address or len(store_id) > 80:
                self.send(cid, "Формат: /store magnit 123 Москва, улица Название, 10")
                return
            self.db.add_stores(uid, [{"chain": slug, "id": store_id, "address": address}])
            self.send(cid, f"✅ Сохранён магазин сети {CHAIN_LABELS[slug]}: {address}.\n"
                           "Важно: ID должен соответствовать именно подключённому каталогу.")
        elif cmd == "/near":
            try:
                args = rest.replace(",", ".").split()
                lat, lon = float(args[0]), float(args[1])
                n = int(args[2]) if len(args) > 2 else 10
            except (ValueError, IndexError):
                self.send(cid, "Формат: /near 55.75 37.61 10")
                return
            self._near(uid, cid, lat, lon, n)
        elif cmd == "/region":
            try:
                args = rest.replace(",", ".").split()
                lat, lon, radius = float(args[0]), float(args[1]), float(args[2])
            except (ValueError, IndexError):
                self.send(cid, "Формат: /region 55.75 37.61 5 (радиус км, 1–10)")
                return
            self._region(uid, cid, lat, lon, radius)
        elif cmd == "/search":
            if not rest:
                self.send(cid, "Формат: /search название товара")
            else:
                self._search(uid, cid, rest)
        elif cmd == "/deal":
            deal = parse_deal(rest)
            if not deal.query:
                self.send(cid, "Формат: /deal Магнит: Кофе 199 ₽ вместо 499 ₽")
            else:
                self._search(uid, cid, deal.query, deal=deal)
        elif cmd.startswith("/"):
            self.send(cid, "Неизвестная команда. /help — список.")
        else:
            deal = parse_deal(text)
            if deal.query:
                self._search(uid, cid, deal.query, deal=deal)
            else:
                self.send(cid, "Не определил название товара. Используй /search название товара.")

    def _near(self, uid: int, cid: int, lat: object, lon: object, n: int):
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            self.send(cid, "Координаты неверные.")
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 1 <= n <= 25):
            self.send(cid, "Ожидаются корректные координаты и от 1 до 25 магазинов на сеть.")
            return
        enabled = self._active_chains(uid)
        active = [a for slug, a in self.adapters.items() if slug in enabled and a.configured]
        if not active:
            self.send(cid, "Нет подключённых каталогов. Списки сетей уже добавлены: /chains, но нужны источники данных.")
            return
        totals, lines = 0, []
        for adapter in active:
            try:
                shops = adapter.nearest(lat, lon, n)
                saved = self.db.add_stores(uid, shops, chain=adapter.slug)
                lines.append(f"{adapter.label}: получено {saved} точек")
                totals += saved
            except ProviderError:
                lines.append(f"{adapter.label}: ошибка подключения")
        self.send(cid, f"📍 Получено точек: {totals}; сохранено всего: {len(self.db.stores(uid))}.\n"+
                  "\n".join(lines)+"\nЭто выборка источников, не полная перепись магазинов.")

    def _region(self, uid: int, cid: int, lat: float, lon: float, radius: float):
        if not (-85 <= lat <= 85 and -180 <= lon <= 180 and 1 <= radius <= 10):
            self.send(cid, "Координаты или радиус неверны (1–10 км, широта ±85°).")
            return
        active = [a for slug, a in self.adapters.items() if slug in self._active_chains(uid) and a.configured]
        if not active:
            self.send(cid, "Не настроены источники каталогов. /status")
            return
        # 3 × 3 sampling grid; bounded to avoid huge scraping load and false coverage claims.
        dy = radius / 111.2
        dx = radius / (111.2 * max(0.1, math.cos(math.radians(lat))))
        points = [(lat + i * dy, lon + j * dx) for i in (-1, 0, 1) for j in (-1, 0, 1)]
        self.send(cid, "🔄 Собираю выборку магазинов из 9 точек района. Это не полный охват города.")
        lines = []
        for adapter in active:
            merged, errors = {}, 0
            for i, (a, b) in enumerate(points):
                if i:
                    time.sleep(max(0.0, self.pace))
                try:
                    for shop in adapter.nearest(round(a, 6), round(b, 6), 15):
                        merged[shop["id"]] = shop
                except ProviderError:
                    errors += 1
            saved = self.db.add_stores(uid, list(merged.values()), chain=adapter.slug)
            lines.append(f"{adapter.label}: {saved} точек, {errors}/9 ошибок")
        self.send(cid, "📍 Выборка сохранена:\n" + "\n".join(lines) +
                  f"\nПоиск ограничен {self.max_stores} сохранёнными точками на запрос.")

    def _search(self, uid: int, cid: int, query: str, *, deal: Deal | None = None):
        if len(query) > 180:
            self.send(cid, "Название слишком длинное. Укажи короткое наименование или штрихкод.")
            return
        selected = self._active_chains(uid)
        # A deal posted about Magnit is not evidence of the same price at Auchan.
        # Default plain /search scans every enabled chain; deal scans its stated chain.
        if deal and deal.chain:
            if deal.chain not in selected:
                self.send(cid, f"Сеть {CHAIN_LABELS[deal.chain]} отключена. Включи: /on {deal.chain}")
                return
            selected = {deal.chain}
        active = {slug for slug in selected if self.adapters[slug].configured}
        if not active:
            if deal and deal.chain:
                self.send(cid, f"⚠️ Скидка относится к сети {CHAIN_LABELS[deal.chain]}, но живой "
                               "источник этой сети НЕ подключён. Не могу подтвердить, в каких магазинах есть товар. "
                               "Проверь /status. Для поиска других сетей: /search " + query)
            else:
                self.send(cid, "⚠️ Перечень сетей добавлен, но ни один источник реальных цен не подключён. "
                               "Бот не будет придумывать остатки. /status и инструкция в README.")
            return
        stores = self.db.stores(uid, chains=active)
        if not stores:
            if deal:
                self.db.set_pending(uid, deal.query, deal.chain,
                                    str(deal.target_price) if deal.target_price is not None else None)
            self.send(cid, "📍 Чтобы проверить магазины, отправь свою геопозицию кнопкой ниже. "
                           "Сохраню ближайшие точки доступных сетей и продолжу поиск. "
                           "Можно также использовать /near 55.75 37.61 10.", ask_location=True)
            return
        self.send(cid, f"🔄 Проверяю до {self.max_stores} магазинов из {len(stores)} сохранённых. "
                       "Результаты будут только из доступных каталогов, не с полок магазинов.")
        result = scan_multi(self.adapters, stores, query, selected_chains=active,
                            max_stores=self.max_stores, pace_seconds=self.pace)
        self.db.scan_record(uid, "search", result.attempted, result.total_saved,
                            result.failed, len(result.results))
        self.send(cid, render_deal(deal, result, self.adapters) if deal
                  else render_multi(query, result, self.adapters))


    def hunt_user(self, uid: int, *, manual: bool = False):
        """Own prices/discounts from configured retailer sources, independent of channels."""
        selected = self._active_chains(uid)
        if manual and any(self.adapters[c].configured for c in selected) and not self.db.stores(uid, chains=selected):
            self.db.set_pending_hunt(uid)
            self.send(uid, "📍 Пришли геопозицию, чтобы собрать ближайшие магазины из подключённых источников. "
                     "После этого запущу поиск скидок. Это не полный охват города.", ask_location=True)
            return None
        report = hunt_prices(
            self.db, self.adapters, uid, selected=selected,
            max_stores=max(1, min(4, int(os.getenv("HUNT_STORES_PER_CYCLE", "3")))),
            max_requests=max(1, min(24, int(os.getenv("HUNT_MAX_REQUESTS", "12")))),
            pause_seconds=self.pace)
        self.db.scan_record(uid, "catalogs", report.stores_checked, report.available_stores,
                            report.errors, len(report.deals))
        if manual or report.deals:
            self.send(uid, render_hunt(report, self.adapters, max_deals=8))
            if report.deals:
                self.db.price_mark_delivered(uid, report.deals)
        return report

    def watch_user(self, uid: int, *, manual: bool = False):
        watches = self.db.watches(uid)
        if not watches:
            if manual:
                self.send(uid, "🎯 Нет товаров для наблюдения. Пример: /watchdeal Магнит: кофе 199 ₽")
            return None
        report = check_watches(self.db, self.adapters, uid, selected=self._active_chains(uid),
                               max_requests=max(1, min(24, int(os.getenv("WATCH_MAX_REQUESTS", "12")))),
                               pause_seconds=self.pace)
        self.db.scan_record(uid, "watches", report.attempted, report.planned,
                            report.errors, len(report.offers))
        if manual or report.alerts:
            self.send(uid, render_watches(report, self.adapters, manual=manual))
            # A failed Telegram send raises; pending alerts remain retryable.
            if report.alerts and not manual:
                self.db.watch_mark_delivered(uid, report.alerts)
        return report

    def scan_user(self, uid: int, *, manual: bool = False, cache: dict | None = None):
        """Read public web feeds. Alerts concern posted claims, not verified store stock."""
        channels = self.db.monitor_channels(uid)
        cfg = self.db.monitor_settings(uid)
        if not channels:
            if manual:
                self.send(uid, "Не выбрано ни одного канала. Добавь /watch @имя_канала")
            return
        cache = cache if cache is not None else {}
        found, errors = [], []
        leads: list[str] = []
        missing = [ch for ch in channels if ch not in cache]
        # Parallel reads avoid 14s * N channels of serialized waiting. Cap
        # concurrency to respect public Telegram pages and limit resource use.
        if missing:
            with ThreadPoolExecutor(max_workers=min(4, len(missing))) as pool:
                futures = {ch: pool.submit(self.discovery_fetcher, ch) for ch in missing}
                for ch, future in futures.items():
                    try:
                        cache[ch] = (future.result(), None)
                    except Exception:
                        # Never echo arbitrary request errors / URLs to the user.
                        cache[ch] = ([], "недоступна веб-лента")
        for channel in channels:
            posts, error = cache[channel]
            if error:
                errors.append("@" + channel)
                continue
            if cfg["auto_channels"]:
                for lead in referenced_channels(posts, limit=12):
                    if lead not in channels and lead not in leads:
                        leads.append(lead)
            had_history = self.db.monitor_has_history(uid, channel)
            for post in posts:
                previously_seen = self.db.monitor_seen(uid, channel, post.post_id)
                if previously_seen and not manual:
                    continue
                candidate = qualify(post, cfg["threshold"], now=int(time.time()))
                # On first scheduled poll, remember existing posts without sending
                # stale history as fresh alerts. /scan shows them on request.
                if candidate and (manual or had_history and not previously_seen):
                    found.append(candidate)
                self.db.monitor_mark_seen(uid, channel, post.post_id)
        # Follow only a couple of references per scheduled scan. A channel must
        # contain at least TWO fresh individual-item deal claims to be adopted.
        # These are still claims, never verified store prices.
        adopted: list[str] = []
        if cfg["auto_channels"] and len(channels) < 24:
            checked = 0
            for lead in leads:
                if checked >= 2 or len(adopted) >= 2:
                    break
                if not self.db.monitor_probe_allowed(uid, lead):
                    continue
                self.db.monitor_mark_probed(uid, lead)
                checked += 1
                if lead not in cache:
                    try:
                        cache[lead] = (self.discovery_fetcher(lead), None)
                    except Exception:
                        cache[lead] = ([], "недоступна веб-лента")
                lead_posts, lead_err = cache[lead]
                if lead_err:
                    continue
                eligible = [post for post in lead_posts if qualify(post, 40, now=int(time.time()), max_age_hours=7*24)]
                if len({post.post_id for post in eligible}) < 2:
                    continue
                if self.db.monitor_add_channel(uid, lead):
                    adopted.append(lead)
                else:
                    break
        found.sort(key=lambda c: (-c.discount_pct, -int(c.post.post_id)))
        self.db.scan_record(uid, "channels", len(channels)-len(errors), len(channels),
                            len(errors), len(found))
        if manual:
            lines = [f"🔎 Проверил {len(channels)} каналов · порог {cfg['threshold']}%."]
            if found:
                lines.append(f"Найдено подходящих публикаций: {len(found)} (показываю до 5).")
                for candidate in found[:5]:
                    lines.append(self._format_candidate(candidate))
            else:
                lines.append("Подходящих публикаций не обнаружено. Это не значит, что скидок нет: "
                             "посты без двух цен и скидки по купонам пропускаю.")
            if adopted:
                lines.append("📡 Добавлены новые каналы: " + ", ".join("@" + ch for ch in adopted))
            if errors:
                lines.append("⚠️ Не открылись: " + ", ".join(errors))
            self.send(uid, "\n\n".join(lines))
        else:
            fresh = {(candidate.post.channel, candidate.post.post_id): candidate for candidate in found}
            for candidate in found:
                self.db.channel_alert_enqueue(uid, candidate.post.channel,
                                              candidate.post.post_id, self._format_candidate(candidate))
            if adopted:
                self.send(uid, "📡 Найдены дополнительные публичные каналы: " +
                          ", ".join("@" + ch for ch in adopted) +
                          ". Публикации по-прежнему требуют проверки реальных цен.")
            # Delivery is tracked only AFTER Telegram confirms success. An outage
            # does not silently lose an alert even if the post was marked seen.
            for item in self.db.channel_alert_pending(uid, 4):
                self.send(uid, item["body"])
                self.db.channel_alert_delivered(uid, item["channel"], item["post_id"])
                # Preserve v0.7 optional source cross-check for freshly seen deals.
                # An old queued message is not falsely marked as live-verified.
                candidate = fresh.get((item["channel"], item["post_id"]))
                if candidate is not None:
                    chain = candidate.deal.chain
                    if chain in self.adapters and self.adapters[chain].configured:
                        stores = self.db.stores(uid, chains={chain})[:6]
                        if stores:
                            report = scan_multi(self.adapters, stores, candidate.deal.query,
                                                selected_chains={chain}, max_stores=6,
                                                pace_seconds=self.pace)
                            self.send(uid, "🔎 Проверка сохранённых магазинов:\n" +
                                      render_deal(candidate.deal, report, self.adapters))

    @staticmethod
    def _format_candidate(candidate) -> str:
        d = candidate.deal
        query = d.query.replace("\n", " ")[:150]
        return (f"🔥 Заявленная скидка ≈{candidate.discount_pct}%\n"
                f"{query}\n"
                f"В посте: {d.target_price} ₽ вместо {d.old_price} ₽\n"
                f"Источник: {candidate.post.url}\n"
                "⚠️ Цена из поста НЕ проверена. Наличие из поста не подтверждено; "
                "если источник магазинов подключён, проверю его отдельно.")


def main():
    load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Укажи TELEGRAM_BOT_TOKEN в секретной переменной окружения", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tg = Telegram(token)
    adapters = load_adapters()
    ids = {int(a) for a in os.getenv("ALLOWED_USER_IDS", "").split(",") if a.strip().isdigit()}
    max_stores = max(1, min(100, int(os.getenv("MAX_STORES_PER_SCAN", "40"))))
    pace = max(1.0, float(os.getenv("REQUEST_PAUSE_SECONDS", "1.2")))
    db = Storage(os.getenv("DATABASE_PATH", "./data/deals.sqlite3"))
    bot = Bot(tg, adapters, db, max_stores=max_stores, pace=pace, allowed=ids)
    tg.method("getMe", {})
    LOG.info("Bot connected. Configured sources: %s", ", ".join(a.slug for a in adapters.values() if a.configured))
    interval = max(300, min(7200, int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "1800"))))
    hunt_interval = max(600, min(21600, int(os.getenv("HUNT_INTERVAL_SECONDS", "3600"))))
    watch_interval = max(300, min(21600, int(os.getenv("WATCH_INTERVAL_SECONDS", "1800"))))

    def scan_channels():
        cache = {}
        for uid in db.monitor_users():
            if uid in ids:
                try:
                    bot.scan_user(uid, cache=cache)
                except Exception:
                    LOG.exception("Public-channel scan failed for one user")

    def scan_watches():
        for uid in db.monitor_users():
            if uid in ids:
                try:
                    bot.watch_user(uid)
                except Exception:
                    LOG.exception("Watchlist scan failed for one user")

    def scan_catalogs():
        for uid in db.monitor_users():
            if uid in ids:
                try:
                    bot.hunt_user(uid)
                except Exception:
                    LOG.exception("Catalog scan failed for one user")

    scheduler = BackgroundScheduler([
        ScheduledJob("channels", interval, scan_channels),
        ScheduledJob("watches", watch_interval, scan_watches),
        ScheduledJob("catalogs", hunt_interval, scan_catalogs),
    ])
    scheduler.start()
    offset = db.get_update_offset()
    failures: dict[int, int] = {}
    LOG.info("Telegram polling active; automatic scans run independently")
    try:
        while True:
            try:
                updates = tg.method("getUpdates", {"offset": offset, "timeout": 25,
                                                   "allowed_updates": ["message"]})
                for update in updates:
                    update_id = int(update["update_id"])
                    msg = update.get("message")
                    try:
                        if msg:
                            bot.handle(msg)
                        failures.pop(update_id, None)
                    except Exception:
                        failures[update_id] = failures.get(update_id, 0) + 1
                        LOG.exception("Failed to handle Telegram update %s (attempt %s)",
                                      update_id, failures[update_id])
                        if failures[update_id] < 3:
                            # Do not acknowledge a transiently failed command.
                            # Next getUpdates call retries the same update.
                            break
                        if msg:
                            try:
                                bot.send(int(msg["chat"]["id"]),
                                         "⚠️ Не удалось обработать запрос после 3 попыток. Попробуй снова.")
                            except Exception:
                                pass
                        failures.pop(update_id, None)
                    # Acknowledge only handled/skipped updates. Persist the cursor
                    # for safe container restarts, before fetching more updates.
                    offset = max(offset, update_id + 1)
                    db.set_update_offset(offset)
            except (OSError, TimeoutError, ValueError) as exc:
                # Never print request URLs with bot token in errors.
                LOG.warning("Telegram polling unavailable (%s)", type(exc).__name__)
                time.sleep(5)
    except KeyboardInterrupt:
        LOG.info("Stopping bot")
    finally:
        scheduler.shutdown()
    return 0



if __name__ == "__main__":
    sys.exit(main())
