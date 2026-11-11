import tempfile
from decimal import Decimal
from pathlib import Path
import unittest

from core import parse_products
from providers import Adapter
from pricehunt import hunt_prices, pct_off, product_identity, render_hunt
from storage import Storage


class CatalogAdapter(Adapter):
    def __init__(self, prices, *, status="есть", old=None):
        super().__init__("magnit", "Магнит", "fixture")
        self.prices = prices
        self.stock = status
        self.old = old
        self.calls = 0
    @property
    def configured(self):
        return True
    def catalog(self, store_id, *, cursor=None, limit=60):
        self.calls += 1
        return ([{"name": "Кофе молотый 250 г", "price": self.prices[0], "old_price": self.old,
                  "status": self.stock, "code": "4601234567890"}], None)
    def search(self, store_id, query):
        return []


class QueryAdapter(CatalogAdapter):
    def catalog(self, store_id, *, cursor=None, limit=60):
        raise NotImplementedError()
    def search(self, store_id, query):
        self.calls += 1
        if query == "кофе":
            return [{"name": "Кофе молотый 250 г", "price": self.prices[0],
                     "old_price": self.old, "status": self.stock, "code": "4601234567890"}]
        return []


class PriceHuntTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "data.sqlite"))
        self.uid = 734
        self.db.ensure_monitor(self.uid)
        self.db.add_stores(self.uid, [{"chain": "magnit", "id": "storeA", "address": "Москва, дом 1"}])
        self.t = 1_800_000_000
    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, ad, now=None):
        return hunt_prices(self.db, {"magnit": ad}, self.uid, selected={"magnit"},
                           max_stores=3, max_requests=12, sleeper=lambda n: None,
                           now=now or self.t)

    def test_price_claim_from_catalog_is_labelled_and_deduped(self):
        ad = CatalogAdapter([Decimal(399)], old=Decimal(2999))
        one = self.scan(ad)
        self.assertEqual(len(one.deals), 1)
        self.assertEqual(one.deals[0].evidence, "retailer_old_price")
        self.assertGreaterEqual(one.deals[0].pct, 86)
        self.assertEqual(len(self.scan(ad).deals), 0)
        self.assertIn("старая цена из каталога (не проверена)", render_hunt(one, {"magnit": ad}))

    def test_history_triggers_only_after_prior_observations(self):
        ad = CatalogAdapter([Decimal(3000)])
        for n in range(3):
            self.assertEqual(len(self.scan(ad, now=self.t + n * 3600).deals), 0)
        ad.prices[0] = Decimal(399)
        hit = self.scan(ad, now=self.t + 3 * 3600)
        self.assertEqual(len(hit.deals), 1)
        self.assertEqual(hit.deals[0].evidence, "history")
        self.assertEqual(hit.deals[0].baseline, Decimal("3000"))

    def test_no_stock_means_no_offer(self):
        ad = CatalogAdapter([Decimal(399)], old=Decimal(2999), status="нет")
        result = self.scan(ad)
        self.assertFalse(result.deals)

    def test_unknown_stock_is_not_claimed_available(self):
        ad = CatalogAdapter([Decimal(399)], old=Decimal(2999), status="не подтверждено")
        result = self.scan(ad)
        self.assertIn("наличие не подтверждено", render_hunt(result, {"magnit": ad}))

    def test_no_catalog_falls_back_to_terms(self):
        self.db.price_add_term(self.uid, "кофе")
        ad = QueryAdapter([Decimal(100)], old=Decimal(500))
        result = self.scan(ad)
        self.assertEqual(result.unsupported_catalogs, 1)
        self.assertEqual(len(result.deals), 1)
        self.assertEqual(ad.calls, 1)

    def test_rotation_prevents_first_store_monopoly(self):
        self.db.add_stores(self.uid, [
            {"chain": "magnit", "id": "storeB", "address": "Москва, дом 2"},
            {"chain": "magnit", "id": "storeC", "address": "Москва, дом 3"}])
        observed = []
        for _ in range(3):
            observed.append(self.db.next_hunt_store_offset(self.uid, 3, 1))
        self.assertEqual(observed, [0,1,2])

    def test_no_live_source_no_deals(self):
        result = hunt_prices(self.db, {}, self.uid)
        self.assertEqual(result.requests, 0)
        self.assertFalse(result.deals)

    def test_prices_and_identifiers(self):
        self.assertEqual(pct_off(Decimal(299), Decimal(1000)), 70)
        self.assertEqual(pct_off(Decimal(1001), Decimal(1000)), 0)
        self.assertEqual(product_identity({"code":"4601234567890", "name":"A"}),
                         product_identity({"code":"4601234567890", "name":"B"}))
        row = parse_products({"products":[{"name":"Кофе", "price":399, "old_price":2999, "stock":4}]})[0]
        self.assertEqual(row["old_price"], Decimal(2999))
        self.assertEqual(row["status"], "есть")

    def test_old_price_wrong_no_alert(self):
        ad = CatalogAdapter([Decimal(500)], old=Decimal(100))
        self.assertEqual(len(self.scan(ad).deals), 0)

    def test_terms_limits(self):
        self.assertFalse(self.db.price_add_term(self.uid, "x"))
        for i in range(12):
            self.assertTrue(self.db.price_add_term(self.uid, f"товар{i}"))
        self.assertFalse(self.db.price_add_term(self.uid, "тринадцатый"))
        self.db.price_remove_term(self.uid, "товар2")
        self.assertTrue(self.db.price_add_term(self.uid, "тринадцатый"))


if __name__ == "__main__":
    unittest.main()
