"""Stock research engine. Research signals only; not trading advice."""
import contextlib, os, json, ssl, sqlite3, time, threading, urllib.request, urllib.parse, urllib.error, datetime, statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from global_markets import (
    INDIA_BENCHMARKS,
    INDIA_WATCHLIST_DEFAULT,
    US_WATCHLIST_DEFAULT,
    currency_for_market,
    format_price,
    get_market_schedule,
    latest_completed_session_date,
    normalize_market,
    resolve_instrument,
)

def _resolve_db_path():
    env_db = os.getenv('STOCK_DB')
    if env_db:
        return Path(env_db)
    local_db = Path(__file__).resolve().with_name('research.db')
    if os.access(local_db.parent, os.W_OK):
        return local_db
    return Path('/tmp/research.db')

DB = _resolve_db_path()
API = 'https://www.alphavantage.co/query'
DEFAULT = list(US_WATCHLIST_DEFAULT)
DEFAULT_INDIA = list(INDIA_WATCHLIST_DEFAULT)
MIN_REQUEST_INTERVAL = 1.5
DEFAULT_CACHE_TTL_SECONDS = 12 * 3600  # 12 hours
PRICE_BASIS = 'UNADJUSTED_DAILY_OHLCV (Alpha Vantage TIME_SERIES_DAILY)'
_ENGINE_VERSION = '4.2'

class RateLimitError(RuntimeError):
    """Base error for Alpha Vantage rate limiting."""
    def __init__(self, message, quota_uncertain=False):
        super().__init__(message)
        self.quota_uncertain = bool(quota_uncertain)

class PerSecondRateLimitError(RateLimitError):
    """Temporary burst / per-second / per-minute rate limit that can be retried with backoff."""

class AmbiguousRateLimitError(PerSecondRateLimitError):
    """Ambiguous rate-limit response (e.g. generic '25 requests per day' or mixed burst/daily text)
    that uses conservative bounded backoff and exposes quota uncertainty if retries fail."""
    def __init__(self, message):
        super().__init__(message, quota_uncertain=True)

class DailyQuotaExhaustedError(RateLimitError):
    """Confirmed daily API quota exhaustion. Must not be retried."""
    def __init__(self, message):
        super().__init__(message, quota_uncertain=False)

def classify_rate_limit(message):
    text = (message or '').strip()
    lower = text.lower()
    burst_markers = (
        '1 request per second',
        'requests per second',
        'per second',
        'spreading out',
        'per minute',
        'calls per minute',
        'burst',
    )
    explicit_daily_exhaustion_markers = (
        'daily quota exhausted',
        'daily limit exceeded',
        'daily rate limit exceeded',
        'exceeded your daily',
        'exceeded the daily',
        'reached your daily',
        'daily request limit reached',
        '0 requests remaining',
    )
    generic_daily_mentions = (
        '25 requests per day',
        'requests/day',
        'per day',
        'daily rate limit',
        'daily limit',
        'daily quota',
    )

    has_burst = any(m in lower for m in burst_markers)
    has_explicit_daily = any(m in lower for m in explicit_daily_exhaustion_markers)
    has_generic_daily = any(m in lower for m in generic_daily_mentions)

    if has_explicit_daily and not has_burst:
        return DailyQuotaExhaustedError(text)
    if has_generic_daily:
        return AmbiguousRateLimitError(text)
    return PerSecondRateLimitError(text, quota_uncertain=False)

class RequestScheduler:
    """Thread-safe request scheduler enforcing a minimum interval between API requests."""
    def __init__(self, min_interval=MIN_REQUEST_INTERVAL, clock=time.monotonic, sleeper=time.sleep):
        self.min_interval = max(float(min_interval), MIN_REQUEST_INTERVAL)
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._next_allowed = None
        self.call_timestamps = []

    def wait(self):
        with self._lock:
            now = self._clock()
            if self._next_allowed is None or now >= self._next_allowed:
                delay = 0.0
                scheduled = now
                self._next_allowed = now + self.min_interval
            else:
                delay = self._next_allowed - now
                scheduled = self._next_allowed
                self._next_allowed = scheduled + self.min_interval
        if delay > 0:
            self._sleep(delay)
        with self._lock:
            actual = self._clock()
            self.call_timestamps.append(actual)
            return delay

    def reset(self):
        with self._lock:
            self._next_allowed = None
            self.call_timestamps.clear()

SCHEDULER = RequestScheduler(min_interval=MIN_REQUEST_INTERVAL)
_DB_LOCK = threading.RLock()
_SYMBOL_LOCKS_GUARD = threading.Lock()
_SYMBOL_LOCKS = {}
_RUN_LOCK = threading.Lock()

def _get_symbol_lock(symbol):
    sym = symbol.strip().upper()
    with _SYMBOL_LOCKS_GUARD:
        if sym not in _SYMBOL_LOCKS:
            _SYMBOL_LOCKS[sym] = threading.Lock()
        return _SYMBOL_LOCKS[sym]

def get_api_key():
    key = os.environ.get('ALPHAVANTAGE_API_KEY')
    if key and key.strip():
        return key.strip()
    try:
        import streamlit as st
        if 'ALPHAVANTAGE_API_KEY' in st.secrets:
            val = st.secrets['ALPHAVANTAGE_API_KEY']
            if val and str(val).strip():
                return str(val).strip()
    except Exception:
        pass
    try:
        import tomllib
        for sp in (Path(__file__).resolve().parent / '.streamlit' / 'secrets.toml',
                   Path.home() / '.streamlit' / 'secrets.toml'):
            if sp.is_file():
                val = tomllib.loads(sp.read_text(encoding='utf-8')).get('ALPHAVANTAGE_API_KEY')
                if val and str(val).strip():
                    return str(val).strip()
    except Exception:
        pass
    return None

