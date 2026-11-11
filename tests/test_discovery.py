import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from bot import Bot
from discovery import (DEFAULT_CHANNELS, ChannelPost, normalize_channel, parse_public_page, qualify)
from providers import CHAIN_LABELS, DisabledAdapter
from storage import Storage


class FakeTelegram:
    def __init__(self):
        self.messages=[]
    def send(self, chat, message, **kwargs):
        self.messages.append((chat, message))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Storage(str(Path(self.tmp.name)/'db.sqlite'))
        self.tg=FakeTelegram()
        self.adapters={slug: DisabledAdapter(slug,label) for slug,label in CHAIN_LABELS.items()}
        self.posts={}
        self.bot=Bot(self.tg,self.adapters,self.db,allowed={123},
                     discovery_fetcher=lambda ch:self.posts.get(ch,[]))
        self.message={'from':{'id':123},'chat':{'id':123,'type':'private'}}
        self.now=int(datetime.now(timezone.utc).timestamp())
    def tearDown(self):
        self.tmp.cleanup()
    def send(self,text):
        self.bot.handle({**self.message,'text':text})
    def post(self, idx, text="Магнит: Электробритва Philips 399 ₽ вместо 2999 ₽"):
        return ChannelPost('magnit_retail',str(idx), f'https://t.me/magnit_retail/{idx}',text,self.now)
    def test_normalize_accepts_only_public_handles(self):
        self.assertEqual(normalize_channel('https://t.me/s/magnit_retail'),'magnit_retail')
        self.assertEqual(normalize_channel('@Magnar'),'magnar')
        for bad in ['https://evil.com/x','@hi','../../stuff','http://127.0.0.1','user?before=123']:
            self.assertIsNone(normalize_channel(bad))
    def test_html_posts_have_id_text_and_timestamp(self):
        markup='''<div class="tgme_widget_message" data-post="magnit_retail/33">
          <div class="tgme_widget_message_text js-message_text">Магнит: Электробритва Philips <b>399 ₽</b><br>вместо 2999 ₽</div>
          <time datetime="2026-11-10T09:00:00+00:00"></time></div>'''
        posts=parse_public_page('magnit_retail',markup)
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0].url,'https://t.me/magnit_retail/33')
        self.assertIn('вместо 2999',posts[0].text)
        self.assertIsNotNone(posts[0].timestamp)
    def test_claim_is_not_verified_and_scored(self):
        p=self.post(5)
        c=qualify(p,70,now=self.now)
        self.assertIsNotNone(c)
        self.assertGreaterEqual(c.discount_pct,85)
        self.assertEqual(c.deal.chain,'magnit')
        self.assertIsNone(qualify(p,95,now=self.now))
        self.assertIsNone(qualify(self.post(2,'Промокод на заказ: 399 ₽ вместо 2999 ₽'),70,now=self.now))
        old=ChannelPost(p.channel,'6',p.url,p.text,self.now-4*86400)
        self.assertIsNone(qualify(old,70,now=self.now))
    def test_seed_channels_and_controls(self):
        self.send('/start')
        self.assertIn('magnit_retail',self.db.monitor_channels(123))
        self.send('/watch @cool_deals')
        self.assertIn('cool_deals',self.db.monitor_channels(123))
        self.send('/unwatch @cool_deals')
        self.assertNotIn('cool_deals',self.db.monitor_channels(123))
        self.send('/alerts 80')
        self.assertEqual(self.db.monitor_settings(123)['threshold'],80)
        self.send('/pause')
        self.assertEqual(self.db.monitor_users(),[])
        self.send('/resume')
        self.assertIn(123,self.db.monitor_users())
    def test_baseline_no_old_alert_then_alert_once_on_new_post(self):
        self.send('/start')
        self.posts['magnit_retail']=[self.post(11)]
        initial=len(self.tg.messages)
        self.bot.scan_user(123)
        self.assertEqual(len(self.tg.messages),initial)
        self.posts['magnit_retail']=[self.post(11),self.post(12)]
        self.bot.scan_user(123)
        msgs=[m for _,m in self.tg.messages[initial:]]
        self.assertEqual(len(msgs),1)
        self.assertIn('https://t.me/magnit_retail/12',msgs[0])
        self.assertIn('не проверена',msgs[0].lower())
        self.bot.scan_user(123)
        self.assertEqual(len(self.tg.messages),initial+1)
    def test_manual_scan_finds_current_deal_and_error(self):
        self.send('/start')
        self.posts['magnit_retail']=[self.post(42)]
        self.send('/scan')
        self.assertIn('https://t.me/magnit_retail/42',self.tg.messages[-1][1])
        self.assertIn('проверил 4 канал',self.tg.messages[-1][1].lower())
    def test_no_false_stock_in_unconfigured_chain(self):
        self.send('/start')
        self.posts['magnit_retail']=[self.post(11)]
        self.bot.scan_user(123)
        self.posts['magnit_retail']=[self.post(12)]
        self.bot.scan_user(123)
        self.assertIn('наличие из поста не подтверждено',self.tg.messages[-1][1].lower())

if __name__=='__main__':unittest.main()
