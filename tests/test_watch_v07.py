import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from bot import Bot
from core import ProviderError
from providers import Adapter
from storage import Storage
from watchlist import check_watches, render_watches


class FakeAdapter(Adapter):
    def __init__(self, stock="есть", price="99", fail=False):
        super().__init__("magnit", "Магнит", "fixture")
        self.stock, self.price, self.fail = stock, Decimal(price), fail
        self.calls = []

    @property
    def configured(self):
        return True

    def search(self, store_id, query):
        self.calls.append((store_id,query))
        if self.fail:
            raise ProviderError("error")
        return [{"name": "Кофе тест", "code": "4600000000001", "price": self.price,
                 "status": self.stock}]


class FakeTelegram:
    def __init__(self):
        self.messages=[]
    def send(self, chat_id, text, **kwargs):
        self.messages.append((chat_id,text))


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Storage(str(Path(self.temp.name)/"db.sqlite3"))
        self.db.ensure_monitor(1)
        self.ad=FakeAdapter()
        self.adapters={"magnit":self.ad}
        self.db.add_stores(1, [{"chain":"magnit", "id":str(i), "address":f"Москва, адрес {i}"} for i in range(1,5)])
    def tearDown(self):
        self.temp.cleanup()

    def scan(self, **kwargs):
        return check_watches(self.db,self.adapters,1,max_requests=2,
                             sleeper=lambda n:None,now=1800000000,**kwargs)

    def test_round_robin_across_all_stores_and_no_duplicate_alerts(self):
        self.assertTrue(self.db.watch_add(1,"magnit","Кофе",100))
        a=self.scan()
        b=self.scan()
        self.assertEqual(a.attempted,2)
        self.assertEqual(b.attempted,2)
        self.assertEqual([x[0] for x in self.ad.calls], ['1','2','3','4'])
        self.assertEqual(len(a.alerts)+len(b.alerts),4)
        self.db.watch_mark_delivered(1, a.alerts+b.alerts)
        c=self.scan()
        self.assertEqual(len(c.alerts),0)
        self.assertIn("Источник сообщает о наличии",render_watches(a,self.adapters))

    def test_no_stock_and_unknown_are_never_alerted(self):
        self.db.watch_add(1,"magnit","Кофе",100)
        self.ad.stock="нет"
        r=self.scan()
        self.assertEqual(r.unavailable,2)
        self.assertEqual(len(r.alerts),0)
        self.ad.stock="не подтверждено"
        r=self.scan()
        self.assertEqual(r.unknown,2)
        self.assertEqual(len(r.alerts),0)

    def test_too_expensive_or_source_error_no_alert(self):
        self.db.watch_add(1,"magnit","Кофе",100)
        self.ad.price=Decimal(119)
        self.assertFalse(self.scan().alerts)
        self.ad.fail=True
        report=self.scan()
        self.assertEqual(report.errors,2)
        self.assertFalse(report.alerts)

    def test_chain_selection_and_limit(self):
        for n in range(20):
            self.assertTrue(self.db.watch_add(1,"magnit",f"Кофе{n}",99))
        self.assertFalse(self.db.watch_add(1,"magnit","Кофе-лишний",99))
        self.assertTrue(self.db.watch_add(1,"magnit","Кофе0",99))
        empty=check_watches(self.db, self.adapters,1, selected=set(), sleeper=lambda n:None)
        self.assertEqual(empty.attempted,0)
        item=self.db.watches(1)[0]
        self.assertTrue(self.db.watch_remove(1,item['id']))
        self.assertFalse(self.db.watch_remove(1,item['id']))

    def test_bot_commands_and_locked_bootstrap(self):
        tg=FakeTelegram()
        locked=Bot(tg,self.adapters,self.db,allowed=set(),pace=0)
        locked.handle({"chat":{"type":"private","id":1},"from":{"id":1},"text":"/watchdeal Магнит: кофе 99 ₽"})
        self.assertEqual(self.db.watches(1),[])
        locked.handle({"chat":{"type":"private","id":1},"from":{"id":1},"text":"/id"})
        self.assertIn("ALLOWED_USER_IDS",tg.messages[-1][1])
        bot=Bot(tg,self.adapters,self.db,allowed={1},pace=0)
        bot.handle({"chat":{"type":"private","id":1},"from":{"id":1},"text":"/watchdeal Магнит: кофе 99 ₽"})
        self.assertEqual(len(self.db.watches(1)),1)
        bot.handle({"chat":{"type":"private","id":1},"from":{"id":1},"text":"/watchlist"})
        self.assertIn("кофе",tg.messages[-1][1])
        bot.handle({"chat":{"type":"private","id":1},"from":{"id":1},"text":"/checkwatch"})
        self.assertIn("Проверено пар", tg.messages[-1][1])
        watch_id=self.db.watches(1)[0]["id"]
        bot.handle({"chat":{"type":"private","id":1},"from":{"id":1},"text":f"/unwatchdeal {watch_id}"})
        self.assertFalse(self.db.watches(1))

if __name__=='__main__': unittest.main()
