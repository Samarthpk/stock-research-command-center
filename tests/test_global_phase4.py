import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import global_markets
import engine
import intelligence
import autonomous_agents as aa


SAMPLE_BSE_SERIES = [
    (f"2026-02-{i:02d}", 1200.0 + i * 5.0) for i in range(1, 29)
] + [
    (f"2026-03-{i:02d}", 1350.0 + i * 6.0) for i in range(1, 31)
]

# Includes both company-specific official PDF filing, company-specific secondary news, AND unrelated generic NSE/BSE/SEBI noise
SAMPLE_INDIA_MIXED_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Reliance Industries Limited Q4 Financial Results Filing under SEBI LODR Reg 33</title>
      <link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/reliance_q4_fy26_results.pdf</link>
      <pubDate>Mon, 30 Mar 2026 11:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Reliance Industries Q4 profit rises - Media Report</title>
      <link>https://news.google.com/rss/articles/CBMi12345</link>
      <pubDate>Mon, 30 Mar 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Khaitan Chemicals pays Rs 11,800 fine to NSE and BSE for delayed intimation</title>
      <link>https://news.google.com/rss/articles/CBMi99999</link>
      <pubDate>Mon, 30 Mar 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>SEBI issues generic circular for all listed entities on BSE and NSE</title>
      <link>https://www.sebi.gov.in/filings/generic_circular.pdf</link>
      <pubDate>Sun, 29 Mar 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
