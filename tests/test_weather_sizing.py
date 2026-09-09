import unittest
from decimal import Decimal
from tests.test_weather_router import RouterTests
from supahtrade.weather_sizing import sized_entry


class SizingTests(unittest.TestCase):
    def test_reduces_quantity_to_available_cash(self):
        f=RouterTests();f.setUp()
        q,p,cap=sized_entry(f.book,f.market['slug'],'NO',f.now,'.01','.30')
        self.assertLess(q,5);self.assertLessEqual(cap,Decimal('.30'))
        self.assertEqual(q*100,(q*100).to_integral_value())

    def test_minimum_cannot_be_overridden_to_fit_cash(self):
        f=RouterTests();f.setUp()
        with self.assertRaisesRegex(ValueError,'minimum'):
            sized_entry(f.book,f.market['slug'],'NO',f.now,'5','.01')

    def test_no_budget_means_no_order(self):
        f=RouterTests();f.setUp()
        with self.assertRaises(ValueError):sized_entry(f.book,f.market['slug'],'NO',f.now,'.01','0')