@contextlib.contextmanager
def _connect_db():
    with _DB_LOCK:
        conn = sqlite3.connect(DB)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

def init_db():
    with _connect_db() as db:
        db.execute(
            'CREATE TABLE IF NOT EXISTS results ('
            'symbol TEXT, asof TEXT, close REAL, change20 REAL, ma20 REAL, ma50 REAL, '
            'volatility REAL, stance TEXT, rationale TEXT, source TEXT, '
            'fetched_at_utc TEXT, data_origin TEXT, market TEXT, currency TEXT, '
            'exchange TEXT, provider_symbol TEXT, price_basis TEXT, '
            'PRIMARY KEY(symbol,asof))'
        )
        cols = {r[1] for r in db.execute('PRAGMA table_info(results)')}
        for col_name in ('fetched_at_utc', 'data_origin', 'market', 'currency', 'exchange', 'provider_symbol', 'price_basis'):
            if col_name not in cols:
                db.execute(f'ALTER TABLE results ADD COLUMN {col_name} TEXT')

        db.execute(
            'CREATE TABLE IF NOT EXISTS daily_cache ('
            'symbol TEXT PRIMARY KEY, rows_json TEXT, source TEXT, '
            'fetched_at_utc TEXT, latest_asof TEXT, market TEXT, currency TEXT, provider_symbol TEXT, price_basis TEXT)'
        )
        cache_cols = {r[1] for r in db.execute('PRAGMA table_info(daily_cache)')}
        for col_name in ('market', 'currency', 'provider_symbol', 'price_basis'):
            if col_name not in cache_cols:
                db.execute(f'ALTER TABLE daily_cache ADD COLUMN {col_name} TEXT')

        db.execute(
            'CREATE TABLE IF NOT EXISTS price_discrepancies ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'symbol TEXT, market TEXT, asof TEXT, '
            'previous_close REAL, previous_fetched_at_utc TEXT, previous_source TEXT, '
            'observed_close REAL, observed_fetched_at_utc TEXT, observed_source TEXT, '
            'price_basis TEXT, status TEXT)'
        )

        db.execute('CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY)')
        if not db.execute('SELECT 1 FROM watchlist LIMIT 1').fetchone():
            db.executemany('INSERT INTO watchlist VALUES (?)', [(s,) for s in DEFAULT])

        db.execute('CREATE TABLE IF NOT EXISTS market_watchlists(market TEXT, symbol TEXT, position INTEGER, PRIMARY KEY(market, symbol))')
        if not db.execute("SELECT 1 FROM market_watchlists WHERE market='US' LIMIT 1").fetchone():
            us_existing = [r[0] for r in db.execute('SELECT symbol FROM watchlist ORDER BY rowid')] or DEFAULT
            db.executemany(
                'INSERT OR IGNORE INTO market_watchlists(market, symbol, position) VALUES (?,?,?)',
                [('US', s, idx) for idx, s in enumerate(us_existing)],
            )
        if not db.execute("SELECT 1 FROM market_watchlists WHERE market='IN' LIMIT 1").fetchone():
            db.executemany(
                'INSERT OR IGNORE INTO market_watchlists(market, symbol, position) VALUES (?,?,?)',
                [('IN', s, idx) for idx, s in enumerate(DEFAULT_INDIA)],
            )

def watchlist(market='US'):
    init_db()
    mkt = normalize_market(market)
    with _connect_db() as db:
        if mkt == 'ALL':
            us = [r[0] for r in db.execute("SELECT symbol FROM market_watchlists WHERE market='US' ORDER BY position, symbol")]
            ind = [r[0] for r in db.execute("SELECT symbol FROM market_watchlists WHERE market='IN' ORDER BY position, symbol")]
            return us + ind
        if mkt == 'IN':
            return [r[0] for r in db.execute("SELECT symbol FROM market_watchlists WHERE market='IN' ORDER BY position, symbol")]
        return [r[0] for r in db.execute('SELECT symbol FROM watchlist ORDER BY symbol')]

def set_watchlist(symbols, market='US'):
    mkt = normalize_market(market)
    if mkt == 'ALL':
        raise ValueError('Select US or India specifically to edit a market watchlist')
    raw_vals = list(dict.fromkeys(x.strip().upper() for x in symbols if x and x.strip()))
    if not raw_vals:
        raise ValueError('Watchlist cannot be empty')
    if len(raw_vals) > 20:
        raise ValueError('Maximum 20 tickers')
    resolved = []
    for s in raw_vals:
        if any(not c.isalnum() and c not in ('.', '-') for c in s) or len(s) > 15:
            raise ValueError('Invalid ticker')
        inst = resolve_instrument(s, market=mkt)
        resolved.append(inst['symbol'])
    vals = list(dict.fromkeys(resolved))
    init_db()
    with _connect_db() as db:
        if mkt == 'US':
            db.execute('DELETE FROM watchlist')
            db.executemany('INSERT INTO watchlist VALUES (?)', [(s,) for s in vals])
        db.execute('DELETE FROM market_watchlists WHERE market = ?', (mkt,))
        db.executemany(
            'INSERT INTO market_watchlists(market, symbol, position) VALUES (?,?,?)',
            [(mkt, s, idx) for idx, s in enumerate(vals)],
        )

def _ssl_context():
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()['x509_ca'] == 0 and Path('/etc/ssl/cert.pem').exists():
        ctx.load_verify_locations(cafile='/etc/ssl/cert.pem')
    return ctx

