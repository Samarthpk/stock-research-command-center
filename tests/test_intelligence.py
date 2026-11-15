import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from intelligence import news_for,sec_ticker_map,sec_filings,company_facts,build_research,llm_evidence_review,is_company_specific_text,india_disclosures_for

RSS=b'<rss><channel><item><title>Example headline</title><link>https://example.com/article</link><pubDate>Fri, 13 Nov 2026 20:00:00 GMT</pubDate></item></channel></rss>'
MAP={'0': {'ticker':'AAPL','title':'Apple Inc','cik_str':320193}}
SUB={'filings':{'recent':{'form':['10-Q','8-K'], 'accessionNumber':['0000320193-26-000001','0000320193-26-000002'],'primaryDocument':['x10q.htm','x8k.htm'],'filingDate':['2026-11-12','2026-11-10']}}}
FACT = {'facts': {'us-gaap': {'NetIncomeLoss': {'units': {'USD': [{'val': 100, 'end': '2026-06-30', 'filed': '2026-07-15', 'form': '10-Q', 'start': '2026-04-01'}]}}}}}

def fake(url,user_agent=None,timeout=15):
    if 'news.google' in url:return RSS
    if 'company_tickers' in url:return json.dumps(MAP).encode()
    if 'submissions' in url:return json.dumps(SUB).encode()
    if 'companyfacts' in url:return json.dumps(FACT).encode()
    raise AssertionError('Unexpected URL: '+url)

class ResearchTests(unittest.TestCase):
    def test_itc_official_identifiers_and_exclusions(self):
        from global_markets import resolve_instrument
        inst = resolve_instrument('ITC', market='IN')
        self.assertEqual(inst['bse_scrip_code'], '500875')
        self.assertEqual(inst['isin'], 'INE154A01025')
        self.assertTrue(is_company_specific_text('ITC Limited Q2 earnings results announced', inst))
        self.assertTrue(is_company_specific_text('ITC buys Yoga Bar owner Sproutlife outright', inst))
        self.assertFalse(is_company_specific_text('ITC Hotels Q2 net profit rises 52%', inst))
        self.assertFalse(is_company_specific_text('337 ITC Case on Behalf of U.S. Shrimpers', inst))
        self.assertFalse(is_company_specific_text('International Trade Commission ITC investigation', inst))
    def test_itc_secondary_mentions_remove_unrelated_entities(self):
        rss=b'<rss><channel><item><title>ITC Q2 Results</title><link>https://example.org/a</link></item><item><title>ITC Hotels Q2 profit grows</title><link>https://example.org/b</link></item><item><title>337 ITC Case U.S. Shrimpers</title><link>https://example.org/c</link></item></channel></rss>'
        out=india_disclosures_for('ITC', fetch=lambda url: rss)
        self.assertEqual([x['title'] for x in out['secondary_disclosure_mentions']],['ITC Q2 Results'])

    def test_news_evidence(self):
        rows=news_for('AAPL',fake)
        self.assertEqual(len(rows),1)
        self.assertIn('headline only',rows[0]['source'])
    def test_input_rejected(self):
        with self.assertRaises(ValueError):news_for('AAPL;rm -rf /',fake)
    def test_sec_requires_identity(self):
        with self.assertRaises(ValueError):sec_ticker_map(fake,user_agent='')
    def test_sec_filings(self):
        data=sec_filings('AAPL',fake,'Example contact@example.com',{'AAPL':(320193,'Apple Inc')})
        self.assertEqual(len(data['filings']),2)
        self.assertIn('sec.gov',data['filings'][0]['url'])
    def test_company_facts_period(self):
        x=company_facts('AAPL',fake,'Example contact@example.com',{'AAPL':(320193,'Apple Inc')})
        self.assertEqual(x['metrics']['Net income']['value_usd'],100)
        self.assertEqual(x['metrics']['Net income']['period_start'],'2026-04-01')
    def test_complete(self):
        r=build_research('AAPL',fake,'Example contact@example.com')
        self.assertEqual(r['assessment'],'RESEARCH_READY')
    def test_no_fabrication_on_failure(self):
        def down(*args,**kwargs):raise OSError('Network down')
        r=build_research('AAPL',down,'Example contact@example.com')
        self.assertEqual(r['assessment'],'INSUFFICIENT_EVIDENCE')
        self.assertTrue(r['errors'])
    def test_llm_unconfigured(self):
        self.assertEqual(llm_evidence_review({'ticker':'AAPL'},'')['status'],'NOT_CONFIGURED')

if __name__=='__main__':unittest.main()
