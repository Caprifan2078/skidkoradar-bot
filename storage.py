"""Small SQLite store for independently selected retailer stores per Telegram user."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Storage:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS stores_v2 (
                    user_id INTEGER NOT NULL,
                    chain TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    address TEXT NOT NULL,
                    PRIMARY KEY (user_id, chain, store_id)
                );
                CREATE TABLE IF NOT EXISTS pending_deals (
                    user_id INTEGER PRIMARY KEY,
                    query TEXT NOT NULL,
                    chain TEXT,
                    target_price TEXT,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_settings (
                    user_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    threshold INTEGER NOT NULL CHECK(threshold BETWEEN 30 AND 95)
                );
                CREATE TABLE IF NOT EXISTS monitored_channels (
                    user_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    PRIMARY KEY(user_id,channel)
                );
                CREATE TABLE IF NOT EXISTS seen_channel_posts (
                    user_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    seen_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id,channel,post_id)
                );
                CREATE TABLE IF NOT EXISTS source_probes (
                    user_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    last_checked INTEGER NOT NULL,
                    PRIMARY KEY (user_id, channel)
                );
                CREATE TABLE IF NOT EXISTS pending_hunt (
                    user_id INTEGER PRIMARY KEY,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS price_terms (
                    user_id INTEGER NOT NULL,
                    term TEXT NOT NULL,
                    PRIMARY KEY(user_id,term)
                );
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chain TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    price TEXT NOT NULL,
                    seen_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS history_lookup ON
                    price_history(user_id, chain, store_id, product_key, seen_at);
                CREATE TABLE IF NOT EXISTS price_alerts (
                    user_id INTEGER NOT NULL,
                    chain TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    alert_price TEXT NOT NULL,
                    seen_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id,chain,store_id,product_key,alert_price)
                );
                CREATE TABLE IF NOT EXISTS hunt_cursors (
                    user_id INTEGER PRIMARY KEY,
                    cursor INTEGER NOT NULL,
                    term_cursor INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS catalog_cursors (
                    user_id INTEGER NOT NULL,
                    chain TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id,chain,store_id)
                );
                CREATE TABLE IF NOT EXISTS deal_watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chain TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL,
                    max_price TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(user_id,chain,query,max_price)
                );
                CREATE TABLE IF NOT EXISTS watch_cursors (
                    user_id INTEGER PRIMARY KEY,
                    cursor INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS watch_alerts (
                    user_id INTEGER NOT NULL,
                    watch_id INTEGER NOT NULL,
                    chain TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    alert_price TEXT NOT NULL,
                    seen_at INTEGER NOT NULL,
                    delivered_at INTEGER,
                    PRIMARY KEY(user_id,watch_id,chain,store_id,product_key,alert_price)
                );
                CREATE TABLE IF NOT EXISTS channel_alert_outbox (
                    user_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    delivered_at INTEGER,
                    PRIMARY KEY(user_id,channel,post_id)
                );
                CREATE TABLE IF NOT EXISTS scan_audit (
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    checked INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    errors INTEGER NOT NULL,
                    findings INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id, kind)
                );
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chain_prefs (
                    user_id INTEGER NOT NULL,
                    chain TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    PRIMARY KEY (user_id, chain)
                );
            """)
            # Retry unsent watch alerts across crashes / network failures.
            watch_cols = {r[1] for r in db.execute("PRAGMA table_info(watch_alerts)")}
            if "delivered_at" not in watch_cols:
                db.execute("ALTER TABLE watch_alerts ADD COLUMN delivered_at INTEGER")
            # Prior v0.7 alerts were already marked when discovered, so retain
            # their previous dedup behavior when migrating to delivery receipts.
            price_cols = {r[1] for r in db.execute("PRAGMA table_info(price_alerts)")}
            if "delivered_at" not in price_cols:
                db.execute("ALTER TABLE price_alerts ADD COLUMN delivered_at INTEGER")
                db.execute("UPDATE price_alerts SET delivered_at=seen_at")
            # Existing databases get the v0.5 discovery preference safely.
            cols = {r[1] for r in db.execute("PRAGMA table_info(monitor_settings)")}
            if "auto_channels" not in cols:
                db.execute("ALTER TABLE monitor_settings ADD COLUMN auto_channels INTEGER NOT NULL DEFAULT 1")
            # One-time migration of v0.1 saved shops: all previous shops were 5ka.
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 2:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stores'").fetchone():
                    db.execute("""INSERT OR IGNORE INTO stores_v2(user_id,chain,store_id,address)
                                  SELECT user_id,'pyaterochka',store_id,address FROM stores""")
                    db.execute("DROP TABLE stores")
                db.execute("PRAGMA user_version=2")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add_stores(self, user: int, stores: list[dict], *, chain: str | None = None) -> int:
        to_save = []
        for s in stores:
            ch = (chain or s.get("chain", "pyaterochka")).strip().lower()
            sid = str(s["id"]).strip()[:80]
            address = str(s["address"]).strip()[:220]
            if ch and sid and address:
                to_save.append((user, ch, sid, address))
        with self._connect() as db:
            db.executemany("""INSERT INTO stores_v2 (user_id,chain,store_id,address) VALUES (?,?,?,?)
                ON CONFLICT(user_id,chain,store_id) DO UPDATE SET address=excluded.address""", to_save)
        return len(to_save)

    def stores(self, user: int, *, chains: set[str] | None = None) -> list[dict]:
        with self._connect() as db:
            data = [{"chain": r["chain"], "id": r["store_id"], "address": r["address"]}
                    for r in db.execute("""SELECT chain,store_id,address FROM stores_v2
                                          WHERE user_id=? ORDER BY chain,address""", (user,))]
        return [s for s in data if chains is None or s["chain"] in chains]

    def clear(self, user: int, chain: str | None = None):
        with self._connect() as db:
            if chain:
                db.execute("DELETE FROM stores_v2 WHERE user_id=? AND chain=?", (user, chain))
            else:
                db.execute("DELETE FROM stores_v2 WHERE user_id=?", (user,))

    def set_chain(self, user: int, chain: str, enabled: bool):
        with self._connect() as db:
            db.execute("""INSERT INTO chain_prefs(user_id,chain,enabled) VALUES(?,?,?)
                ON CONFLICT(user_id,chain) DO UPDATE SET enabled=excluded.enabled""",
                (user, chain, int(enabled)))

    def enabled(self, user: int, available: list[str] | tuple[str, ...]) -> set[str]:
        selected = set(available)
        with self._connect() as db:
            for r in db.execute("SELECT chain,enabled FROM chain_prefs WHERE user_id=?", (user,)):
                if not r["enabled"]:
                    selected.discard(r["chain"])
                elif r["chain"] in available:
                    selected.add(r["chain"])
        return selected

    def set_pending(self, user: int, query: str, chain: str | None, target_price: str | None):
        """Store minimal details (not forwarded post text), expires after one day."""
        with self._connect() as db:
            db.execute("""INSERT INTO pending_deals(user_id, query, chain, target_price, created_at)
                          VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                          query=excluded.query,chain=excluded.chain,
                          target_price=excluded.target_price,created_at=excluded.created_at""",
                       (user, query[:180], chain, target_price, int(time.time())))

    def pop_pending(self, user: int, *, max_age: int = 86400) -> dict | None:
        """Read-and-clear: don't unexpectedly rerun an old post next time location is shared."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM pending_deals WHERE user_id=?", (user,)).fetchone()
            db.execute("DELETE FROM pending_deals WHERE user_id=?", (user,))
        if row is None or int(time.time()) - row["created_at"] > max_age:
            return None
        return {"query": row["query"], "chain": row["chain"], "target_price": row["target_price"]}

    # v0.4: User-controlled public deal channels and idempotent alerts.
    def ensure_monitor(self, user: int, defaults: tuple[str, ...] = ()):
        with self._connect() as db:
            cur = db.execute("SELECT 1 FROM monitor_settings WHERE user_id=?", (user,))
            if cur.fetchone() is None:
                db.execute("INSERT INTO monitor_settings(user_id,enabled,threshold) VALUES(?,1,70)", (user,))
                db.executemany("INSERT OR IGNORE INTO monitored_channels(user_id,channel) VALUES(?,?)",
                               [(user, ch) for ch in defaults])

    def monitor_settings(self, user: int) -> dict:
        with self._connect() as db:
            r = db.execute("SELECT enabled,threshold,auto_channels FROM monitor_settings WHERE user_id=?", (user,)).fetchone()
        return {"enabled": bool(r["enabled"]), "threshold": int(r["threshold"]), "auto_channels": bool(r["auto_channels"])} if r else {"enabled": False, "threshold": 70, "auto_channels": True}

    def monitor_users(self) -> list[int]:
        with self._connect() as db:
            return [int(r["user_id"]) for r in db.execute(
                "SELECT user_id FROM monitor_settings WHERE enabled=1 ORDER BY user_id LIMIT 100")]

    def monitor_channels(self, user: int) -> list[str]:
        with self._connect() as db:
            return [r["channel"] for r in db.execute(
                "SELECT channel FROM monitored_channels WHERE user_id=? ORDER BY channel", (user,))]

    def monitor_add_channel(self, user: int, channel: str, max_channels: int = 24) -> bool:
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM monitored_channels WHERE user_id=?", (user,)).fetchone()[0]
            exists = db.execute("SELECT 1 FROM monitored_channels WHERE user_id=? AND channel=?",
                                (user, channel)).fetchone() is not None
            if count >= max_channels and not exists:
                return False
            db.execute("INSERT OR IGNORE INTO monitored_channels(user_id,channel) VALUES(?,?)", (user,channel))
            return True

    def monitor_remove_channel(self, user: int, channel: str):
        with self._connect() as db:
            db.execute("DELETE FROM monitored_channels WHERE user_id=? AND channel=?", (user,channel))

    def monitor_set(self, user: int, *, enabled: bool | None = None, threshold: int | None = None, auto_channels: bool | None = None):
        with self._connect() as db:
            if enabled is not None:
                db.execute("UPDATE monitor_settings SET enabled=? WHERE user_id=?", (int(enabled),user))
            if threshold is not None:
                db.execute("UPDATE monitor_settings SET threshold=? WHERE user_id=?", (threshold,user))
            if auto_channels is not None:
                db.execute("UPDATE monitor_settings SET auto_channels=? WHERE user_id=?", (int(auto_channels),user))

    def monitor_seen(self, user: int, channel: str, post_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM seen_channel_posts WHERE user_id=? AND channel=? AND post_id=?",
                              (user,channel,post_id)).fetchone() is not None

    def monitor_mark_seen(self, user: int, channel: str, post_id: str):
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO seen_channel_posts(user_id,channel,post_id,seen_at) VALUES(?,?,?,?)",
                       (user,channel,post_id,int(time.time())))
            db.execute("DELETE FROM seen_channel_posts WHERE user_id=? AND seen_at<?",
                       (user,int(time.time())-30*86400))

    def monitor_has_history(self, user: int, channel: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM seen_channel_posts WHERE user_id=? AND channel=? LIMIT 1",
                              (user,channel)).fetchone() is not None


    def monitor_probe_allowed(self, user: int, channel: str, *, cool_down: int = 7*86400) -> bool:
        """Limit candidate feed verification to once per week for each user."""
        with self._connect() as db:
            r = db.execute("SELECT last_checked FROM source_probes WHERE user_id=? AND channel=?",
                           (user, channel)).fetchone()
        return r is None or int(time.time()) - r["last_checked"] >= cool_down

    def monitor_mark_probed(self, user: int, channel: str):
        with self._connect() as db:
            db.execute("""INSERT INTO source_probes(user_id,channel,last_checked) VALUES(?,?,?)
                          ON CONFLICT(user_id,channel) DO UPDATE SET last_checked=excluded.last_checked""",
                       (user, channel, int(time.time())))
            db.execute("DELETE FROM source_probes WHERE last_checked<?",
                       (int(time.time()) - 30*86400,))

    # v0.6: independent retailer-catalogue monitoring (NOT Telegram channel claims).
    def price_terms(self, user: int) -> list[str]:
        with self._connect() as db:
            return [r["term"] for r in db.execute(
                "SELECT term FROM price_terms WHERE user_id=? ORDER BY term LIMIT 12", (user,))]

    def price_add_term(self, user: int, term: str) -> bool:
        term = " ".join(term.lower().split()).strip()
        if not (3 <= len(term) <= 60):
            return False
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM price_terms WHERE user_id=?", (user,)).fetchone()[0]
            exists = db.execute("SELECT 1 FROM price_terms WHERE user_id=? AND term=?", (user, term)).fetchone()
            if count >= 12 and not exists:
                return False
            db.execute("INSERT OR IGNORE INTO price_terms(user_id,term) VALUES(?,?)", (user, term))
        return True

    def price_remove_term(self, user: int, term: str):
        with self._connect() as db:
            db.execute("DELETE FROM price_terms WHERE user_id=? AND term=?", (user, term.lower().strip()))

    def catalog_cursor(self, user: int, chain: str, store_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT cursor FROM catalog_cursors WHERE user_id=? AND chain=? AND store_id=?",
                             (user, chain, store_id)).fetchone()
            return row["cursor"] if row else None

    def catalog_cursor_save(self, user: int, chain: str, store_id: str, cursor: str | None):
        """Commit scan position only after a successful source response."""
        if cursor is not None and (len(cursor) > 120 or any(c.isspace() for c in cursor)):
            raise ValueError("Bad catalog cursor")
        with self._connect() as db:
            if cursor is None:
                db.execute("DELETE FROM catalog_cursors WHERE user_id=? AND chain=? AND store_id=?",
                           (user, chain, store_id))
            else:
                db.execute("INSERT INTO catalog_cursors(user_id,chain,store_id,cursor,updated_at) "
                           "VALUES(?,?,?,?,?) ON CONFLICT(user_id,chain,store_id) "
                           "DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                           (user,chain,store_id,cursor,int(time.time())))

    def next_hunt_store_offset(self, user: int, count: int, increment: int) -> int:
        if count <= 0:
            return 0
        with self._connect() as db:
            row = db.execute("SELECT cursor FROM hunt_cursors WHERE user_id=?", (user,)).fetchone()
            start = (row["cursor"] if row else 0) % count
            db.execute("INSERT INTO hunt_cursors(user_id,cursor) VALUES(?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET cursor=excluded.cursor",
                       (user, (start + min(count, increment)) % count))
        return start

    def next_hunt_term_offset(self, user: int, count: int, increment: int) -> int:
        if count <= 0:
            return 0
        with self._connect() as db:
            row = db.execute("SELECT term_cursor FROM hunt_cursors WHERE user_id=?", (user,)).fetchone()
            start = (row["term_cursor"] if row else 0) % count
            db.execute("INSERT INTO hunt_cursors(user_id,cursor,term_cursor) VALUES(?,0,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET term_cursor=excluded.term_cursor",
                       (user, (start + min(count, increment)) % count))
        return start

    def price_reference(self, user: int, chain: str, store_id: str, product_key: str,
                        *, now: int | None = None):
        from decimal import Decimal, InvalidOperation
        from statistics import median
        now = int(time.time()) if now is None else now
        with self._connect() as db:
            rows = db.execute("""SELECT price FROM price_history WHERE
                  user_id=? AND chain=? AND store_id=? AND product_key=? AND seen_at>=?
                  ORDER BY seen_at DESC,id DESC LIMIT 12""",
                (user, chain, store_id, product_key, now-30*86400)).fetchall()
        try:
            prices = [Decimal(r["price"]) for r in rows]
        except (InvalidOperation, ValueError):
            return None, None
        if len(prices) < 3:
            return None, None
        # Robust to one spurious outlier. Compare with a median of earlier readings.
        return Decimal(median(prices)), "history"

    def price_observe(self, user: int, chain: str, store_id: str, product_key: str,
                      price, *, now: int | None = None):
        now = int(time.time()) if now is None else now
        with self._connect() as db:
            # Deduplicate repeated searches during the same minute.
            prev = db.execute("""SELECT price,seen_at FROM price_history WHERE
                user_id=? AND chain=? AND store_id=? AND product_key=?
                ORDER BY seen_at DESC,id DESC LIMIT 1""",
                (user, chain, store_id, product_key)).fetchone()
            if prev and prev["price"] == str(price) and now - prev["seen_at"] < 60:
                return
            db.execute("""INSERT INTO price_history(
                user_id,chain,store_id,product_key,price,seen_at) VALUES(?,?,?,?,?,?)""",
                (user, chain, store_id, product_key, str(price), now))
            db.execute("DELETE FROM price_history WHERE seen_at<?", (now-45*86400,))

    def price_new_alert(self, user: int, chain: str, store_id: str, product_key: str,
                        price, *, now: int | None = None) -> bool:
        """Reserve alert for sending; retry after 5 min if delivery was not confirmed."""
        now = int(time.time()) if now is None else int(now)
        args = (user, chain, store_id, product_key, str(price))
        with self._connect() as db:
            row = db.execute("SELECT seen_at,delivered_at FROM price_alerts WHERE "
                             "user_id=? AND chain=? AND store_id=? AND product_key=? AND alert_price=?",
                             args).fetchone()
            if row:
                if row["delivered_at"] is not None and row["seen_at"] >= now - 14*86400:
                    return False
                if row["delivered_at"] is None and row["seen_at"] >= now - 300:
                    return False
            db.execute("INSERT INTO price_alerts(user_id,chain,store_id,product_key,alert_price,seen_at,delivered_at) "
                       "VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(user_id,chain,store_id,product_key,alert_price) "
                       "DO UPDATE SET seen_at=excluded.seen_at, delivered_at=NULL", args + (now,))
            db.execute("DELETE FROM price_alerts WHERE seen_at<?", (now-60*86400,))
        return True

    def price_mark_delivered(self, user: int, deals: list, *, now: int | None = None):
        from pricehunt import product_identity
        now = int(time.time()) if now is None else int(now)
        with self._connect() as db:
            for d in deals:
                key = product_identity({"name": d.product_name, "code": d.code})
                db.execute("UPDATE price_alerts SET delivered_at=? WHERE user_id=? AND chain=? "
                           "AND store_id=? AND product_key=? AND alert_price=?",
                           (now, user, d.chain, d.store_id, key, str(d.price)))

    def set_pending_hunt(self, user: int):
        with self._connect() as db:
            db.execute("INSERT INTO pending_hunt(user_id,created_at) VALUES(?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET created_at=excluded.created_at",
                       (user, int(time.time())))

    def pop_pending_hunt(self, user: int, *, max_age: int = 3600) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT created_at FROM pending_hunt WHERE user_id=?", (user,)).fetchone()
            db.execute("DELETE FROM pending_hunt WHERE user_id=?", (user,))
        return bool(row is not None and time.time() - row["created_at"] <= max_age)

    # v0.7: user-selected prices across all saved and connected stores.
    def watches(self, user: int) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT id,chain,query,max_price FROM deal_watches WHERE user_id=? ORDER BY id", (user,))
            return [dict(row) for row in rows]

    def watch_add(self, user: int, chain: str | None, query: str, price) -> bool:
        from decimal import Decimal, InvalidOperation
        query = " ".join(query.strip().split())
        try:
            price = Decimal(str(price))
        except InvalidOperation:
            return False
        if not (3 <= len(query) <= 120 and Decimal("0") < price <= Decimal("10000000")):
            return False
        chain = chain or ""
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM deal_watches WHERE user_id=?", (user,)).fetchone()[0]
            exists = db.execute("SELECT 1 FROM deal_watches WHERE user_id=? AND chain=? AND query=? AND max_price=?",
                                (user, chain, query, str(price))).fetchone()
            if count >= 20 and not exists:
                return False
            db.execute("INSERT OR IGNORE INTO deal_watches(user_id,chain,query,max_price,created_at) VALUES(?,?,?,?,?)",
                       (user, chain, query, str(price), int(time.time())))
        return True

    def watch_remove(self, user: int, watch_id: int) -> bool:
        with self._connect() as db:
            cur = db.execute("DELETE FROM deal_watches WHERE user_id=? AND id=?", (user, watch_id))
            db.execute("DELETE FROM watch_alerts WHERE user_id=? AND watch_id=?", (user,watch_id))
            return cur.rowcount > 0

    def next_watch_offset(self, user: int, count: int, step: int) -> int:
        if count <= 0:
            return 0
        with self._connect() as db:
            row = db.execute("SELECT cursor FROM watch_cursors WHERE user_id=?", (user,)).fetchone()
            start = (row["cursor"] if row else 0) % count
            db.execute("INSERT INTO watch_cursors(user_id,cursor) VALUES(?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET cursor=excluded.cursor",
                       (user, (start + min(count, step)) % count))
        return start

    def watch_new_alert(self, user: int, watch_id: int, chain: str, store_id: str,
                        product_key: str, price: str, *, now: int | None = None) -> bool:
        """Queue an alert; keep it eligible until a Telegram send succeeds."""
        now = int(time.time()) if now is None else now
        args = (user,watch_id,chain,store_id,product_key,price)
        with self._connect() as db:
            row = db.execute("SELECT seen_at,delivered_at FROM watch_alerts WHERE user_id=? AND watch_id=? "
                             "AND chain=? AND store_id=? AND product_key=? AND alert_price=?", args).fetchone()
            if row and row["delivered_at"] is not None and row["seen_at"] >= now-14*86400:
                return False
            db.execute("INSERT INTO watch_alerts(user_id,watch_id,chain,store_id,product_key,alert_price,seen_at,delivered_at) "
                       "VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(user_id,watch_id,chain,store_id,product_key,alert_price) "
                       "DO UPDATE SET seen_at=excluded.seen_at, delivered_at=NULL", args+(now,))
            db.execute("DELETE FROM watch_alerts WHERE seen_at<?", (now-60*86400,))
        return True

    def watch_mark_delivered(self, user: int, offers: list[dict]):
        with self._connect() as db:
            for offer in offers:
                db.execute("UPDATE watch_alerts SET delivered_at=? WHERE user_id=? AND watch_id=? AND "
                           "chain=? AND store_id=? AND product_key=? AND alert_price=?",
                           (int(time.time()),user,offer["watch_id"],offer["chain"],offer["store_id"],
                            offer["product_key"],str(offer["price"])))

    # v0.8: durable outgoing channel notifications; dedup by post ID.
    def channel_alert_enqueue(self, user: int, channel: str, post_id: str, body: str, *, now=None):
        now = int(time.time()) if now is None else int(now)
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO channel_alert_outbox(user_id,channel,post_id,body,created_at) "
                       "VALUES(?,?,?,?,?)", (user,channel,post_id,body[:3900],now))
            db.execute("DELETE FROM channel_alert_outbox WHERE created_at<? AND delivered_at IS NOT NULL",
                       (now-30*86400,))

    def channel_alert_pending(self, user: int, limit: int = 4) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT channel,post_id,body FROM channel_alert_outbox WHERE user_id=? AND delivered_at IS NULL "
                "ORDER BY created_at,channel,post_id LIMIT ?", (user,limit))]

    def channel_alert_delivered(self, user: int, channel: str, post_id: str):
        with self._connect() as db:
            db.execute("UPDATE channel_alert_outbox SET delivered_at=? WHERE user_id=? AND "
                       "channel=? AND post_id=? AND delivered_at IS NULL", (int(time.time()),user,channel,post_id))

    def get_update_offset(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT value FROM app_state WHERE key='telegram_offset'").fetchone()
        return int(row['value']) if row else 0

    def set_update_offset(self, offset: int):
        with self._connect() as db:
            db.execute("INSERT INTO app_state(key,value) VALUES('telegram_offset',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))

    # v0.8: last real scan coverage for the user, not city-wide marketing claims.
    def scan_record(self, user: int, kind: str, checked: int, total: int,
                    errors: int, findings: int, *, now: int | None = None):
        if kind not in ('search','channels','watches','catalogs'):
            raise ValueError('Unsupported audit kind')
        now = int(time.time()) if now is None else int(now)
        with self._connect() as db:
            db.execute("INSERT INTO scan_audit(user_id,kind,checked,total,errors,findings,updated_at) "
                       "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,kind) DO UPDATE SET "
                       "checked=excluded.checked,total=excluded.total,errors=excluded.errors,"
                       "findings=excluded.findings,updated_at=excluded.updated_at",
                       (user,kind,max(0,checked),max(0,total),max(0,errors),max(0,findings),now))

    def scan_coverage(self, user: int) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT kind,checked,total,errors,findings,updated_at "
                                              "FROM scan_audit WHERE user_id=? ORDER BY kind",(user,))]