def request_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'StockResearchCommandCenter/0.4 (personal research)'})
    with urllib.request.urlopen(req,timeout=25,context=_ssl_context()) as response:
        return json.load(response)

def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

def is_data_stale(asof_str, fetched_at_utc=None, now_utc=None, ttl_seconds=DEFAULT_CACHE_TTL_SECONDS, market='US'):
    now_utc = now_utc or _now_utc()
    mkt = 'IN' if str(market or 'US').upper() in ('IN', 'INDIA') else 'US'
    stale_by_asof = False
    if asof_str:
        try:
            asof_date = datetime.date.fromisoformat(str(asof_str)[:10])
            expected_str = latest_completed_session_date(mkt, now_utc)
            expected_date = datetime.date.fromisoformat(expected_str)
            # Allow 1 trading day lag for post-close provider ingestion; older than 3 calendar days past expected session is stale
            if (expected_date - asof_date).days > 3:
                stale_by_asof = True
        except ValueError:
            pass

    stale_by_fetch = False
    if fetched_at_utc and ttl_seconds is not None:
        try:
            dt = datetime.datetime.fromisoformat(fetched_at_utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            if (now_utc - dt).total_seconds() > ttl_seconds:
                stale_by_fetch = True
        except ValueError:
            pass

    return bool(stale_by_asof or stale_by_fetch)

def format_freshness_label(asof_str, fetched_at_utc=None, data_origin='cached', now_utc=None, market='US'):
    stale = is_data_stale(asof_str, fetched_at_utc=fetched_at_utc, now_utc=now_utc, market=market)
    ts_short = fetched_at_utc[:19].replace('T', ' ') + ' UTC' if fetched_at_utc else 'unknown time'
    if stale:
        return f'⚠️ Stale (as of {asof_str}, cached {ts_short})'
    if data_origin == 'live':
        return f'Live fetch ({ts_short})'
    return f'Cached ({ts_short})'

def record_price_discrepancy(
    symbol,
    asof,
    previous_close,
    observed_close,
    *,
    market='US',
    previous_fetched_at_utc='',
    previous_source='',
    observed_fetched_at_utc=None,
    observed_source='',
    price_basis=PRICE_BASIS,
    status='UNRESOLVED_DISCREPANCY',
):
    init_db()
    sym = symbol.strip().upper()
    mkt = normalize_market(market)
    obs_ts = observed_fetched_at_utc or _now_utc().isoformat()
    with _connect_db() as db:
        db.execute(
            'INSERT INTO price_discrepancies '
            '(symbol, market, asof, previous_close, previous_fetched_at_utc, previous_source, '
            'observed_close, observed_fetched_at_utc, observed_source, price_basis, status) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                sym,
                mkt,
                asof,
                float(previous_close),
                previous_fetched_at_utc or '',
                previous_source or '',
                float(observed_close),
                obs_ts,
                observed_source or '',
                price_basis,
                status,
            ),
        )
    return {
        'symbol': sym,
        'market': mkt,
        'asof': asof,
        'previous_close': float(previous_close),
        'previous_fetched_at_utc': previous_fetched_at_utc or '',
        'previous_source': previous_source or '',
        'observed_close': float(observed_close),
        'observed_fetched_at_utc': obs_ts,
        'observed_source': observed_source or '',
        'price_basis': price_basis,
        'status': status,
    }


def get_price_discrepancies(symbol=None, asof=None, market=None):
    init_db()
    clauses = []
    params = []
    if symbol:
        clauses.append('symbol = ?')
        params.append(symbol.strip().upper())
    if asof:
        clauses.append('asof = ?')
        params.append(str(asof).strip())
    if market:
        clauses.append('market = ?')
        params.append(normalize_market(market))
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    with _connect_db() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f'SELECT * FROM price_discrepancies{where} ORDER BY id DESC', params).fetchall()
    return [dict(r) for r in rows]


