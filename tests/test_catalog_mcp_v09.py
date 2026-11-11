"""MCP contract tests against the documented Pyaterochka tool response shapes.

These are mocked provider results, NOT proof of live availability at 5ka.ru.
"""
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from core import ProviderError
from pricehunt import hunt_prices
from providers import PyaterochkaAdapter
from storage import Storage


class FakeMCP:
    configured = True

    def __init__(self):
        self.calls = []
        self.categories = [{"id": "100", "name": "Продукты"}, {"id": "200", "name": "Техника"}]
        self.pages = {
            ("100", 0): [
                {"name": "Кофе 250 г", "plu": "001", "price": 399, "old_price": 2999, "is_available": True},
                {"name": "Молоко 1 л", "plu": "002", "price": 100, "is_available": False}],
            ("100", 2): [{"name": "Сыр 100 г", "plu": "003", "price": 89, "is_available": True}],
            ("200", 0): [{"name": "Бритва", "plu": "004", "price": 600, "old_price": 2000, "is_available": None}],
        }

    def call(self, name, args):
        self.calls.append((name, args))
        if name == "list_store_categories":
            return {"categories": self.categories}
        if name == "list_category_products":
            key = (args["category_id"], args["offset"])
            return {"products": self.pages.get(key, [])[:args["limit"]]}
        raise AssertionError(f"Unexpected tool {name}")


class MCPV09Tests(unittest.TestCase):
    def setUp(self):
        self.source = FakeMCP()
        self.adapter = PyaterochkaAdapter("http://127.0.0.1:8765/mcp")
        self.adapter.client = self.source
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name)/"prices.db")
        self.db = Storage(self.db_path)
        self.uid = 455
        self.db.ensure_monitor(self.uid)
        self.db.add_stores(self.uid, [{"chain":"pyaterochka", "id":"S105", "address":"Москва, точка А"}])

    def tearDown(self):
        self.temp.cleanup()

    def test_category_pagination_and_availability(self):
        products, cursor = self.adapter.catalog("S105", limit=2)
        self.assertEqual(cursor, "0:2")
        self.assertEqual(products[0]["old_price"], Decimal(2999))
        self.assertEqual(products[0]["status"], "есть")
        self.assertEqual(products[1]["status"], "нет")
        self.assertEqual(products[0]["code"], "001")
        _, cursor2 = self.adapter.catalog("S105", limit=2, cursor=cursor)
        self.assertEqual(cursor2, "1:0")
        products3, cursor3 = self.adapter.catalog("S105", limit=2, cursor=cursor2)
        self.assertEqual(products3[0]["status"], "не подтверждено")
        self.assertIsNone(cursor3)
        self.assertEqual(self.source.calls[-1][0], "list_category_products")
        self.assertEqual(self.source.calls[-1][1]["category_id"], "200")

    def test_bounded_category_calls(self):
        self.adapter.catalog("S105", limit=2)
        self.assertEqual(len(self.source.calls), 2)
        self.assertEqual(self.source.calls[1][1]["limit"], 2)
        self.assertEqual(self.source.calls[1][1]["offset"], 0)

    def test_cursor_rejects_invalid_format(self):
        with self.assertRaises(ProviderError):
            self.adapter.catalog("S105", cursor="../x")
        self.assertEqual([name for name,_ in self.source.calls], ["list_store_categories"])

    def test_malformed_category_does_not_claim_inventory(self):
        self.source.categories = [{"id":"../../../etc/passwd", "name":"Bad"}]
        with self.assertRaises(ProviderError):
            self.adapter.catalog("S105")

    def test_cursor_persists_across_storage_instances(self):
        self.db.catalog_cursor_save(self.uid,"pyaterochka","S105","1:0")
        reopened = Storage(self.db_path)
        self.assertEqual(reopened.catalog_cursor(self.uid,"pyaterochka","S105"),"1:0")
        reopened.catalog_cursor_save(self.uid,"pyaterochka","S105", None)
        self.assertIsNone(self.db.catalog_cursor(self.uid,"pyaterochka","S105"))

    def test_hunt_advances_categories_and_avoids_repeat(self):
        adapters={"pyaterochka":self.adapter}
        result=hunt_prices(self.db,adapters,self.uid,max_stores=1,max_requests=1,
                           pause_seconds=0,sleeper=lambda n:None,now=1800000000)
        self.assertEqual(result.stores_checked,1)
        self.assertEqual(len(result.deals),1)
        self.assertEqual(result.deals[0].stock,"есть")
        self.assertEqual(self.db.catalog_cursor(self.uid,"pyaterochka","S105"),"1:0")
        result2=hunt_prices(self.db,adapters,self.uid,max_stores=1,max_requests=1,
                            pause_seconds=0,sleeper=lambda n:None,now=1800003600)
        self.assertEqual(result2.products_seen,1)
        self.assertEqual(result2.deals[0].stock,"не подтверждено")
        self.assertIsNone(self.db.catalog_cursor(self.uid,"pyaterochka","S105"))

    def test_cursor_is_not_advanced_on_source_failure(self):
        self.db.catalog_cursor_save(self.uid,"pyaterochka","S105","1:0")
        class Broken:
            configured = True
            def call(self,*args):
                raise ProviderError('source down')
        self.adapter.client=Broken()
        report=hunt_prices(self.db,{"pyaterochka":self.adapter},self.uid,max_stores=1,
                           max_requests=1,pause_seconds=0,sleeper=lambda n:None)
        self.assertEqual(self.db.catalog_cursor(self.uid,"pyaterochka","S105"),"1:0")
        self.assertEqual(report.unsupported_catalogs,1)  # retry not possible within budget


if __name__ == '__main__':
    unittest.main()
