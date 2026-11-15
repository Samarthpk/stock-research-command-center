from concurrent.futures import ThreadPoolExecutor
import datetime
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from engine import (
    AmbiguousRateLimitError,
    DailyQuotaExhaustedError,
    PerSecondRateLimitError,
    RequestScheduler,
    analyze,
    classify_rate_limit,
    fetch_daily,
    format_freshness_label,
    get_api_key,
    init_db,
    is_data_stale,
    latest,
    report,
    run,
)


def _fake_alpha_vantage_payload(base_price=100.0, days=60):
    start = datetime.date(2026, 8, 1)
    series = {}
    for i in range(days):
        d = (start + datetime.timedelta(days=i)).isoformat()
        series[d] = {"4. close": f"{base_price + i:.2f}"}
    return {"Time Series (Daily)": series}


class VirtualClock:
    def __init__(self, initial=1000.0):
        self.now = float(initial)
        self.lock = threading.Lock()
        self.sleeps = []

    def time(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            self.now += seconds


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(engine, "DB", Path(self.tmpdir.name) / "test_research.db")
        self.db_patch.start()
        init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_trend(self):
        rows = [(f"d{i}", 100 + i) for i in range(60)]
        row = analyze("TEST", rows, "example")
        self.assertEqual(row["stance"], "Positive trend")
        self.assertEqual(row["close"], 159)

    def test_not_enough(self):
        with self.assertRaises(ValueError):
            analyze("A", [("x", 1)], "example")

    def test_no_fabrication(self):
        result = report(
            {
                "checked_at_utc": "now",
                "results": [],
                "errors": [{"symbol": "AAA", "error": "rate limited"}],
            }
        )
        self.assertIn("No successful live data fetches", result)

    def test_get_api_key_from_env(self):
        with patch.dict(os.environ, {"ALPHAVANTAGE_API_KEY": "test_key_123"}):
            self.assertEqual(get_api_key(), "test_key_123")

    def test_rate_limiter_sequential_and_concurrent(self):
        vclock = VirtualClock()
        sched = RequestScheduler(min_interval=1.5, clock=vclock.time, sleeper=vclock.sleep)

        # Sequential calls
        for _ in range(4):
            sched.wait()
        self.assertEqual(len(sched.call_timestamps), 4)
        for i in range(1, len(sched.call_timestamps)):
            gap = sched.call_timestamps[i] - sched.call_timestamps[i - 1]
            self.assertGreaterEqual(gap, 1.5 - 1e-6)

        # Concurrent calls across 6 worker threads using real monotonic clock with 1.5s interval
        vclock2 = VirtualClock()
        sched_concurrent = RequestScheduler(min_interval=1.5, clock=vclock2.time, sleeper=vclock2.sleep)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: sched_concurrent.wait(), range(6)))

        self.assertEqual(len(sched_concurrent.call_timestamps), 6)
        sorted_ts = sorted(sched_concurrent.call_timestamps)
        for i in range(1, len(sorted_ts)):
            self.assertGreaterEqual(sorted_ts[i] - sorted_ts[i - 1], 1.5 - 1e-6)

    def test_classify_rate_limit_distinguishes_per_second_ambiguous_and_daily_quota(self):
        burst_only_msg = (
            "Thank you for using Alpha Vantage! Please consider spreading out your free API requests "
            "more sparingly (1 request per second)."
        )
        combined_generic_msg = (
            "Thank you for using Alpha Vantage! Please consider spreading out your free API requests "
            "more sparingly (1 request per second). You may subscribe to any of the premium plans at "
            "https://www.alphavantage.co/premium/ to lift the free key rate limit (25 requests per day), "
            "feeds, and monthly volume."
        )
        generic_25_per_day_msg = (
            "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day. "
            "Please subscribe to any of the premium plans at https://www.alphavantage.co/premium/ to "
            "instantly remove all daily rate limits."
        )
        explicit_daily_msg = (
            "Daily quota exhausted: you have exceeded your daily rate limit (0 requests remaining today)."
        )

        # Pure burst message -> PerSecondRateLimitError
        self.assertIsInstance(classify_rate_limit(burst_only_msg), PerSecondRateLimitError)
        self.assertNotIsInstance(classify_rate_limit(burst_only_msg), DailyQuotaExhaustedError)

        # Generic combined message mentioning both '1 request per second' and '25 requests per day'
        # must NOT be classified as DailyQuotaExhaustedError
        combined_err = classify_rate_limit(combined_generic_msg)
        self.assertIsInstance(combined_err, AmbiguousRateLimitError)
        self.assertNotIsInstance(combined_err, DailyQuotaExhaustedError)
        self.assertTrue(combined_err.quota_uncertain)

        # Generic '25 requests per day' message without specific exhaustion evidence -> AmbiguousRateLimitError
        generic_err = classify_rate_limit(generic_25_per_day_msg)
        self.assertIsInstance(generic_err, AmbiguousRateLimitError)
        self.assertNotIsInstance(generic_err, DailyQuotaExhaustedError)
        self.assertTrue(generic_err.quota_uncertain)

        # Explicit daily exhaustion -> DailyQuotaExhaustedError
        self.assertIsInstance(classify_rate_limit(explicit_daily_msg), DailyQuotaExhaustedError)

    def test_regression_generic_alpha_vantage_combined_1rps_and_25rpd_retries_with_backoff(self):
        vclock = VirtualClock()
        sched = RequestScheduler(min_interval=1.5, clock=vclock.time, sleeper=vclock.sleep)
        backoff_sleeps = []
        exact_generic_msg = (
            "Thank you for using Alpha Vantage! Please consider spreading out your free API requests "
            "more sparingly (1 request per second). You may subscribe to any of the premium plans at "
            "https://www.alphavantage.co/premium/ to lift the free key rate limit (25 requests per day), "
            "feeds, and monthly volume."
        )
        responses = [
            {"Information": exact_generic_msg},
            _fake_alpha_vantage_payload(180.0, 60),
        ]
        with patch.object(engine, "request_json", side_effect=responses) as mock_req:
            rows, source = fetch_daily(
                "AMZN",
                key="dummy",
                use_cache=False,
                max_retries=2,
                base_backoff=1.5,
                scheduler=sched,
                sleeper=backoff_sleeps.append,
            )
            self.assertEqual(mock_req.call_count, 2)
            self.assertEqual(len(rows), 60)
            self.assertEqual(backoff_sleeps, [1.5])

        # And if ambiguous responses persist across bounded retries, run() exposes quota_uncertain=True
        with patch.object(
            engine,
            "request_json",
            return_value={"Information": exact_generic_msg},
        ) as mock_req2:
            out = run(
                ["GOOGL"],
                key="dummy",
                use_cache=False,
                max_retries=2,
                base_backoff=1.5,
                scheduler=sched,
                sleeper=vclock.sleep,
            )
            self.assertEqual(mock_req2.call_count, 3)
            self.assertFalse(out["quota_exhausted"])
            self.assertTrue(out["quota_uncertain"])
            self.assertEqual(out["quota_status"], "uncertain")
            self.assertEqual(out["errors"][0]["error_type"], "ambiguous_rate_limit")
            self.assertTrue(out["errors"][0]["quota_uncertain"])
            self.assertIn("Remaining daily quota is uncertain", report(out))

    def test_bounded_retry_with_exponential_backoff_on_per_second_limit(self):
        vclock = VirtualClock()
        sched = RequestScheduler(min_interval=1.5, clock=vclock.time, sleeper=vclock.sleep)
        backoff_sleeps = []

        responses = [
            {"Information": "Please consider spreading out your free API requests (1 request per second)."},
            {"Note": "Thank you for using Alpha Vantage! Burst rate limit reached (1 request per second)."},
            _fake_alpha_vantage_payload(100.0, 60),
        ]

        with patch.object(engine, "request_json", side_effect=responses) as mock_req:
            rows, source = fetch_daily(
                "AAPL",
                key="dummy",
                use_cache=False,
                max_retries=3,
                base_backoff=1.5,
                scheduler=sched,
                sleeper=backoff_sleeps.append,
            )
            self.assertEqual(mock_req.call_count, 3)
            self.assertEqual(len(rows), 60)
            self.assertEqual(backoff_sleeps, [1.5, 3.0])

        # Verify bounded retry stops after max_retries
        always_throttled = [
            {"Information": "Please consider spreading out your free API requests (1 request per second)."}
            for _ in range(10)
        ]
        backoff_sleeps_fail = []
        with patch.object(engine, "request_json", side_effect=always_throttled) as mock_req_fail:
            with self.assertRaises(PerSecondRateLimitError):
                fetch_daily(
                    "MSFT",
                    key="dummy",
                    use_cache=False,
                    max_retries=2,
                    base_backoff=1.5,
                    scheduler=sched,
                    sleeper=backoff_sleeps_fail.append,
                )
            # 1 initial attempt + 2 bounded retries = 3 total calls
            self.assertEqual(mock_req_fail.call_count, 3)
            self.assertEqual(backoff_sleeps_fail, [1.5, 3.0])

    def test_daily_quota_exhaustion_stops_retries_immediately(self):
        vclock = VirtualClock()
        sched = RequestScheduler(min_interval=1.5, clock=vclock.time, sleeper=vclock.sleep)
        backoff_sleeps = []
        daily_quota_resp = {
            "Information": "Daily quota exhausted: you have exceeded your daily rate limit (0 requests remaining)."
        }

        with patch.object(engine, "request_json", return_value=daily_quota_resp) as mock_req:
            with self.assertRaises(DailyQuotaExhaustedError):
                fetch_daily(
                    "NVDA",
                    key="dummy",
                    use_cache=False,
                    max_retries=3,
                    base_backoff=1.5,
                    scheduler=sched,
                    sleeper=backoff_sleeps.append,
                )
            # Must not retry when daily quota is confirmed exhausted
            self.assertEqual(mock_req.call_count, 1)
            self.assertEqual(backoff_sleeps, [])

    def test_caching_prevents_duplicate_api_calls_and_labels_stale_data(self):
        vclock = VirtualClock()
        sched = RequestScheduler(min_interval=1.5, clock=vclock.time, sleeper=vclock.sleep)
        payload = _fake_alpha_vantage_payload(200.0, 60)

        with patch.object(engine, "request_json", return_value=payload) as mock_req:
            out1 = run(["AAPL"], key="dummy", use_cache=True, scheduler=sched, sleeper=vclock.sleep)
            self.assertEqual(mock_req.call_count, 1)
            self.assertEqual(out1["counts"], {"success": 1, "cached": 0, "failed": 0, "pending": 0})
            self.assertEqual(out1["results"][0]["data_origin"], "live")

            # Second run for the same symbol uses SQLite cache without calling the API
            out2 = run(["AAPL"], key="dummy", use_cache=True, scheduler=sched, sleeper=vclock.sleep)
            self.assertEqual(mock_req.call_count, 1)
            self.assertEqual(out2["counts"], {"success": 0, "cached": 1, "failed": 0, "pending": 0})
            self.assertEqual(out2["results"][0]["data_origin"], "cached")

        # Verify stale labeling on old market close / old fetch timestamp
        now_utc = datetime.datetime(2026, 11, 14, 20, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(is_data_stale("2026-10-01", fetched_at_utc="2026-10-01T20:00:00+00:00", now_utc=now_utc))
        label = format_freshness_label("2026-10-01", fetched_at_utc="2026-10-01T20:00:00+00:00", data_origin="cached", now_utc=now_utc)
        self.assertIn("Stale", label)

    def test_partial_failures_preserve_success_and_never_fabricate(self):
        vclock = VirtualClock()
        sched = RequestScheduler(min_interval=1.5, clock=vclock.time, sleeper=vclock.sleep)

        def fake_request(url):
            if "symbol=AAPL" in url:
                return _fake_alpha_vantage_payload(150.0, 60)
            if "symbol=MSFT" in url:
                return _fake_alpha_vantage_payload(300.0, 60)
            if "symbol=AMZN" in url:
                return {"Information": "Burst limit (1 request per second)."}
            return {"Information": "Daily quota exhausted: exceeded your daily limit."}

        with patch.object(engine, "request_json", side_effect=fake_request):
            out = run(
                ["AAPL", "MSFT", "AMZN", "GOOGL", "NVDA", "SPY"],
                key="dummy",
                use_cache=True,
                max_retries=1,
                scheduler=sched,
                sleeper=vclock.sleep,
            )

        # AAPL and MSFT succeeded; AMZN failed after 1 retry; GOOGL hit daily quota exhaustion; NVDA & SPY stayed pending
        self.assertEqual([r["symbol"] for r in out["results"]], ["AAPL", "MSFT"])
        self.assertEqual(out["counts"]["success"], 2)
        self.assertEqual(out["counts"]["cached"], 0)
        self.assertEqual(out["counts"]["failed"], 2)
        self.assertEqual(out["counts"]["pending"], 2)
        self.assertEqual(out["pending"], ["NVDA", "SPY"])
        self.assertTrue(out["quota_exhausted"])

        # Stored DB rows contain ONLY AAPL and MSFT — never fabricated signals for failed/pending symbols
        stored_symbols = [r["symbol"] for r in latest()]
        self.assertEqual(stored_symbols, ["AAPL", "MSFT"])

    def test_concurrent_run_pacing_and_deduplication(self):
        vclock = VirtualClock()
        sched = RequestScheduler(min_interval=1.5, clock=vclock.time, sleeper=vclock.sleep)

        def fake_request(url):
            return _fake_alpha_vantage_payload(120.0, 60)

        with patch.object(engine, "request_json", side_effect=fake_request) as mock_req:
            out = run(
                ["AAPL", "MSFT", "NVDA", "GOOGL", "AAPL"],
                key="dummy",
                use_cache=True,
                max_workers=4,
                scheduler=sched,
                sleeper=vclock.sleep,
            )

        # Duplicate 'AAPL' in input list is deduplicated; 4 unique symbols fetched with >=1.5s spacing
        self.assertEqual(mock_req.call_count, 4)
        self.assertEqual(out["counts"], {"success": 4, "cached": 0, "failed": 0, "pending": 0})
        sorted_ts = sorted(sched.call_timestamps)
        self.assertEqual(len(sorted_ts), 4)
        for i in range(1, len(sorted_ts)):
            self.assertGreaterEqual(sorted_ts[i] - sorted_ts[i - 1], 1.5 - 1e-6)


if __name__ == "__main__":
    unittest.main()