def get_cached_daily(symbol, max_age_seconds=DEFAULT_CACHE_TTL_SECONDS, now_utc=None, market=None):
    init_db()
    sym = symbol.strip().upper()
    now_utc = now_utc or _now_utc()
    with _connect_db() as db:
        row = db.execute(
            'SELECT rows_json, source, fetched_at_utc, latest_asof, market, currency, provider_symbol, price_basis '
            'FROM daily_cache WHERE symbol = ?',
            (sym,)
        ).fetchone()
    if not row:
        return None
    rows_json, source, fetched_at_utc, latest_asof, row_mkt, row_curr, prov_sym, p_basis = row
    if market and row_mkt and normalize_market(market) != normalize_market(row_mkt):
        return None
    try:
        parsed_rows = [(d, float(p)) for d, p in json.loads(rows_json)]
    except Exception:
        return None
    age_seconds = None
    if fetched_at_utc:
        try:
            dt = datetime.datetime.fromisoformat(fetched_at_utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            age_seconds = (now_utc - dt).total_seconds()
        except ValueError:
            age_seconds = None
    if max_age_seconds is not None and age_seconds is not None and age_seconds > max_age_seconds:
        return None
    return {
        'symbol': sym,
        'rows': parsed_rows,
        'source': source,
        'fetched_at_utc': fetched_at_utc,
        'latest_asof': latest_asof,
        'age_seconds': age_seconds,
        'market': row_mkt or (market or 'US'),
        'currency': row_curr or currency_for_market(row_mkt or market or 'US'),
        'provider_symbol': prov_sym or sym,
        'price_basis': p_basis or PRICE_BASIS,
    }


def save_cached_daily(
    symbol,
    rows,
    source,
    fetched_at_utc=None,
    market='US',
    currency=None,
    provider_symbol=None,
    allow_overwrite_on_conflict=False,
):
    init_db()
    sym = symbol.strip().upper()
    mkt = normalize_market(market)
    curr = currency or currency_for_market(mkt)
    prov = provider_symbol or sym
    fetched_at_utc = fetched_at_utc or _now_utc().isoformat()
    latest_asof = rows[-1][0] if rows else ''

    # Data Integrity Guard: Check whether an existing cached series for the same symbol/market
    # has a materially different close price for the same trading session date.
    existing = get_cached_daily(sym, max_age_seconds=None, market=mkt)
    if existing and existing.get('rows') and rows:
        existing_map = dict(existing['rows'])
        for d, new_p in rows:
            if d in existing_map and abs(float(existing_map[d]) - float(new_p)) > 0.005:
                record_price_discrepancy(
                    sym,
                    d,
                    existing_map[d],
                    new_p,
                    market=mkt,
                    previous_fetched_at_utc=existing.get('fetched_at_utc') or '',
                    previous_source=existing.get('source') or '',
                    observed_fetched_at_utc=fetched_at_utc,
                    observed_source=source,
                    price_basis=PRICE_BASIS,
                )
                if not allow_overwrite_on_conflict:
                    # Do NOT silently overwrite inconsistent cached evidence
                    return existing.get('fetched_at_utc') or fetched_at_utc

    with _connect_db() as db:
        db.execute(
            'INSERT OR REPLACE INTO daily_cache '
            '(symbol, rows_json, source, fetched_at_utc, latest_asof, market, currency, provider_symbol, price_basis) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (sym, json.dumps(rows), source, fetched_at_utc, latest_asof, mkt, curr, prov, PRICE_BASIS)
        )
    return fetched_at_utc

def verify_bse_symbol_support(symbol, key=None, scheduler=None):
    """Test Alpha Vantage historical OHLCV support for an Indian BSE symbol before enabling it.

    If OHLCV bars are unavailable, explicitly marks the symbol UNSUPPORTED rather than
    claiming Indian market-data functionality.
    """
    inst = resolve_instrument(symbol, market='IN')
    key = key or get_api_key()
    if not key:
        return {
            'symbol': inst['symbol'],
            'bse_ticker': inst['bse_ticker'],
            'bse_scrip_code': inst['bse_scrip_code'],
            'nse_symbol': inst['nse_symbol'],
            'supported': False,
            'ohlcv_verified': False,
            'status': 'UNSUPPORTED_UNVERIFIED_MISSING_API_KEY',
            'currency': 'INR',
            'note': 'Alpha Vantage API key not present in environment; Indian BSE OHLCV retrieval cannot be claimed.',
        }
    sched = scheduler if scheduler is not None else SCHEDULER
    candidates = [inst['bse_ticker']]
    if inst.get('bse_numeric_ticker') and inst['bse_numeric_ticker'] not in candidates:
        candidates.append(inst['bse_numeric_ticker'])

    for cand in candidates:
        sched.wait()
        url = API + '?' + urllib.parse.urlencode({
            'function': 'TIME_SERIES_DAILY',
            'symbol': cand,
            'outputsize': 'compact',
            'apikey': key,
        })
        try:
            obj = request_json(url)
        except Exception as e:
            return {
                'symbol': inst['symbol'],
                'tested_ticker': cand,
                'supported': False,
                'ohlcv_verified': False,
                'status': f'UNSUPPORTED_NETWORK_ERROR: {type(e).__name__}',
                'currency': 'INR',
            }
        if 'Time Series (Daily)' in obj and obj['Time Series (Daily)']:
            ts = obj['Time Series (Daily)']
            dates = sorted(ts.keys())
            latest_bar = ts[dates[-1]]
            ohlcv_keys = ('1. open', '2. high', '3. low', '4. close', '5. volume')
            has_full_ohlcv = all(k in latest_bar for k in ohlcv_keys)
            try:
                latest_ohlcv = {
                    'open': float(latest_bar['1. open']),
                    'high': float(latest_bar['2. high']),
                    'low': float(latest_bar['3. low']),
                    'close': float(latest_bar['4. close']),
                    'volume': int(float(latest_bar['5. volume'])),
                } if has_full_ohlcv else {
                    'close': float(latest_bar['4. close']),
                }
            except (KeyError, ValueError, TypeError):
                continue
            return {
                'symbol': inst['symbol'],
                'verified_provider_symbol': cand,
                'bse_scrip_code': inst['bse_scrip_code'],
                'nse_symbol': inst['nse_symbol'],
                'supported': True,
                'ohlcv_verified': has_full_ohlcv,
                'latest_ohlcv': latest_ohlcv,
                'status': 'VERIFIED_OHLCV_SUPPORTED' if has_full_ohlcv else 'VERIFIED_CLOSE_ONLY',
                'latest_asof': dates[-1],
                'sessions_count': len(dates),
                'currency': 'INR',
            }
        if 'Note' in obj or 'Information' in obj:
            return {
                'symbol': inst['symbol'],
                'tested_ticker': cand,
                'supported': False,
                'ohlcv_verified': False,
                'status': 'UNVERIFIED_RATE_LIMITED_BY_PROVIDER',
                'currency': 'INR',
            }
    return {
        'symbol': inst['symbol'],
        'tested_candidates': candidates,
        'supported': False,
        'ohlcv_verified': False,
        'status': 'UNSUPPORTED_BY_PROVIDER',
        'currency': 'INR',
    }

def research_indian_benchmarks(key=None, scheduler=None):
    """Research NIFTY 50 and SENSEX benchmark availability separately without fabricating index data."""
    key = key or get_api_key()
    sched = scheduler if scheduler is not None else SCHEDULER
    report_out = {}
    for name, meta in INDIA_BENCHMARKS.items():
        entry = {
            'benchmark': name,
            'exchange': meta['exchange'],
            'currency': meta['currency'],
            'candidates_tested': [],
            'direct_index_available': False,
            'status': 'UNVERIFIED_OFFLINE',
            'notes': meta['notes'],
        }
        if key:
            for cand in meta['alpha_vantage_candidates']:
                sched.wait()
                url = API + '?' + urllib.parse.urlencode({
                    'function': 'TIME_SERIES_DAILY',
                    'symbol': cand,
                    'outputsize': 'compact',
                    'apikey': key,
                })
                try:
                    obj = request_json(url)
                    has_series = bool(obj.get('Time Series (Daily)'))
                    entry['candidates_tested'].append({
                        'symbol': cand,
                        'has_daily_series': has_series,
                        'is_etf_proxy': 'BEES' in cand,
                    })
                    if has_series and 'BEES' not in cand:
                        entry['direct_index_available'] = True
                        entry['status'] = 'DIRECT_INDEX_AVAILABLE'
                    elif has_series and entry['status'] != 'DIRECT_INDEX_AVAILABLE':
                        entry['status'] = 'ETF_PROXY_ONLY_DIRECT_INDEX_UNAVAILABLE'
                except Exception as e:
                    entry['candidates_tested'].append({'symbol': cand, 'error': type(e).__name__})
            if entry['status'] == 'UNVERIFIED_OFFLINE':
                entry['status'] = 'UNSUPPORTED_ON_FREE_ALPHAVANTAGE'
        report_out[name] = entry
    return report_out

def fetch_daily(
    symbol,
    key=None,
    use_cache=True,
    force_refresh=False,
    cache_ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
    max_retries=3,
    base_backoff=1.5,
    scheduler=None,
    sleeper=time.sleep,
    return_metadata=False,
    market='US',
):
    mkt = normalize_market(market)
    inst = resolve_instrument(symbol, market=mkt)
    canonical_sym = inst['symbol']
    provider_sym = inst['provider_symbol']
    currency = inst['currency']
    sched = scheduler if scheduler is not None else SCHEDULER
    sym_lock = _get_symbol_lock(f'{mkt}:{canonical_sym}')

    with sym_lock:
        if use_cache and not force_refresh:
            cached = get_cached_daily(canonical_sym, max_age_seconds=cache_ttl_seconds, market=mkt)
            if cached and cached['rows']:
                if return_metadata:
                    return cached['rows'], cached['source'], {
                        'data_origin': 'cached',
                        'fetched_at_utc': cached['fetched_at_utc'],
                        'market': mkt,
                        'currency': currency,
                        'provider_symbol': cached.get('provider_symbol') or provider_sym,
                        'exchange': inst['exchange'],
                    }
                return cached['rows'], cached['source']

        key = key or get_api_key()
        if not key:
            raise RuntimeError('Missing ALPHAVANTAGE_API_KEY')

        # For Indian BSE equities, try canonical <SYMBOL>.BSE first, then numeric <BSE_SCRIP>.BSE if applicable
        candidate_provider_syms = [provider_sym]
        if mkt == 'IN' and inst.get('bse_numeric_ticker') and inst['bse_numeric_ticker'] not in candidate_provider_syms:
            candidate_provider_syms.append(inst['bse_numeric_ticker'])

        last_err = None
        for prov_candidate in candidate_provider_syms:
            url = API + '?' + urllib.parse.urlencode({
                'function': 'TIME_SERIES_DAILY',
                'symbol': prov_candidate,
                'outputsize': 'compact',
                'apikey': key,
            })

            for attempt in range(max_retries + 1):
                sched.wait()
                try:
                    obj = request_json(url)
                except urllib.error.HTTPError as e:
                    if e.code in (429, 503) and attempt < max_retries:
                        sleeper(base_backoff * (2 ** attempt))
                        continue
                    raise RuntimeError(f'HTTP {e.code} fetching {prov_candidate}: {e.reason}') from e

                if 'Note' in obj or 'Information' in obj:
                    msg = obj.get('Note') or obj.get('Information')
                    rate_err = classify_rate_limit(msg)
                    if isinstance(rate_err, DailyQuotaExhaustedError):
                        raise rate_err
                    last_err = rate_err
                    if attempt < max_retries:
                        sleeper(base_backoff * (2 ** attempt))
                        continue
                    raise rate_err

                if 'Error Message' in obj:
                    last_err = RuntimeError(obj['Error Message'])
                    break  # Try numeric BSE scrip fallback if available

                data = obj.get('Time Series (Daily)')
                if not data:
                    last_err = RuntimeError(f'No valid daily data for {prov_candidate}: {str(obj)[:150]}')
                    break

                rows = sorted([(d, float(v['4. close'])) for d, v in data.items()], key=lambda x: x[0])
                source = 'https://www.alphavantage.co/documentation/#daily'
                fetched_at_utc = save_cached_daily(
                    canonical_sym,
                    rows,
                    source,
                    market=mkt,
                    currency=currency,
                    provider_symbol=prov_candidate,
                )
                if return_metadata:
                    return rows, source, {
                        'data_origin': 'live',
                        'fetched_at_utc': fetched_at_utc,
                        'market': mkt,
                        'currency': currency,
                        'provider_symbol': prov_candidate,
                        'exchange': inst['exchange'],
                    }
                return rows, source

        raise last_err or RuntimeError(f'Failed to fetch {canonical_sym}')

def analyze(symbol, rows, source, fetched_at_utc=None, data_origin='live', market='US', currency=None, exchange=None, provider_symbol=None):
    if len(rows)<51: raise ValueError('Need at least 51 trading days for MA50 and 20-day momentum')
    mkt = 'IN' if str(market or 'US').upper() in ('IN', 'INDIA') or symbol.upper() in DEFAULT_INDIA else 'US'
    curr = currency or currency_for_market(mkt)
    curr_sym = '₹' if curr == 'INR' else '$'
    prices=[p for _,p in rows]
    last=prices[-1]; ma20=statistics.mean(prices[-20:]);ma50=statistics.mean(prices[-50:])
    momentum=last/prices[-21]-1
    returns=[prices[i]/prices[i-1]-1 for i in range(len(prices)-19,len(prices))]
    vol=statistics.stdev(returns)*(252**0.5) if len(returns)>1 else 0
    if last>ma20>ma50 and momentum>0.02:
        stance='Positive trend'
    elif last<ma20<ma50 and momentum< -0.02:
        stance='Negative trend'
    else:
        stance='Mixed / watch'
    # Price-only technical screening never issues INVESTIGATE/WATCH/CAUTION rankings without verified financial filings & metrics
    ranking='INSUFFICIENT_EVIDENCE'
    asof = rows[-1][0]
    fetched_at_utc = fetched_at_utc or _now_utc().isoformat()
    stale = is_data_stale(asof, fetched_at_utc=fetched_at_utc, market=mkt)
    freshness = format_freshness_label(asof, fetched_at_utc=fetched_at_utc, data_origin=data_origin, market=mkt)
    discrepancies = get_price_discrepancies(symbol=symbol, asof=asof, market=mkt)
    if discrepancies:
        stance = 'Unresolved price discrepancy'
        ranking = 'INSUFFICIENT_EVIDENCE'
    rationale=f'Close {curr_sym}{last:.2f} ({curr}); MA20 {curr_sym}{ma20:.2f}; MA50 {curr_sym}{ma50:.2f}; 20-session return {momentum:+.1%}; annualized recent volatility {vol:.1%}. Technical trend only; no valuation or news analysis.'
    if discrepancies:
        d0 = discrepancies[0]
        rationale += f" [DATA INTEGRITY ALERT: conflicting close prices observed for {asof}: {d0['previous_close']} vs {d0['observed_close']}. Ranking withheld.]"
    return dict(
        symbol=symbol,
        market=mkt,
        currency=curr,
        exchange=exchange or ('BSE/NSE' if mkt == 'IN' else 'NYSE/NASDAQ'),
        provider_symbol=provider_symbol or (f'{symbol}.BSE' if mkt == 'IN' else symbol),
        price_basis=PRICE_BASIS,
        asof=asof,
        close=round(last,2),
        change20=round(momentum*100,2),
        ma20=round(ma20,2),
        ma50=round(ma50,2),
        volatility=round(vol*100,2),
        stance=stance,
        ranking=ranking,
        rationale=rationale,
        source=source,
        fetched_at_utc=fetched_at_utc,
        data_origin=data_origin,
        stale=stale,
        freshness=freshness,
        data_discrepancy=discrepancies[0] if discrepancies else None,
    )

def store(row, allow_overwrite_on_conflict=False):
    init_db()
    keys=[
        'symbol','asof','close','change20','ma20','ma50','volatility',
        'stance','rationale','source','fetched_at_utc','data_origin',
        'market','currency','exchange','provider_symbol','price_basis'
    ]
    vals = [row.get(k) for k in keys]
    if not vals[10]:
        vals[10] = _now_utc().isoformat()
    if not vals[11]:
        vals[11] = 'live'
    if not vals[12]:
        vals[12] = 'IN' if str(row.get('symbol', '')).upper() in DEFAULT_INDIA else 'US'
    if not vals[13]:
        vals[13] = currency_for_market(vals[12])
    if not vals[14]:
        vals[14] = 'BSE/NSE' if vals[12] == 'IN' else 'NYSE/NASDAQ'
    if not vals[15]:
        vals[15] = f"{vals[0]}.BSE" if vals[12] == 'IN' else vals[0]
    if not vals[16]:
        vals[16] = PRICE_BASIS

    with _connect_db() as db:
        existing = db.execute(
            'SELECT close, fetched_at_utc, source FROM results WHERE symbol = ? AND asof = ?',
            (vals[0], vals[1]),
        ).fetchone()
    if existing and vals[2] is not None and existing[0] is not None and abs(float(existing[0]) - float(vals[2])) > 0.005:
        record_price_discrepancy(
            vals[0],
            vals[1],
            existing[0],
            vals[2],
            market=vals[12],
            previous_fetched_at_utc=existing[1] or '',
            previous_source=existing[2] or '',
            observed_fetched_at_utc=vals[10],
            observed_source=vals[9] or '',
            price_basis=vals[16],
        )
        if not allow_overwrite_on_conflict:
            return

    with _connect_db() as db:
        db.execute(
            'INSERT OR REPLACE INTO results '
            '(symbol,asof,close,change20,ma20,ma50,volatility,stance,rationale,source,'
            'fetched_at_utc,data_origin,market,currency,exchange,provider_symbol,price_basis) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            vals
        )

def latest(market=None):
    init_db()
    with _connect_db() as db:
        db.row_factory=sqlite3.Row
        raw_rows = [
            dict(r) for r in db.execute(
                'SELECT r.* FROM results r INNER JOIN '
                '(SELECT symbol, MAX(asof) AS asof FROM results GROUP BY symbol) q '
                'ON r.symbol=q.symbol AND r.asof=q.asof ORDER BY symbol'
            )
        ]
    now_utc = _now_utc()
    filtered = []
    target_mkt = normalize_market(market) if market else None
    for r in raw_rows:
        row_mkt = r.get('market') or ('IN' if r['symbol'] in DEFAULT_INDIA else 'US')
        r['market'] = row_mkt
        r['currency'] = r.get('currency') or currency_for_market(row_mkt)
        r['exchange'] = r.get('exchange') or ('BSE/NSE' if row_mkt == 'IN' else 'NYSE/NASDAQ')
        r['provider_symbol'] = r.get('provider_symbol') or (f"{r['symbol']}.BSE" if row_mkt == 'IN' else r['symbol'])
        r['price_basis'] = r.get('price_basis') or PRICE_BASIS
        if target_mkt and target_mkt != 'ALL' and row_mkt != target_mkt:
            continue
        origin = r.get('data_origin') or 'cached'
        fetched_at = r.get('fetched_at_utc')
        r['stale'] = is_data_stale(r.get('asof'), fetched_at_utc=fetched_at, now_utc=now_utc, market=row_mkt)
        r['freshness'] = format_freshness_label(r.get('asof'), fetched_at_utc=fetched_at, data_origin=origin, now_utc=now_utc, market=row_mkt)
        r['ranking'] = 'INSUFFICIENT_EVIDENCE'
        discs = get_price_discrepancies(symbol=r['symbol'], asof=r.get('asof'), market=row_mkt)
        r['data_discrepancy'] = discs[0] if discs else None
        if discs:
            r['stance'] = 'Unresolved price discrepancy'
        filtered.append(r)
    return filtered

def _infer_symbol_market(sym, default_market='US'):
    mkt = normalize_market(default_market)
    if mkt in ('US', 'IN'):
        return mkt
    s = sym.strip().upper()
    if s in DEFAULT_INDIA or s.endswith(('.BSE', '.BO', '.NSE', '.NS')):
        return 'IN'
    return 'US'

def run(
    symbols=None,
    key=None,
    use_cache=True,
    force_refresh=False,
    cache_ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
    max_retries=3,
    base_backoff=1.5,
    scheduler=None,
    sleeper=time.sleep,
    max_workers=1,
    progress_callback=None,
    market='US',
):
    mkt_mode = normalize_market(market)
    raw_symbols = symbols if symbols is not None else watchlist(market=mkt_mode)
    ordered_symbols = list(dict.fromkeys(s.strip().upper() for s in raw_symbols if s and s.strip()))
    results = []
    live_results = []
    cached_results = []
    errors = []
    pending = list(ordered_symbols)
    quota_exhausted = False
    quota_uncertain = False
    quota_status = 'ok'

    def _notify():
        if progress_callback:
            progress_callback({
                'success': len(live_results),
                'cached': len(cached_results),
                'failed': len(errors),
                'pending': len(pending),
            })

    _notify()

    to_fetch = []
    for s in ordered_symbols:
        sym_mkt = _infer_symbol_market(s, mkt_mode)
        inst = resolve_instrument(s, market=sym_mkt)
        canon = inst['symbol']
        if use_cache and not force_refresh:
            cached = get_cached_daily(canon, max_age_seconds=cache_ttl_seconds, market=sym_mkt)
            if cached and cached['rows']:
                try:
                    row = analyze(
                        canon,
                        cached['rows'],
                        cached['source'],
                        fetched_at_utc=cached['fetched_at_utc'],
                        data_origin='cached',
                        market=sym_mkt,
                        currency=inst['currency'],
                        exchange=inst['exchange'],
                        provider_symbol=cached.get('provider_symbol') or inst['provider_symbol'],
                    )
                    store(row)
                    results.append(row)
                    cached_results.append(row)
                    pending.remove(s)
                    _notify()
                    continue
                except Exception as e:
                    errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'analysis_error', 'quota_uncertain': False})
                    pending.remove(s)
                    _notify()
                    continue
        to_fetch.append((s, sym_mkt, inst))

    if max_workers and max_workers > 1 and len(to_fetch) > 1:
        stop_event = threading.Event()
        state_lock = threading.Lock()

        def _worker(item):
            nonlocal quota_exhausted, quota_uncertain
            orig_s, sym_mkt, inst = item
            canon = inst['symbol']
            if stop_event.is_set():
                return
            try:
                rows, source, meta = fetch_daily(
                    orig_s,
                    key=key,
                    use_cache=use_cache,
                    force_refresh=force_refresh,
                    cache_ttl_seconds=cache_ttl_seconds,
                    max_retries=max_retries,
                    base_backoff=base_backoff,
                    scheduler=scheduler,
                    sleeper=sleeper,
                    return_metadata=True,
                    market=sym_mkt,
                )
                row = analyze(
                    canon,
                    rows,
                    source,
                    fetched_at_utc=meta['fetched_at_utc'],
                    data_origin=meta['data_origin'],
                    market=sym_mkt,
                    currency=meta.get('currency', inst['currency']),
                    exchange=meta.get('exchange', inst['exchange']),
                    provider_symbol=meta.get('provider_symbol', inst['provider_symbol']),
                )
                store(row)
                with state_lock:
                    results.append(row)
                    if meta['data_origin'] == 'cached':
                        cached_results.append(row)
                    else:
                        live_results.append(row)
                    if orig_s in pending:
                        pending.remove(orig_s)
                    _notify()
            except DailyQuotaExhaustedError as e:
                stop_event.set()
                with state_lock:
                    quota_exhausted = True
                    errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'daily_quota', 'quota_uncertain': False})
                    if orig_s in pending:
                        pending.remove(orig_s)
                    _notify()
            except AmbiguousRateLimitError as e:
                with state_lock:
                    quota_uncertain = True
                    errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'ambiguous_rate_limit', 'quota_uncertain': True})
                    if orig_s in pending:
                        pending.remove(orig_s)
                    _notify()
            except PerSecondRateLimitError as e:
                with state_lock:
                    errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'rate_limit', 'quota_uncertain': bool(getattr(e, 'quota_uncertain', False))})
                    if orig_s in pending:
                        pending.remove(orig_s)
                    _notify()
            except Exception as e:
                with state_lock:
                    errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'api_error', 'quota_uncertain': False})
                    if orig_s in pending:
                        pending.remove(orig_s)
                    _notify()

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_worker, to_fetch))
    else:
        for orig_s, sym_mkt, inst in to_fetch:
            canon = inst['symbol']
            try:
                rows, source, meta = fetch_daily(
                    orig_s,
                    key=key,
                    use_cache=use_cache,
                    force_refresh=force_refresh,
                    cache_ttl_seconds=cache_ttl_seconds,
                    max_retries=max_retries,
                    base_backoff=base_backoff,
                    scheduler=scheduler,
                    sleeper=sleeper,
                    return_metadata=True,
                    market=sym_mkt,
                )
                row = analyze(
                    canon,
                    rows,
                    source,
                    fetched_at_utc=meta['fetched_at_utc'],
                    data_origin=meta['data_origin'],
                    market=sym_mkt,
                    currency=meta.get('currency', inst['currency']),
                    exchange=meta.get('exchange', inst['exchange']),
                    provider_symbol=meta.get('provider_symbol', inst['provider_symbol']),
                )
                store(row)
                results.append(row)
                if meta['data_origin'] == 'cached':
                    cached_results.append(row)
                else:
                    live_results.append(row)
                pending.remove(orig_s)
                _notify()
            except DailyQuotaExhaustedError as e:
                quota_exhausted = True
                errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'daily_quota', 'quota_uncertain': False})
                pending.remove(orig_s)
                _notify()
                break
            except AmbiguousRateLimitError as e:
                quota_uncertain = True
                errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'ambiguous_rate_limit', 'quota_uncertain': True})
                pending.remove(orig_s)
                _notify()
            except PerSecondRateLimitError as e:
                errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'rate_limit', 'quota_uncertain': bool(getattr(e, 'quota_uncertain', False))})
                pending.remove(orig_s)
                _notify()
            except Exception as e:
                errors.append({'symbol': canon, 'market': sym_mkt, 'error': str(e), 'error_type': 'api_error', 'quota_uncertain': False})
                pending.remove(orig_s)
                _notify()

    if quota_exhausted:
        quota_status = 'exhausted'
    elif quota_uncertain:
        quota_status = 'uncertain'

    results.sort(key=lambda r: (r.get('market', 'US'), r['symbol']))
    counts = {
        'success': len(live_results),
        'cached': len(cached_results),
        'failed': len(errors),
        'pending': len(pending),
    }
    return {
        'market': mkt_mode,
        'results': results,
        'live_results': live_results,
        'cached_results': cached_results,
        'errors': errors,
        'pending': pending,
        'counts': counts,
        'quota_exhausted': quota_exhausted,
        'quota_uncertain': quota_uncertain,
        'quota_status': quota_status,
        'checked_at_utc': _now_utc().isoformat(),
    }

