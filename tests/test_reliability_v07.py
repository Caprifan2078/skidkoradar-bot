import tempfile
from pathlib import Path
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
import unittest

from bot import Bot
from providers import JsonCatalogAdapter
from storage import Storage
from test_watch_v07 import FakeAdapter, FakeTelegram
from core import ProviderError

class Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.calls += 1
        self.send_response(302)
        self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/leak')
        self.end_headers()
    def log_message(self,*args): pass

class FailingTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.fail_next = True
    def send(self, chat_id,text,**kw):
        if self.fail_next:
            self.fail_next=False
            raise OSError('telegram temporarily offline')
        return super().send(chat_id,text,**kw)

class ReliabilityTests(unittest.TestCase):
    def test_json_bridge_token_is_not_forwarded_on_redirect(self):
        server=HTTPServer(('127.0.0.1',0),Redirect)
        server.calls=0
        th=Thread(target=server.serve_forever,daemon=True)
        th.start()
        try:
            ad=JsonCatalogAdapter('magnit','Магнит',f'http://127.0.0.1:{server.server_port}',token='private')
            with self.assertRaises(ProviderError):
                ad.search('001','кофе')
            self.assertEqual(server.calls,1, 'redirect must not be followed')
        finally:
            server.shutdown();server.server_close();th.join(timeout=3)

    def test_retry_alert_after_failed_send(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Storage(str(Path(temp)/'db.sqlite'))
            db.ensure_monitor(101)
            db.add_stores(101,[{'chain':'magnit','id':'1','address':'Тестовый адрес'}])
            db.watch_add(101,'magnit','кофе',100)
            tg=FailingTelegram()
            bot=Bot(tg,{'magnit':FakeAdapter()},db,pace=0,allowed={101})
            with self.assertRaises(OSError):
                bot.watch_user(101)
            bot.watch_user(101)
            self.assertIn('Источник сообщает о наличии',tg.messages[-1][1])
            previous=len(tg.messages)
            bot.watch_user(101)
            self.assertEqual(len(tg.messages),previous)

if __name__=='__main__': unittest.main()
