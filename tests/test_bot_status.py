import json,tempfile,unittest
from pathlib import Path
from supahtrade.bot_status import read_status,overview
class BotStatusTests(unittest.TestCase):
 def test_missing_is_not_zero_pnl(self):
  self.assertFalse(read_status('missing-do-not-create')['available'])
 def test_stale_and_future(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'status.json';p.write_text(json.dumps({'at':100,'armed':True}))
   self.assertTrue(read_status(p,300)['stale']);self.assertFalse(read_status(p,50)['available'])
 def test_explicit_laptop_journal_is_used_without_desktop_fallback(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);desktop=root/'data/us-weather-live-v1';desktop.mkdir(parents=True)
   (desktop/'status.json').write_text(json.dumps({'at':100,'armed':True,'status':'observing'}))
   laptop=root/'laptop'
   self.assertFalse(overview(root,laptop)['polymarket']['available'])
   laptop.mkdir();(laptop/'status.json').write_text(json.dumps({'at':100,'armed':False,'status':'stopped'}))
   self.assertEqual(overview(root,laptop)['polymarket']['status'],'stopped')
 def test_independent_root_does_not_fall_back_to_observer(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);observer=root/'data/us-weather-independent-observer-v1';observer.mkdir(parents=True)
   (observer/'status.json').write_text(json.dumps({'at':100,'status':'stopped','armed':False}))
   self.assertTrue(overview(root)['independent_weather']['available'])
   self.assertFalse(overview(root,independent_weather_root=root/'live')['independent_weather']['available'])
import sqlite3
from supahtrade.bot_status import independent_status
class HistoricalDecisionsTests(unittest.TestCase):
 def test_combined_dashboard_uses_both_shared_strategy_reports(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);combined=root/'data/us-weather-combined-v1'
   for name in ('reference','independent'):
    path=combined/name;path.mkdir(parents=True)
    (path/'status.json').write_text(json.dumps({'at':100,'status':'observing','armed':True}))
   (combined/'combined.json').write_text(json.dumps({'version':1,'live':True}))
   result=overview(root)
   self.assertTrue(result['combined']['enabled'])
   self.assertEqual(Path(result['polymarket']['journal_root']),combined/'reference')
   self.assertEqual(Path(result['independent_weather']['journal_root']),combined/'independent')
 def test_recovers_decisions_without_reviving_stopped_state(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   (root/'status.json').write_text(json.dumps({'at':100,'status':'stopped','armed':False}))
   with sqlite3.connect(root/'signals.sqlite') as db:
    db.execute('CREATE TABLE independent_cycles(id INTEGER PRIMARY KEY,at REAL,report TEXT)')
    db.execute('INSERT INTO independent_cycles(at,report) VALUES(?,?)',(90,json.dumps({'armed':True,'decisions':[{'status':'rejected'}]})))
   db.close()
   s=independent_status(root)
   self.assertEqual(s['status'],'stopped');self.assertFalse(s['armed'])
   self.assertTrue(s['decisions_historical']);self.assertEqual(s['decisions_at'],90)
   self.assertEqual(len(s['decisions']),1)
 def test_missing_journal_not_created(self):
  with tempfile.TemporaryDirectory() as d:
   independent_status(d)
   self.assertFalse((Path(d)/'signals.sqlite').exists())
