import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from bot import Bot
from discovery import ChannelPost, parse_public_page, referenced_channels
from providers import CHAIN_LABELS, DisabledAdapter
from storage import Storage


class Capture:
    def __init__(self):
        self.messages = []
    def send(self, chat_id, text, **kwargs):
        self.messages.append(text)


class MultiChannelV05(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / 'state.db'))
        self.tg = Capture()
        self.posts = {}
        self.checked = []
        self.now = int(datetime.now(timezone.utc).timestamp())
        self.bot = Bot(self.tg, {slug: DisabledAdapter(slug, label) for slug,label in CHAIN_LABELS.items()},
                       self.db, allowed={123}, discovery_fetcher=self.fetch)
        self.base = {'from': {'id':123}, 'chat': {'id': 123, 'type':'private'}}
        self.send('/start')
    def tearDown(self):
        self.tmp.cleanup()
    def send(self, text):
        self.bot.handle({**self.base, 'text': text})
    def fetch(self, channel):
        self.checked.append(channel)
        return self.posts.get(channel, [])
    def post(self, channel, index, text):
        return ChannelPost(channel, str(index), f'https://t.me/{channel}/{index}',text,self.now)
    def test_forward_from_public_channel_subscribes(self):
        self.bot.handle({**self.base, 'forward_origin': {'type':'channel', 'chat': {'username':'shop_deals'}},
                         'text':'Магнит: Кофе 99 ₽ вместо 500 ₽'})
        self.assertIn('shop_deals', self.db.monitor_channels(123))
        self.assertTrue(any('добавлен' in m for m in self.tg.messages))
        # The exact same forwarded post must not produce repeated subscription notice.
        before = sum('добавлен в мониторинг' in m for m in self.tg.messages)
        self.bot.handle({**self.base, 'forward_origin': {'type':'channel', 'chat': {'username':'shop_deals'}},
                         'text':'Магнит: Кофе 99 ₽ вместо 500 ₽'})
        self.assertEqual(sum('добавлен в мониторинг' in m for m in self.tg.messages), before)
    def test_private_forward_does_not_subscribe(self):
        self.bot.handle({**self.base, 'forward_origin': {'type':'channel','chat':{'title':'Private'}},
                         'text':'hello'})
        self.assertNotIn('private', self.db.monitor_channels(123))
    def test_telegram_html_link_is_extracted_in_post_content(self):
        markup = '''<div data-post="magnit_retail/18">
        <div class="tgme_widget_message_text"><a href="https://t.me/real_sale_daily">Подробнее</a></div>
        </div>'''
        posts = parse_public_page('magnit_retail', markup)
        self.assertEqual(referenced_channels(posts), ['real_sale_daily'])
    def test_discovers_only_referenced_feeds_with_repeated_discount_posts(self):
        self.posts['magnit_retail'] = [self.post('magnit_retail',1,'Пост со ссылкой на t.me/big_discounts')]
        self.posts['big_discounts'] = [self.post('big_discounts',2,'Магнит: Электробритва Philips 399 ₽ вместо 2999 ₽'),
                                      self.post('big_discounts',3,'Магнит: Кофемашина Delonghi 499 ₽ вместо 4999 ₽')]
        self.bot.scan_user(123)
        self.assertIn('big_discounts', self.db.monitor_channels(123))
        self.assertTrue(any('big_discounts' in m for m in self.tg.messages))
        self.assertNotIn('real_sale_daily', self.db.monitor_channels(123))
    def test_bad_feed_not_added_and_toggle_works(self):
        self.send('/autochannels off')
        self.posts['magnit_retail']=[self.post('magnit_retail',1,'ссылка на https://t.me/bad_channel')]
        self.posts['bad_channel']=[self.post('bad_channel',1,'просто новости')]
        self.bot.scan_user(123)
        self.assertNotIn('bad_channel', self.db.monitor_channels(123))
        self.assertNotIn('bad_channel', self.checked)
        self.send('/autochannels on')
        self.bot.scan_user(123)
        self.assertIn('bad_channel',self.checked)
        self.assertNotIn('bad_channel',self.db.monitor_channels(123))
        self.assertFalse(self.db.monitor_probe_allowed(123,'bad_channel'))
    def test_probe_cap_two_per_poll(self):
        leads = ' '.join('https://t.me/sale_channel_'+str(i) for i in range(8))
        self.posts['magnit_retail']=[self.post('magnit_retail',1,leads)]
        self.bot.scan_user(123)
        checked_candidates=[ch for ch in self.checked if ch.startswith('sale_channel')]
        self.assertEqual(len(checked_candidates),2)

if __name__=='__main__':
    unittest.main()
