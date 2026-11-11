"""End-to-end offline integration: a real local HTTP bridge, not real retail data."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
from pathlib import Path
import threading
import unittest

from bot import Bot
from providers import load_adapters
from storage import Storage


class Bridge(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path
        if route == '/magnit/stores':
            data = {'stores': [{'id': 'm1', 'address': 'Москва, тестовый магазин'}]}
        elif route == '/magnit/catalog':
            data = {'products': [{'name':'Кофе 250 г', 'barcode':'4601234567890',
                                  'price':399, 'old_price':2999, 'stock':2}],
                    'next_cursor': None}
        elif route == '/magnit/search':
            data = {'products': [{'name':'Кофе 250 г', 'price':399, 'stock':0}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        content = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class FakeTelegram:
    def __init__(self):
        self.messages=[]
    def send(self, cid, text, **kwargs):
        self.messages.append(text)


class HuntHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1', 0), Bridge)
        cls.thread=threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Storage(str(Path(self.tmp.name)/'db.sqlite3'))
        self.tg=FakeTelegram()
        self.ad=load_adapters({'MAGNIT_SOURCE_URL':self.base+'/magnit'})
        self.bot=Bot(self.tg, self.ad, self.db, pace=0, allowed={12})
    def tearDown(self):
        self.tmp.cleanup()
    def send(self, text):
        self.bot.handle({'chat':{'id':12, 'type':'private'},'from':{'id':12},'text':text})

    def test_catalog_hunt_end_to_end(self):
        self.send('/near 55.75 37.61 1')
        self.assertEqual(len(self.db.stores(12)), 1)
        self.send('/hunt')
        answer=self.tg.messages[-1]
        self.assertIn('Кофе 250 г',answer)
        self.assertIn('399',answer)
        self.assertIn('−87%',answer)
        self.assertIn('источник сообщает о наличии',answer)
        self.assertIn('старая цена из каталога (не проверена)', answer)
        self.send('/hunt')
        self.assertIn('не найдено',self.tg.messages[-1])

    def test_manual_search_displays_no_stock(self):
        self.send('/near 55.75 37.61 1')
        self.send('/search кофе')
        self.assertIn('источник сообщает, что нет', self.tg.messages[-1])


if __name__=='__main__':
    unittest.main()