""".encode("utf-8")


class GlobalPhase4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_phase4.db"
        self.orig_db = engine.DB
        engine.DB = self.db_path
        engine.init_db()

    def tearDown(self):
        engine.DB = self.orig_db
        self.tmp.cleanup()

    def test_separate_us_and_india_watchlists(self):
        us_list = engine.watchlist("US")
        in_list = engine.watchlist("IN")
        all_list = engine.watchlist("ALL")
        self.assertEqual(set(us_list), {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "SPY"})
        self.assertEqual(in_list, ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"])
        self.assertEqual(set(all_list), set(us_list) | set(in_list))

        engine.set_watchlist(["RELIANCE", "TCS"], market="IN")
        self.assertEqual(engine.watchlist("IN"), ["RELIANCE", "TCS"])
        self.assertEqual(set(engine.watchlist("US")), {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "SPY"})

    def test_nse_and_bse_symbols_not_interchangeable(self):
        rel = global_markets.resolve_instrument("RELIANCE", market="IN")
        self.assertEqual(rel["market"], "IN")
        self.assertEqual(rel["nse_symbol"], "RELIANCE")
        self.assertEqual(rel["bse_scrip_code"], "500325")
        self.assertEqual(rel["bse_code"], "500325")
        self.assertEqual(rel["provider_symbol"], "RELIANCE.BSE")
        self.assertEqual(rel["isin"], "INE002A01018")
        self.assertEqual(rel["currency"], "INR")
        self.assertFalse(rel["nse_bse_interchangeable"])

        rel_by_bse_code = global_markets.resolve_instrument("500325", market="IN")
        self.assertEqual(rel_by_bse_code["symbol"], "RELIANCE")
        self.assertEqual(rel_by_bse_code["provider_symbol"], "RELIANCE.BSE")

        with self.assertRaises(ValueError):
            global_markets.resolve_instrument("500325.NSE", market="IN")

        with self.assertRaises(ValueError):
            global_markets.resolve_instrument("RELIANCE.BSE", market="US")

    def test_native_currencies_preserved_usd_and_inr(self):
        self.assertEqual(global_markets.format_price(195.4, "USD"), "USD $195.40")
        self.assertEqual(global_markets.format_price(2985.75, "INR"), "INR ₹2,985.75")

        in_analysis = engine.analyze(
            "RELIANCE",
            SAMPLE_BSE_SERIES,
            "https://www.alphavantage.co/documentation/#daily",
            market="IN",
            currency="INR",
            exchange="NSE/BSE",
            provider_symbol="RELIANCE.BSE",
        )
        self.assertEqual(in_analysis["market"], "IN")
        self.assertEqual(in_analysis["currency"], "INR")
        self.assertEqual(in_analysis["provider_symbol"], "RELIANCE.BSE")
        self.assertIn("₹", in_analysis["rationale"])
        self.assertIn("INR", in_analysis["rationale"])
        # Price-only technical analysis never ranks a company without primary financial evidence
        self.assertEqual(in_analysis["ranking"], "INSUFFICIENT_EVIDENCE")

    def test_maintainable_calendars_2027_onward_and_special_sessions(self):
        # 2026 & 2027+ US holidays (algorithmic NYSE rules)
        self.assertEqual(global_markets.is_market_holiday("US", dt.date(2026, 7, 3)), "Independence Day (Observed)")
        self.assertEqual(global_markets.is_market_holiday("US", dt.date(2027, 7, 5)), "Independence Day (Observed)")
        self.assertEqual(global_markets.is_market_holiday("US", dt.date(2027, 3, 26)), "Good Friday")
        self.assertEqual(global_markets.is_market_holiday("US", dt.date(2030, 1, 21)), "Martin Luther King, Jr. Day")
        self.assertFalse(global_markets.is_trading_day(dt.date(2027, 7, 5), "US"))

        # 2027+ India holidays (fixed + movable + algorithmic Good Friday)
        self.assertEqual(global_markets.is_market_holiday("IN", dt.date(2027, 1, 26)), "Republic Day")
        self.assertEqual(global_markets.is_market_holiday("IN", dt.date(2027, 3, 26)), "Good Friday")
        self.assertEqual(global_markets.is_market_holiday("IN", dt.date(2029, 8, 15)), "Independence Day")
        self.assertFalse(global_markets.is_trading_day(dt.date(2027, 1, 26), "IN"))

        # Special trading sessions: India Diwali Muhurat Trading (2027-10-29 18:00-19:00 IST)
        muhurat_session = global_markets.get_special_session("IN", dt.date(2027, 10, 29))
        self.assertIsNotNone(muhurat_session)
        self.assertEqual(muhurat_session["type"], "SPECIAL_OPEN")
        self.assertTrue(global_markets.is_trading_day(dt.date(2027, 10, 29), "IN"))
        # 18:30 IST on 2027-10-29 is 13:00 UTC -> market is open during Muhurat session
        muhurat_utc = dt.datetime(2027, 10, 29, 13, 0, tzinfo=ZoneInfo("UTC"))
        in_muhurat_sched = global_markets.get_market_schedule_status("IN", now_utc=muhurat_utc)
        self.assertTrue(in_muhurat_sched["is_open_now"])
        self.assertEqual(in_muhurat_sched["market_phase"], "SPECIAL_SESSION_OPEN")

        # US Black Friday early close (2027-11-26 closes at 13:00 ET)
        bf_session = global_markets.get_special_session("US", dt.date(2027, 11, 26))
        self.assertIsNotNone(bf_session)
        self.assertEqual(bf_session["type"], "EARLY_CLOSE")
        self.assertEqual(bf_session["close_hm"], (13, 0))

    def test_indian_disclosures_strictly_company_specific_and_separate_official_from_news(self):
        with self.assertRaises(ValueError):
            intelligence.sec_filings("RELIANCE", user_agent="TestApp test@example.com", market="IN")
        with self.assertRaises(ValueError):
            intelligence.company_facts("RELIANCE", user_agent="TestApp test@example.com", market="IN")

        disc = intelligence.india_disclosures_for(
            "RELIANCE",
            fetch=lambda url, **kw: SAMPLE_INDIA_MIXED_RSS_XML,
        )
        # Only the official BSE PDF mentioning Reliance is in primary filings
        self.assertEqual(len(disc["filings"]), 1)
        self.assertIn("bseindia.com/xml-data/corpfiling", disc["filings"][0]["url"])
        # The Google News article mentioning Reliance is in secondary_disclosure_mentions, NOT primary filings
        self.assertEqual(len(disc["secondary_disclosure_mentions"]), 1)
        self.assertIn("Reliance Industries Q4 profit", disc["secondary_disclosure_mentions"][0]["title"])
        # Khaitan Chemicals and generic SEBI circular are completely rejected
        all_titles = [x["title"] for x in disc["filings"] + disc["secondary_disclosure_mentions"]]
        self.assertFalse(any("Khaitan" in t for t in all_titles))
        self.assertFalse(any("generic circular" in t for t in all_titles))

    def test_indian_fundamental_metrics_verified_against_official_documents(self):
        disc = intelligence.india_disclosures_for(
            "RELIANCE",
            fetch=lambda url, **kw: SAMPLE_INDIA_MIXED_RSS_XML,
        )
        official_url = disc["filings"][0]["url"]

        # Metric citing an unverified news URL is rejected
        unverified_candidate = {
            "Revenue": {
                "value_inr": 2500000000000,
                "period_end": "2026-03-31",
                "filed": "2026-03-30",
                "company": "Reliance Industries Limited",
                "source_url": "https://news.google.com/rss/articles/CBMi12345",
            }
        }
        res_unverified = intelligence.india_financial_results_for(
            "RELIANCE",
            disclosures=disc,
            candidate_metrics=unverified_candidate,
        )
        self.assertEqual(res_unverified["metrics"], {})
        self.assertFalse(res_unverified["verified_against_official_documents"])

        # Metric citing the verified company-specific official BSE filing PDF is accepted
        verified_candidate = {
            "Revenue": {
                "value_inr": 2500000000000,
                "period_start": "2026-01-01",
                "period_end": "2026-03-31",
                "filed": "2026-03-30",
                "company": "Reliance Industries Limited",
                "source_url": official_url,
            }
        }
        res_verified = intelligence.india_financial_results_for(
            "RELIANCE",
            disclosures=disc,
            candidate_metrics=verified_candidate,
        )
        self.assertTrue(res_verified["verified_against_official_documents"])
        self.assertIn("Revenue", res_verified["metrics"])
        self.assertEqual(res_verified["metrics"]["Revenue"]["value_inr"], 2500000000000)

    def test_never_rank_company_on_portal_urls_headlines_or_incomplete_financials(self):
        price_payload = {
            "status": "OK",
            "market": "IN",
            "currency": "INR",
            "provider_symbol": "RELIANCE.BSE",
            "ohlcv_verified": True,
            "as_of": "2026-03-30",
            "close": 1530.0,
            "ma20": 1480.0,
            "ma50": 1410.0,
            "return20_pct": 6.5,
            "volatility_pct": 18.2,
            "trend": "POSITIVE",
            "source_url": "https://www.alphavantage.co/documentation/",
        }
        # Even when price is OK and official filing link is present, if verified IND-AS metrics are empty,
        # quality is PARTIAL_EVIDENCE and signal MUST be INSUFFICIENT_EVIDENCE (never INVESTIGATE/WATCH/CAUTION)
        rec = aa.analyze_symbol(
            "RELIANCE",
            fetch=lambda url, **kw: SAMPLE_INDIA_MIXED_RSS_XML,
            sec_user_agent="",
            price=price_payload,
            ai_key="",
            market="IN",
        )
        self.assertEqual(rec["quality"], "PARTIAL_EVIDENCE")
        self.assertEqual(rec["research_outcome"], "PARTIAL")
        self.assertEqual(rec["signal"]["label"], "INSUFFICIENT_EVIDENCE")

        # Only when official filings AND verified official metrics AND price are all present does it rank INVESTIGATE
        rec["fundamentals"]["metrics"] = {
            "Revenue": {
                "value_inr": 2500000000000,
                "period_end": "2026-03-31",
                "filed": "2026-03-30",
                "source_url": "https://www.bseindia.com/xml-data/corpfiling/AttachLive/reliance_q4_fy26_results.pdf",
                "verified_official_document": True,
            }
        }
        rec["quality"] = aa.quality_gate(rec)
        rec["research_outcome"] = aa.classify_research_outcome(rec)
        rec["signal"] = aa.decide_signal(rec)
        self.assertEqual(rec["quality"], "PRIMARY_EVIDENCE_PRESENT")
        self.assertEqual(rec["research_outcome"], "COMPLETE")
        self.assertEqual(rec["signal"]["label"], "INVESTIGATE")

    def test_separate_complete_partial_failed_outcomes_and_sanitization(self):
        out_dir = Path(self.tmp.name) / "reports"
        fixed_now = dt.datetime(2026, 3, 30, 23, 0, tzinfo=ZoneInfo("UTC"))

        with patch.object(aa, "analyze_symbol") as mock_analyze:
            def _fake_analyze(sym, *, fetch=None, sec_user_agent="", cik_map=None, price=None, ai_key="", market="US"):
                if sym == "AAPL":
                    quality = "PRIMARY_EVIDENCE_PRESENT"
                    p_status = "OK"
                    outcome = "COMPLETE"
                    label = "INVESTIGATE"
                elif sym == "MSFT":
                    quality = "PARTIAL_EVIDENCE"
                    p_status = "UNAVAILABLE"
                    outcome = "PARTIAL"
                    label = "INSUFFICIENT_EVIDENCE"
                else:
                    quality = "INSUFFICIENT_EVIDENCE"
                    p_status = "ERROR"
                    outcome = "FAILED"
                    label = "INSUFFICIENT_EVIDENCE"
                return {
                    "ticker": sym,
                    "market": market,
                    "exchange": "NYSE/NASDAQ",
                    "currency": "USD",
                    "agents": {},
                    "price": {"status": p_status, "as_of": "2026-03-30", "close": 210.0 if p_status == "OK" else None, "currency": "USD", "trend": "POSITIVE"},
                    "news": [],
                    "filings": {"filings": []},
                    "fundamentals": {"metrics": {}},
                    "pillars": {},
                    "five_pillar_summary": {},
                    "quality": quality,
                    "research_outcome": outcome,
                    "signal": {
                        "label": label,
                        "reason": "Test reason",
                        "as_of": "2026-03-30",
                        "currency": "USD",
                        "evidence_citations": [],
                        "uncertainty": "Historical daily close only.",
                    },
                    "ai": {"status": "NOT_CONFIGURED"},
                }

            mock_analyze.side_effect = _fake_analyze

            mission = aa.run_mission(
                ["AAPL", "MSFT", "NVDA"],
                output_dir=out_dir,
                market="US",
                enforce_once_per_session=True,
                now_utc=fixed_now,
                sleep=lambda _: None,
            )
            self.assertEqual(mission["status"], "PARTIAL")
            self.assertEqual(mission["outcome_counts"], {"COMPLETE": 1, "PARTIAL": 1, "FAILED": 1})
            self.assertEqual(mission["outcomes_by_symbol"], {"COMPLETE": ["AAPL"], "PARTIAL": ["MSFT"], "FAILED": ["NVDA"]})

            md_text = (out_dir / "us_daily_brief.md").read_text(encoding="utf-8")
            self.assertIn("COMPLETE (1)", md_text)
            self.assertIn("PARTIAL (1)", md_text)
            self.assertIn("FAILED (1)", md_text)
            self.assertTrue(aa.verify_report_sanitized(md_text))

            with self.assertRaises(ValueError):
                aa.verify_report_sanitized("https://www.alphavantage.co/query?apikey=SECRET12345678")

    def test_bse_ohlcv_verification_and_unsupported_fallback(self):
        fake_bse_ohlcv = {
            "Meta Data": {"2. Symbol": "RELIANCE.BSE"},
            "Time Series (Daily)": {
                "2026-03-27": {"1. open": "1480.0", "2. high": "1502.0", "3. low": "1475.0", "4. close": "1495.50", "5. volume": "312000"},
                "2026-03-30": {"1. open": "1498.0", "2. high": "1520.0", "3. low": "1490.0", "4. close": "1512.25", "5. volume": "415000"},
            },
        }
        with patch.object(engine, "request_json", return_value=fake_bse_ohlcv):
            ver = engine.verify_bse_symbol_support("RELIANCE", key="dummy", scheduler=engine.RequestScheduler(min_interval=0.0))
        self.assertTrue(ver["supported"])
        self.assertTrue(ver["ohlcv_verified"])
        self.assertEqual(ver["verified_provider_symbol"], "RELIANCE.BSE")
        self.assertEqual(ver["latest_ohlcv"]["close"], 1512.25)
        self.assertEqual(ver["latest_ohlcv"]["volume"], 415000)

        # When unavailable, marks supported=False and status=UNSUPPORTED_BY_PROVIDER
        with patch.object(engine, "request_json", return_value={"Error Message": "Invalid API call"}):
            unsupported = engine.verify_bse_symbol_support("RELIANCE", key="dummy", scheduler=engine.RequestScheduler(min_interval=0.0))
        self.assertFalse(unsupported["supported"])
        self.assertFalse(unsupported["ohlcv_verified"])
        self.assertEqual(unsupported["status"], "UNSUPPORTED_BY_PROVIDER")

    def test_price_discrepancy_never_silently_overwrites_and_withholds_ranking(self):
        # 1. Seed verified 2026-11-13 session for AAPL with close 356.27
        series_356 = [(f"2026-09-{i:02d}", 330.0 + i * 0.2) for i in range(1, 30)] + [
            (f"2026-10-{i:02d}", 340.0 + i * 0.3) for i in range(1, 31)
        ] + [("2026-11-13", 356.27)]
        saved_initial_ts = engine.save_cached_daily(
            "AAPL",
            series_356,
            "https://www.alphavantage.co/documentation/#daily",
            fetched_at_utc="2026-11-15T01:58:43+00:00",
            market="US",
            currency="USD",
        )
        self.assertEqual(saved_initial_ts, "2026-11-15T01:58:43+00:00")
        self.assertEqual(engine.get_price_discrepancies("AAPL", asof="2026-11-13", market="US"), [])

        initial_row = engine.analyze("AAPL", series_356, "https://www.alphavantage.co/documentation/#daily", market="US", currency="USD")
        engine.store(initial_row)

        # 2. Attempt to overwrite the same 2026-11-13 session with conflicting close 272.41
        series_272 = series_356[:-1] + [("2026-11-13", 272.41)]
        conflict_ts = engine.save_cached_daily(
            "AAPL",
            series_272,
            "https://www.alphavantage.co/documentation/#daily",
            fetched_at_utc="2026-11-15T02:10:00+00:00",
            market="US",
            currency="USD",
        )
        # Returns the preserved timestamp rather than overwriting with the conflicting 02:10:00 timestamp
        self.assertEqual(conflict_ts, "2026-11-15T01:58:43+00:00")
        discs = engine.get_price_discrepancies("AAPL", asof="2026-11-13", market="US")
        self.assertEqual(len(discs), 1)
        self.assertEqual(discs[0]["previous_close"], 356.27)
        self.assertEqual(discs[0]["observed_close"], 272.41)

        # Verify original cached close (356.27) was NOT overwritten
        cached_after = engine.get_cached_daily("AAPL", max_age_seconds=None, market="US")
        self.assertEqual(dict(cached_after["rows"])["2026-11-13"], 356.27)

        # Verify engine.latest() flags the discrepancy and withholds any ranking
        latest_rows = engine.latest(market="US")
        self.assertEqual(len(latest_rows), 1)
        self.assertEqual(latest_rows[0]["close"], 356.27)
        self.assertEqual(latest_rows[0]["ranking"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(latest_rows[0]["stance"], "Unresolved price discrepancy")
        self.assertIsNotNone(latest_rows[0]["data_discrepancy"])

        # Verify autonomous_agents.decide_signal also forces INSUFFICIENT_EVIDENCE on price discrepancy
        sig = aa.decide_signal({
            "market": "US",
            "currency": "USD",
            "quality": "PRIMARY_EVIDENCE_PRESENT",
            "price": {
                "status": "DATA_DISCREPANCY_UNRESOLVED",
                "as_of": "2026-11-13",
                "reason": "Conflicting close for 2026-11-13: cached 356.27 vs fetched 272.41",
            },
        })
        self.assertEqual(sig["label"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("discrepancy", sig["reason"].lower())

    def test_general_news_never_primary_and_missing_data_explicit(self):
        # Even if a news headline item is passed inside 'filings', _is_valid_primary_filing_item rejects it
        news_item_disguised = {
            "source": "Google News RSS (headline only)",
            "title": "AAPL stock rises",
            "url": "https://news.google.com/rss/articles/123",
            "published": "Fri, 13 Nov 2026 20:00:00 GMT",
            "is_primary_regulatory_evidence": False,
        }
        self.assertFalse(aa._is_valid_primary_filing_item(news_item_disguised))

        rec = aa.analyze_symbol(
            "RELIANCE",
            fetch=lambda url, **kw: SAMPLE_INDIA_MIXED_RSS_XML,
            market="IN",
        )
        self.assertEqual(rec["signal"]["label"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(len(rec.get("missing_data", [])) >= 1)
        self.assertTrue(len(rec.get("unsupported_conclusions", [])) >= 1)

    def test_duplicate_session_prevention_lock(self):
        out_dir = Path(self.tmp.name) / "dup_reports"
        fixed_now = dt.datetime(2026, 11, 14, 12, 0, tzinfo=ZoneInfo("UTC"))
        calls = []

        def _fake_analyze(sym, **kwargs):
            calls.append(sym)
            return {
                "ticker": sym,
                "market": "US",
                "exchange": "NYSE/NASDAQ",
                "currency": "USD",
                "agents": {},
                "price": {"status": "OK", "as_of": "2026-11-13", "close": 356.27, "currency": "USD", "trend": "POSITIVE"},
                "news": [],
                "filings": {"filings": []},
                "fundamentals": {"metrics": {}},
                "pillars": {},
                "five_pillar_summary": {},
                "missing_data": [],
                "unsupported_conclusions": [],
                "quality": "PARTIAL_EVIDENCE",
                "research_outcome": "PARTIAL",
                "signal": {"label": "INSUFFICIENT_EVIDENCE", "reason": "Test", "as_of": "2026-11-13", "currency": "USD", "evidence_citations": []},
                "ai": {"status": "NOT_CONFIGURED"},
            }

        with patch.object(aa, "analyze_symbol", side_effect=_fake_analyze):
            m1 = aa.run_mission(["AAPL"], output_dir=out_dir, market="US", enforce_once_per_session=True, now_utc=fixed_now, sleep=lambda _: None)
            self.assertFalse(m1.get("skipped_duplicate_session", False))
            self.assertEqual(len(calls), 1)

            # Second scheduled run for the same 2026-11-13 session is skipped automatically
            m2 = aa.run_mission(["AAPL"], output_dir=out_dir, market="US", enforce_once_per_session=True, now_utc=fixed_now, sleep=lambda _: None)
            self.assertTrue(m2.get("skipped_duplicate_session", False))
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
