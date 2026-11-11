import unittest
from diagnostics import probe_sources, render_diagnostics
from providers import Adapter, DisabledAdapter
from core import ProviderError

class Probe(Adapter):
    def __init__(self, fail=False):
        super().__init__('magnit','Магнит','fixture')
        self.fail=fail
        self.calls=0
    @property
    def configured(self): return True
    def search(self, store_id, query):
        self.calls+=1
        if self.fail: raise ProviderError('down')
        return [{'name':'Кофе','status':'есть'},{'name':'Шоколад','status':'не подтверждено'}]

class DoctorTests(unittest.TestCase):
    def test_connected_no_store_and_disconnected(self):
        a=Probe()
        adapters={'magnit':a, 'dixy':DisabledAdapter('dixy','Дикси')}
        result=probe_sources(adapters,[{'chain':'magnit','id':'abc','address':'Москва'}])
        self.assertEqual(result[0]['state'],'responded')
        self.assertEqual(result[0]['stock_known'],1)
        self.assertEqual(result[1]['state'],'not_configured')
        self.assertEqual(a.calls,1)
        self.assertIn('Получен ответ каталога',render_diagnostics(result,adapters))
        result=probe_sources(adapters,[])
        self.assertEqual(result[0]['state'],'no_store')
        self.assertEqual(a.calls,1)
    def test_failed_source_not_shown_as_no_stock(self):
        a=Probe(fail=True)
        result=probe_sources({'magnit':a},[{'chain':'magnit','id':'123'}])
        self.assertEqual(result[0]['state'],'error')

if __name__=='__main__': unittest.main()
