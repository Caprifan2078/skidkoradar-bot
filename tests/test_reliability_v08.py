"""v0.8 regression tests: scheduling, durable delivery, scan coverage."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import patch

from bot import Bot, Telegram
from discovery import ChannelPost
from providers import CHAIN_LABELS, DisabledAdapter
from scheduler import BackgroundScheduler, ScheduledJob
from storage import Storage
from test_watch_v07 import FakeAdapter, FakeTelegram

UID = 101

class OneFailureTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.fail_once = True
    def send(self, chat_id, text, **kwargs):
        if self.fail_once:
            self.fail_once = False
            raise OSError("temporary failure")
        return super().send(chat_id, text, **kwargs)


class V08Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/"deals.sqlite")
        self.db = Storage(self.path)
        self.db.ensure_monitor(UID)
    def tearDown(self):
        self.tmp.cleanup()

    def test_background_scheduler_does_not_block_the_caller(self):
        entered, release = Event(), Event()
        def long_scan():
            entered.set()
            release.wait(timeout=4)
        sched = BackgroundScheduler([ScheduledJob('long', 600, long_scan)])
        try:
            sched.start()
            self.assertTrue(entered.wait(timeout=2))
            self.assertTrue(sched._thread.is_alive())
            self.assertEqual(sched.jobs[0].runs, 1)
        finally:
            release.set()
            sched.shutdown()

    def test_failed_background_job_does_not_block_next_task(self):
        called=[]
        def fail():
            raise ValueError('simulated')
        sched=BackgroundScheduler([ScheduledJob('bad',100,fail),
                                   ScheduledJob('good',100,lambda: called.append('ok'))])
        with patch('scheduler.LOG'):
            sched.run_due()
        self.assertEqual(called,['ok'])
        self.assertEqual(sched.jobs[0].failures,1)
        sched.run_due()
        self.assertEqual(called,['ok'])

    def test_failed_channel_delivery_is_retried_even_if_post_seen(self):
        tg=OneFailureTelegram()
        now=int(datetime.now(timezone.utc).timestamp())
        posts=[]
        self.db.monitor_add_channel(UID, 'magnit_retail')
        bot=Bot(tg, {slug:DisabledAdapter(slug,label) for slug,label in CHAIN_LABELS.items()},
                self.db,allowed={UID},discovery_fetcher=lambda ch:posts if ch=='magnit_retail' else [])
        posts.append(ChannelPost('magnit_retail','1','https://t.me/magnit_retail/1',
                     'Магнит: Электробритва Philips 399 ₽ вместо 2999 ₽',now))
        bot.scan_user(UID)  # baseline is not announced
        posts.append(ChannelPost('magnit_retail','2','https://t.me/magnit_retail/2',
                     'Магнит: Электробритва Philips 399 ₽ вместо 2999 ₽',now))
        with self.assertRaises(OSError):
            bot.scan_user(UID)
        self.assertEqual(len(self.db.channel_alert_pending(UID)),1)
        bot.scan_user(UID)
        self.assertEqual(len(self.db.channel_alert_pending(UID)),0)
        self.assertIn('/2',tg.messages[-1][1])
        before=len(tg.messages)
        bot.scan_user(UID)
        self.assertEqual(len(tg.messages),before)

    def test_delivery_receipt_for_catalog_alert_and_deferred_retry(self):
        product_key='test-hash'
        args=(UID,'magnit','1',product_key,Decimal('99'))
        now=3000000
        self.assertTrue(self.db.price_new_alert(*args,now=now))
        self.assertFalse(self.db.price_new_alert(*args,now=now+120))
        self.assertTrue(self.db.price_new_alert(*args,now=now+301))
        from pricehunt import HuntDeal,product_identity
        deal=HuntDeal('magnit','test street','1','Coffee',Decimal('99'),Decimal('500'),80,
                      'retailer_old_price','есть',code='4601234567890')
        key=product_identity({'name':deal.product_name,'code':deal.code})
        self.assertTrue(self.db.price_new_alert(UID,'magnit','1',key,Decimal('99'),now=now))
        self.db.price_mark_delivered(UID,[deal],now=now+1)
        self.assertFalse(self.db.price_new_alert(UID,'magnit','1',key,Decimal('99'),now=now+401))

    def test_persist_telegram_offset_and_last_scan_coverage(self):
        self.db.set_update_offset(439)
        self.db.scan_record(UID,'watches',5,100,1,2,now=888)
        other=Storage(self.path)
        self.assertEqual(other.get_update_offset(),439)
        self.assertEqual(other.scan_coverage(UID)[0]['checked'],5)
        tg=FakeTelegram()
        bot=Bot(tg,{'magnit':FakeAdapter()},other,allowed={UID})
        bot.handle({'from':{'id':UID},'chat':{'id':UID,'type':'private'},'text':'/coverage'})
        self.assertIn('5/100',tg.messages[-1][1])
        self.assertIn('не всех магазинов города',tg.messages[-1][1])

    def test_catalog_discount_is_not_marked_sent_on_failed_telegram(self):
        class DiscountAdapter(FakeAdapter):
            def catalog(self, store_id, *, cursor=None, limit=60):
                return ([{'name':'Кофе тест','price':Decimal('99'),
                          'old_price':Decimal('599'), 'status':'есть',
                          'code':'4600000000001'}], None)
        self.db.add_stores(UID,[{'chain':'magnit','id':'1','address':'Тестовый магазин'}])
        tg=OneFailureTelegram()
        bot=Bot(tg, {'magnit':DiscountAdapter()},self.db,pace=0,allowed={UID})
        with self.assertRaises(OSError):
            bot.hunt_user(UID)
        with self.db._connect() as connection:
            row=connection.execute('SELECT delivered_at FROM price_alerts').fetchone()
            self.assertIsNone(row['delivered_at'])
            # Simulate a later retry without waiting 5 real minutes.
            connection.execute('UPDATE price_alerts SET seen_at=seen_at-301')
        bot.hunt_user(UID)
        self.assertIn('Кофе тест',tg.messages[-1][1])
        self.assertEqual(len(tg.messages),1)
        with self.db._connect() as connection:
            self.assertIsNotNone(connection.execute('SELECT delivered_at FROM price_alerts').fetchone()[0])
        bot.hunt_user(UID)
        self.assertEqual(len(tg.messages),1)

    def test_search_coverage_never_claims_entire_city(self):
        self.db.add_stores(UID,[{'chain':'magnit','id':str(i),'address':f'Тестовая {i}'}
                                 for i in range(30)])
        tg=FakeTelegram()
        bot=Bot(tg,{'magnit':FakeAdapter()},self.db,allowed={UID},pace=0,max_stores=2)
        bot.handle({'from':{'id':UID},'chat':{'id':UID,'type':'private'},'text':'/search кофе'})
        rows=self.db.scan_coverage(UID)
        self.assertEqual(rows[0]['checked'],2)
        self.assertEqual(rows[0]['total'],30)
        self.assertIn('часть сохранённых точек',tg.messages[-1][1].lower())

    def test_telegram_http_error_does_not_expose_token(self):
        import urllib.error
        token='secret-bot-token-do-not-leak'
        client=Telegram(token)
        error=urllib.error.HTTPError(f'https://api.telegram.org/bot{token}/sendMessage',401,
                                    'Unauthorized',{},None)
        with patch('bot.urllib.request.urlopen',side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                client.method('sendMessage',{'chat_id':1,'text':'hi'})
        self.assertNotIn(token,str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

if __name__=='__main__':
    unittest.main()
