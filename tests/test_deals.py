import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bot import Bot
from deals import parse_deal
from providers import CHAIN_LABELS, Adapter, DisabledAdapter, render_deal, scan_multi
from storage import Storage


class InMemoryAdapter(Adapter):
    def __init__(self, slug, price, available=True):
        super().__init__(slug, CHAIN_LABELS[slug], 'fake')
        self.price = price
        self.available = available
    @property
    def configured(self):
        return True
    def nearest(self, lat, lon, limit):
        return [{'id': self.slug+'-1', 'address':'улица Тестовая, 3'}]
    def search(self, store_id, query):
        return [{'name':'Электробритва Philips', 'price':self.price,
                 'status':'есть' if self.available else 'не подтверждено', 'code':''}]

class FakeTelegram:
    def __init__(self):
        self.messages=[]
        self.location_prompts=0
    def send(self, chat, message, *, ask_location=False):
        self.messages.append(message)
        self.location_prompts += int(ask_location)

class DealTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Storage(str(Path(self.tmp.name)/'db.sqlite3'))
        self.tg=FakeTelegram()
        self.adapters={slug:DisabledAdapter(slug,label) for slug,label in CHAIN_LABELS.items()}
        self.adapters['magnit']=InMemoryAdapter('magnit',399)
        self.adapters['pyaterochka']=InMemoryAdapter('pyaterochka',299)
        self.bot=Bot(self.tg,self.adapters,self.db,pace=0,allowed={1})
    def tearDown(self):
        self.tmp.cleanup()
    def msg(self,text=None,location=None):
        o={'chat':{'id':1,'type':'private'},'from':{'id':1}}
        if text is not None:o['text']=text
        if location is not None:o['location']=location
        return o
    def test_parser_original_sale_price_and_chain(self):
        d=parse_deal('🔥 Магнит: Электробритва Philips 399 ₽ вместо 2999 ₽')
        self.assertEqual((d.chain,d.query,d.target_price,d.old_price),
                         ('magnit','Электробритва Philips',Decimal(399),Decimal(2999)))
    def test_cosmetic_first(self):
        d=parse_deal('Магнит Косметик: Шампунь 99 ₽ вместо 349 ₽')
        self.assertEqual(d.chain,'magnit_cosmetic')
    def test_no_guessed_price_on_ambiguity(self):
        self.assertIsNone(parse_deal('Чай 299 ₽ 199 ₽').target_price)
    def test_forward_targets_only_stated_chain(self):
        self.db.add_stores(1,[{'chain':'magnit','id':'m','address':'адрес Магнита'},
                              {'chain':'pyaterochka','id':'p','address':'адрес Пятёрочки'}])
        self.bot.handle(self.msg('🔥 Магнит: Электробритва Philips 399 ₽ вместо 2999 ₽'))
        report=self.tg.messages[-1]
        self.assertIn('399.00 ₽',report)
        self.assertIn('адрес Магнита',report)
        self.assertNotIn('адрес Пятёрочки',report)
        self.assertIn('источник сообщает о наличии',report)
    def test_forward_location_automatically_continues(self):
        self.bot.handle(self.msg('🔥 Магнит: Электробритва Philips 399 ₽ вместо 2999 ₽'))
        self.assertEqual(self.tg.location_prompts,1)
        self.bot.handle(self.msg(location={'latitude':55.75,'longitude':37.61}))
        self.assertIn('399.00 ₽',self.tg.messages[-1])
        self.assertIsNone(self.db.pop_pending(1))
    def test_unavailable_chain_does_not_claim_stock(self):
        self.bot.handle(self.msg('Магнит Косметик: Электробритва Philips 399 ₽ вместо 2999 ₽'))
        self.assertIn('НЕ подключён',self.tg.messages[-1])
        self.assertEqual(self.tg.location_prompts,0)
    def test_overpriced_clearly_not_listed_as_discount(self):
        self.adapters['magnit'].price=599
        self.db.add_stores(1,[{'chain':'magnit','id':'m','address':'дорогой магазин'}])
        self.bot.handle(self.msg('Магнит: Электробритва Philips 399 ₽ вместо 2999 ₽'))
        self.assertIn('дороже указанной скидки',self.tg.messages[-1])
        self.assertIn('Предложений по заявленной цене',self.tg.messages[-1])
    def test_barcode_is_not_inferred_from_product_name(self):
        d=parse_deal('Штрихкод: 4601234567890; в Пятёрочке за 399 ₽')
        self.assertEqual(d.query,'4601234567890')
        self.assertEqual(d.chain,'pyaterochka')

if __name__=='__main__':unittest.main()
