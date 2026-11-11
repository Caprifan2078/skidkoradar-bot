import json
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bot import Bot
from core import extract_query
from providers import (CHAIN_LABELS, DisabledAdapter, JsonCatalogAdapter, PyaterochkaAdapter,
                       load_adapters, render_multi, resolve_chain, scan_multi)
from storage import Storage


class FakeRetailServer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if body['method'] != 'tools/call':
            return self._json({'error': 'unexpected method'})
        tool = body['params']['name']
        args = body['params']['arguments']
        if tool == 'find_nearest_stores':
            data = {'stores': [{'store_id': 'P1', 'address': 'Пятёрочка, адрес 1'}]}
        elif tool == 'search_products':
            data = {'products': [{'name': 'Молоко 1 л', 'price': 100, 'stock': 3}]}
        else:
            data = {'products': []}
        return self._json({'jsonrpc':'2.0', 'id': body['id'], 'result': {
            'content': [{'type':'text', 'text': json.dumps(data, ensure_ascii=False)}]}})

    def do_GET(self):
        path = urlsplit(self.path)
        q = parse_qs(path.query)
        if path.path == '/magnit/stores':
            return self._json({'stores': [{'id': 'M1', 'address': 'Магнит, адрес 2'}]})
        if path.path == '/perekrestok/stores':
            return self._json({'stores': [{'id': 'K1', 'address': 'Перекрёсток, адрес 3'}]})
        if path.path == '/magnit/search':
            assert q['store_id'] == ['M1']
            return self._json({'products': [{'name': 'Молоко 1 л', 'price': 84.5, 'available': True}]})
        if path.path == '/perekrestok/search':
            return self._json({'products': [{'name': 'Молоко 1 л', 'price': 79.90}]})
        if path.path == '/broken/search':
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send(self, chat, message, **kwargs):
        self.messages.append(message)


class MultiChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeRetailServer)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / 'bot.sqlite3'))
        self.tg = FakeTelegram()
        env = {'PYATEROCHKA_MCP_URL': self.url + '/mcp',
               'MAGNIT_SOURCE_URL': self.url + '/magnit',
               'PEREKRESTOK_SOURCE_URL': self.url + '/perekrestok'}
        self.providers = load_adapters(env)
        self.bot = Bot(self.tg, self.providers, self.db, max_stores=10, pace=0, allowed={1234})

    def tearDown(self):
        self.tmp.cleanup()

    def msg(self, text, uid=1234):
        return {'chat': {'id': uid, 'type':'private'}, 'from': {'id': uid}, 'text': text}

    def test_nine_chains_registered_and_defaults(self):
        self.assertEqual(len(CHAIN_LABELS), 9)
        self.assertTrue(self.providers['magnit'].configured)
        self.assertTrue(self.providers['perekrestok'].configured)
        self.assertFalse(self.providers['chizhik'].configured)
        self.assertEqual(resolve_chain('Пятёрочка'), 'pyaterochka')
        self.assertEqual(resolve_chain('Магнит Косметик'), 'magnit_cosmetic')

    def test_multichain_search_price_order_and_unknown_stock(self):
        self.bot.handle(self.msg('/near 55.75 37.61 10'))
        shops = self.db.stores(1234)
        self.assertEqual({x['chain'] for x in shops}, {'pyaterochka', 'perekrestok', 'magnit'})
        self.bot.handle(self.msg('/search молоко'))
        report = self.tg.messages[-1]
        self.assertIn('79.90 ₽', report)
        self.assertIn('84.50 ₽', report)
        self.assertIn('100.00 ₽', report)
        self.assertIn('остаток не подтверждён', report)
        self.assertTrue(report.index('79.90') < report.index('84.50') < report.index('100.00'))

    def test_disable_only_one_chain(self):
        self.bot.handle(self.msg('/near 55.75 37.61 5'))
        self.bot.handle(self.msg('/off magnit'))
        self.bot.handle(self.msg('/search молоко'))
        out = self.tg.messages[-1]
        self.assertNotIn('84.50', out)
        self.assertIn('79.90', out)
        self.assertIn('100.00', out)
        self.bot.handle(self.msg('/on magnit'))
        self.assertIn('magnit', self.db.enabled(1234, tuple(self.providers)))

    def test_user_storage_isolated_and_authorization(self):
        self.bot.handle(self.msg('/near 55.75 37.61 5'))
        self.assertFalse(self.db.stores(9999))
        self.bot.handle(self.msg('/search молоко', uid=9999))
        self.assertIn('⛔', self.tg.messages[-1])

    def test_unconfigured_chains_never_claim_live_search(self):
        only_disabled = {slug: DisabledAdapter(slug, label) for slug, label in CHAIN_LABELS.items()}
        bot = Bot(self.tg, only_disabled, self.db)
        bot.handle(self.msg('/search молоко'))
        self.assertIn('ни один источник', self.tg.messages[-1])

    def test_bridge_failure_no_false_availability(self):
        broken = JsonCatalogAdapter('magnit', 'Магнит', self.url + '/broken')
        self.db.add_stores(1234, [{'chain': 'magnit', 'id': 'M2', 'address': 'Нет связи'}])
        adapters = dict(self.providers, magnit=broken)
        report = scan_multi(adapters, self.db.stores(1234), 'молоко', pace_seconds=0, sleeper=lambda _: None)
        self.assertEqual(report.failed, 1)
        self.assertFalse(report.results)
        self.assertIn('Ошибок источников: 1', render_multi('молоко', report, adapters))

    def test_extraction(self):
        self.assertEqual(extract_query('🔥 Молоко 1 л 90 ₽ вместо 150 ₽'), 'Молоко 1 л')
        self.assertEqual(extract_query('Штрихкод: 4601234567890'), '4601234567890')

    def test_old_db_upgrade(self):
        path = Path(self.tmp.name) / 'legacy.sqlite3'
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE stores(user_id INTEGER,store_id TEXT,address TEXT,PRIMARY KEY(user_id,store_id))')
        con.execute('INSERT INTO stores VALUES(1234,\'S105\',\'Старый магазин\')')
        con.commit()
        con.close()
        migrated = Storage(str(path))
        self.assertEqual(migrated.stores(1234), [{'chain':'pyaterochka','id':'S105','address':'Старый магазин'}])
        migrated.clear(1234)
        migrated = Storage(str(path))
        self.assertEqual(migrated.stores(1234), [])

    def test_chain_and_store_count(self):
        self.bot.handle(self.msg('/store magnit 123 Москва, ул. Примерная'))
        self.bot.handle(self.msg('/store perekrestok 123 Москва, ул. Примерная'))
        self.assertEqual(len(self.db.stores(1234)), 2)  # same ID in distinct chains
        self.bot.handle(self.msg('/clearstores magnit'))
        self.assertEqual(len(self.db.stores(1234)), 1)

    def test_https_required_for_remote_bridges(self):
        with self.assertRaises(ValueError):
            JsonCatalogAdapter('magnit','Магнит','http://not-local.example.net')


if __name__ == '__main__':
    unittest.main()
