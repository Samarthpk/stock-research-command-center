import sys
import json
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autonomous_agents import parse_watchlist,quality_gate,decide_signal,analyze_symbol,run_mission,fetch_alpha

MAP={'0':{'ticker':'AAPL','title':'Apple','cik_str':320193}}
SUB={'filings':{'recent':{'form':['10-Q'],'accessionNumber':['0000320193-26-000001'],'primaryDocument':['a.htm'],'filingDate':['2026-11-12']}}}
FACT={'facts':{'us-gaap':{'NetIncomeLoss':{'units':{'USD':[{'val':100,'end':'2026-09-30','filed':'2026-11-12','form':'10-Q','start':'2026-07-01'}]}}}}}
NEWS=b'<rss><channel><item><title>Example</title><link>https://example.org/a</link><pubDate>Fri, 13 Nov 2026 20:00:00 GMT</pubDate></item></channel></rss>'
def fake(url,user_agent=None,timeout=15):
    if 'news.google' in url:return NEWS
    if 'company_tickers' in url:return json.dumps(MAP).encode()
    if 'submissions' in url:return json.dumps(SUB).encode()
    if 'companyfacts' in url:return json.dumps(FACT).encode()
    raise AssertionError('Unexpected URL')
class Tests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(parse_watchlist('aapl,msft,aapl'),['AAPL','MSFT'])
        with self.assertRaises(ValueError): parse_watchlist('AAPL;bad')
    def test_quality_gate_requires_primary(self):
        self.assertEqual(quality_gate({'news':[{'url':'http://x','title':'a'}]}),'PARTIAL_EVIDENCE')
    def test_signal_no_buy_sell(self):
        self.assertEqual(decide_signal({'quality':'INSUFFICIENT_EVIDENCE','price':{'trend':'POSITIVE','status':'OK'}})['label'],'INSUFFICIENT_EVIDENCE')
    def test_research_agents(self):
        r=analyze_symbol('AAPL',fetch=fake,sec_user_agent='Research contact@example.org',cik_map={'AAPL':(320193,'Apple')},price={'status':'OK','trend':'POSITIVE'})
        self.assertEqual(r['quality'],'PRIMARY_EVIDENCE_PRESENT')
        self.assertEqual(r['agents']['ai_analyst']['status'],'SKIPPED')
        self.assertEqual(r['signal']['label'],'INVESTIGATE')
    def test_partial_outage(self):
        def bad(*args,**kwargs): raise OSError('network broken')
        r=analyze_symbol('AAPL',fetch=bad,sec_user_agent='Research contact@example.org',cik_map={'AAPL':(320193,'Apple')})
        self.assertEqual(r['quality'],'INSUFFICIENT_EVIDENCE')
        self.assertEqual(r['agents']['news']['status'],'FAILED')
    def test_mission_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            m=run_mission(['AAPL'], output_dir=d,fetch=fake,sec_user_agent='Research contact@example.org',price_fetch=lambda *a: {'status':'OK','trend':'POSITIVE'},alpha_key='dummy',sleep=lambda s:None)
            self.assertEqual(m['status'],'COMPLETE')
            self.assertTrue((Path(d)/'latest.json').exists())
            self.assertEqual(json.loads((Path(d)/'latest.json').read_text())['results'][0]['ticker'],'AAPL')
    def test_rate_limit_stops_remaining_symbols(self):
        calls=[]
        def exhausted(symbol,key):
            calls.append(symbol)
            return {'status':'RATE_LIMITED'}
        with tempfile.TemporaryDirectory() as d:
            m=run_mission(['AAPL','MSFT'],output_dir=d,fetch=fake,price_fetch=exhausted,alpha_key='dummy',sleep=lambda s:None)
            self.assertEqual(calls,['AAPL'])
            self.assertEqual(m['results'][1]['price']['status'],'QUOTA_BLOCKED')
    def test_missing_sec_identity_not_faked(self):
        r=analyze_symbol('AAPL',fetch=fake)
        self.assertEqual(r['agents']['filings']['status'],'SKIPPED')
        self.assertEqual(r['quality'],'PARTIAL_EVIDENCE')
if __name__=='__main__':unittest.main()
