import unittest
from engine import analyze,report
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
if __name__=='__main__':unittest.main()
