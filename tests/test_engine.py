import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import analyze, report, get_api_key

class EngineTests(unittest.TestCase):
 def test_trend(self):
  rows=[(f'd{i}',100+i) for i in range(60)]
  row=analyze('TEST',rows,'example')
  self.assertEqual(row['stance'],'Positive trend')
  self.assertEqual(row['close'],159)
 def test_not_enough(self):
  with self.assertRaises(ValueError):analyze('A', [('x',1)], 'example')
 def test_no_fabrication(self):
  result=report({'checked_at_utc':'now','results':[],'errors':[{'symbol':'AAA','error':'rate limited'}]})
  self.assertIn('No successful live data fetches',result)
 def test_get_api_key_from_env(self):
  with patch.dict(os.environ, {'ALPHAVANTAGE_API_KEY': 'test_key_123'}):
   self.assertEqual(get_api_key(), 'test_key_123')
if __name__=='__main__':unittest.main()
