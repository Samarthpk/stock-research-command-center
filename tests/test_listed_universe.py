import unittest
from listed_universe import parse_nasdaq_directory, parse_nse_equities, load_directory, search_directory, US_NASDAQ_URL,US_OTHER_URL,NSE_EQUITY_URL

class TestListedUniverse(unittest.TestCase):
    def test_nasdaq_equities_and_test_issue_excluded(self):
        csvdata="Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\nAAPL|Apple Inc.|Q|N|N|100|N|N\nFAKE|Dummy|Q|Y|N|100|N|N\nFile Creation Time: 20261114|||||||\n"
        rows=parse_nasdaq_directory(csvdata)
        self.assertEqual([r["symbol"] for r in rows],["AAPL"])
        self.assertEqual(rows[0]["market"],"US")
    def test_otherlisted_parses_exchange_and_etf(self):
        s="ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\nSPY|SPDR ETF|P|SPY|Y|100|N|SPY\n"
        rows=parse_nasdaq_directory(s,other=True)
        self.assertEqual(rows[0]["exchange"],"NYSE Arca")
        self.assertEqual(rows[0]["asset_type"],"ETF")
    def test_nse_equities_series_eq_only(self):
        s="SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE\nRELIANCE,Reliance Industries Ltd.,EQ,1990,10,1,INE002A01018,10\nTEST,Test Ltd.,BE,2020,10,1,INE000000001,10\n"
        rows=parse_nse_equities(s)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["isin"],"INE002A01018")
    def test_unavailable_sources_visible(self):
        def fake(url):
            if url == US_NASDAQ_URL:
                return "Symbol|Security Name|Test Issue|ETF\nAAPL|Apple Inc.|N|N\n"
            raise OSError("blocked")
        data=load_directory(fetch=fake)
        self.assertEqual(len(data["rows"]),1)
        self.assertEqual(data["sources"]["India NSE EQ"]["status"],"UNAVAILABLE")
        self.assertFalse(data["complete_coverage"])
        self.assertEqual(search_directory(data["rows"],"apple")["total"],1)
    def test_search_by_isin_case_insensitive(self):
        r=[{"market":"IN","exchange":"NSE","symbol":"RELIANCE","name":"Reliance Industries","isin":"INE002A01018"}]
        self.assertEqual(search_directory(r,"ine002a01018",market="IN")["total"],1)

if __name__=="__main__":
    unittest.main()
