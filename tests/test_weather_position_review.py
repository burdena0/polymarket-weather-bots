from types import SimpleNamespace
import unittest
from tests import test_intraday_weather as fixtures
from supahtrade.weather_position_review import review_position
from supahtrade.intraday_weather import MODEL


class PositionReviewTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.IntradayTests();self.f.setUp()
        self.position={**self.f.contract,'slug':'tc-temp-test','outcome':'YES','quantity':'5'}
        def no_book(slug):raise ValueError('No liquidity on requested order side')
        self.sources=SimpleNamespace(forecast=lambda *a:self.f.forecast,observations=lambda *a:self.f.obs,book=no_book)

    def test_deterioration_flagged_without_order_method(self):
        baseline={'selected':{'outcome':'YES','probability_low':.95},'forecast_summary':{'model':MODEL,'forecast_max_f':80}}
        r=review_position(self.position,self.sources,lambda:self.f.now,baseline)
        self.assertEqual(r['status'],'review_required');self.assertFalse(r['automatic_exit'])
        self.assertTrue(r['baseline_available']);self.assertIn('quote_error',r)

    def test_stale_data_does_not_become_a_forecast_change(self):
        self.f.forecast['received']-=301
        r=review_position(self.position,self.sources,lambda:self.f.now)
        self.assertEqual(r['status'],'review_unavailable');self.assertEqual(r['alerts'],[])

    def test_missing_baseline_is_explicit(self):
        r=review_position(self.position,self.sources,lambda:self.f.now)
        self.assertEqual(r['status'],'reviewed');self.assertFalse(r['baseline_available'])

    def test_ended_day_does_not_request_new_forecast(self):
        r=review_position(self.position,object(),lambda:self.f.end)
        self.assertEqual(r['status'],'awaiting_settlement')