def report(runout, market=None):
    counts = runout.get('counts') or {
        'success': len(runout.get('live_results', runout.get('results', []))),
        'cached': len(runout.get('cached_results', [])),
        'failed': len(runout.get('errors', [])),
        'pending': len(runout.get('pending', [])),
    }
    mkt_label = market or runout.get('market') or 'US'
    lines=[
        f'# Stock Research Daily Brief ({mkt_label})',
        '',
        f"Checked (UTC): {runout['checked_at_utc']}",
        f"Summary: {counts['success']} live fetched, {counts['cached']} cached, {counts['failed']} failed, {counts['pending']} pending.",
        '',
        'Educational technical screening only — not investment advice, real-time quotes, or verified buy/sell recommendations.',
        '',
    ]
    for r in runout['results']:
        origin_label = r.get('freshness') or format_freshness_label(
            r.get('asof'),
            fetched_at_utc=r.get('fetched_at_utc'),
            data_origin=r.get('data_origin', 'live'),
            market=r.get('market', 'US'),
        )
        curr = r.get('currency', 'USD')
        lines += [
            f"## [{r.get('market', 'US')}] {r['symbol']} ({curr}) — {r['stance']}",
            f"As of (market close): {r['asof']} | Exchange: {r.get('exchange', 'N/A')} | Status: {origin_label}",
            r['rationale'],
            f"Data method: {r['source']}",
            '',
        ]
    if runout['errors']:
        lines += ['## Unavailable / not checked']+[f"- {e['symbol']} ({e.get('error_type', 'error')}): {e['error']}" for e in runout['errors']]
    if runout.get('quota_uncertain'):
        lines += ['', '> Note: Rate-limit responses were ambiguous after bounded backoff. Remaining daily quota is uncertain (Alpha Vantage does not report exact remaining request counts).']
    if runout.get('pending'):
        lines += ['## Pending (skipped after daily quota exhaustion)'] + [f"- {sym}" for sym in runout['pending']]
    if not runout['results']:
        lines += ['**No successful live data fetches. No signals issued.**']
    return '\n'.join(lines)

if __name__=='__main__':
    init_db();out=run();print(report(out))
