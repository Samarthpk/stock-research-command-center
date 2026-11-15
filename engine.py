"""Stock research engine. Research signals only; not trading advice."""
import contextlib, os, json, ssl, sqlite3, time, threading, urllib.request, urllib.parse, urllib.error, datetime, statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
DEFAULT = ['AAPL','MSFT','NVDA','GOOGL','AMZN','SPY']
MIN_REQUEST_INTERVAL = 1.5
DEFAULT_CACHE_TTL_SECONDS = 12 * 3600  # 12 hours
_ENGINE_VERSION = '2'

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

    # Only classify as immediate DailyQuotaExhaustedError when there is specific evidence
    # of daily exhaustion and no per-second/burst throttling indicator.
    if has_explicit_daily and not has_burst:
        return DailyQuotaExhaustedError(text)

    # If the message mentions both per-second pacing and '25 requests per day', or mentions
    # generic '25 requests per day' without explicit exhaustion proof, treat as ambiguous/retryable
    # with conservative bounded backoff and mark quota_uncertain=True.
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
            'fetched_at_utc TEXT, data_origin TEXT, '
            'PRIMARY KEY(symbol,asof))'
        )
        cols = {r[1] for r in db.execute('PRAGMA table_info(results)')}
        if 'fetched_at_utc' not in cols:
            db.execute('ALTER TABLE results ADD COLUMN fetched_at_utc TEXT')
        if 'data_origin' not in cols:
            db.execute('ALTER TABLE results ADD COLUMN data_origin TEXT')

        db.execute(
            'CREATE TABLE IF NOT EXISTS daily_cache ('
            'symbol TEXT PRIMARY KEY, rows_json TEXT, source TEXT, '
            'fetched_at_utc TEXT, latest_asof TEXT)'
        )
        db.execute('CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY)')
        if not db.execute('SELECT 1 FROM watchlist LIMIT 1').fetchone():
            db.executemany('INSERT INTO watchlist VALUES (?)', [(s,) for s in DEFAULT])

def watchlist():
    init_db()
    with _connect_db() as db:
        return [r[0] for r in db.execute('SELECT symbol FROM watchlist ORDER BY symbol')]

def set_watchlist(symbols):
    vals = list(dict.fromkeys(x.strip().upper() for x in symbols if x.strip()))
    if any(not s.replace('.','').replace('-','').isalnum() or len(s)>12 for s in vals):
        raise ValueError('Invalid ticker')
    if len(vals)>20: raise ValueError('Maximum 20 tickers')
    init_db()
    with _connect_db() as db:
        db.execute('DELETE FROM watchlist')
        db.executemany('INSERT INTO watchlist VALUES (?)',[(s,) for s in vals])

def _ssl_context():
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()['x509_ca'] == 0 and Path('/etc/ssl/cert.pem').exists():
        ctx.load_verify_locations(cafile='/etc/ssl/cert.pem')
    return ctx

def request_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'StockResearchCommandCenter/0.1 (personal research)'})
    with urllib.request.urlopen(req,timeout=25,context=_ssl_context()) as response:
        return json.load(response)

def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

def is_data_stale(asof_str, fetched_at_utc=None, now_utc=None, ttl_seconds=DEFAULT_CACHE_TTL_SECONDS):
    now_utc = now_utc or _now_utc()
    stale_by_asof = False
    if asof_str:
        try:
            asof_date = datetime.date.fromisoformat(str(asof_str)[:10])
            days_old = (now_utc.date() - asof_date).days
            weekday = now_utc.weekday()
            # Allow weekend gap (e.g. Friday close checked on Sunday/Monday is 2-3 days old)
            max_fresh_days = 4 if weekday == 0 else (3 if weekday == 6 else 2)
            if days_old > max_fresh_days:
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

def format_freshness_label(asof_str, fetched_at_utc=None, data_origin='cached', now_utc=None):
    stale = is_data_stale(asof_str, fetched_at_utc=fetched_at_utc, now_utc=now_utc)
    ts_short = fetched_at_utc[:19].replace('T', ' ') + ' UTC' if fetched_at_utc else 'unknown time'
    if stale:
        return f'⚠️ Stale (as of {asof_str}, cached {ts_short})'
    if data_origin == 'live':
        return f'Live fetch ({ts_short})'
    return f'Cached ({ts_short})'

def get_cached_daily(symbol, max_age_seconds=DEFAULT_CACHE_TTL_SECONDS, now_utc=None):
    init_db()
    sym = symbol.strip().upper()
    now_utc = now_utc or _now_utc()
    with _connect_db() as db:
        row = db.execute(
            'SELECT rows_json, source, fetched_at_utc, latest_asof FROM daily_cache WHERE symbol = ?',
            (sym,)
        ).fetchone()
    if not row:
        return None
    rows_json, source, fetched_at_utc, latest_asof = row
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
    }

def save_cached_daily(symbol, rows, source, fetched_at_utc=None):
    init_db()
    sym = symbol.strip().upper()
    fetched_at_utc = fetched_at_utc or _now_utc().isoformat()
    latest_asof = rows[-1][0] if rows else ''
    with _connect_db() as db:
        db.execute(
            'INSERT OR REPLACE INTO daily_cache (symbol, rows_json, source, fetched_at_utc, latest_asof) '
            'VALUES (?, ?, ?, ?, ?)',
            (sym, json.dumps(rows), source, fetched_at_utc, latest_asof)
        )
    return fetched_at_utc

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
):
    sym = symbol.strip().upper()
    sched = scheduler if scheduler is not None else SCHEDULER
    sym_lock = _get_symbol_lock(sym)

    with sym_lock:
        if use_cache and not force_refresh:
            cached = get_cached_daily(sym, max_age_seconds=cache_ttl_seconds)
            if cached and cached['rows']:
                if return_metadata:
                    return cached['rows'], cached['source'], {
                        'data_origin': 'cached',
                        'fetched_at_utc': cached['fetched_at_utc'],
                    }
                return cached['rows'], cached['source']

        key = key or get_api_key()
        if not key:
            raise RuntimeError('Missing ALPHAVANTAGE_API_KEY')
        url = API + '?' + urllib.parse.urlencode({
            'function': 'TIME_SERIES_DAILY',
            'symbol': sym,
            'outputsize': 'compact',
            'apikey': key,
        })

        last_err = None
        for attempt in range(max_retries + 1):
            sched.wait()
            try:
                obj = request_json(url)
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < max_retries:
                    sleeper(base_backoff * (2 ** attempt))
                    continue
                raise RuntimeError(f'HTTP {e.code} fetching {sym}: {e.reason}') from e

            if 'Note' in obj or 'Information' in obj:
                msg = obj.get('Note') or obj.get('Information')
                rate_err = classify_rate_limit(msg)
                if isinstance(rate_err, DailyQuotaExhaustedError):
                    # Stop retries immediately when daily quota is exhausted
                    raise rate_err
                last_err = rate_err
                if attempt < max_retries:
                    sleeper(base_backoff * (2 ** attempt))
                    continue
                raise rate_err

            if 'Error Message' in obj:
                raise RuntimeError(obj['Error Message'])

            data = obj.get('Time Series (Daily)')
            if not data:
                raise RuntimeError(f'No valid daily data for {sym}: {str(obj)[:150]}')

            rows = sorted([(d, float(v['4. close'])) for d, v in data.items()], key=lambda x: x[0])
            source = 'https://www.alphavantage.co/documentation/#daily'
            fetched_at_utc = save_cached_daily(sym, rows, source)
            if return_metadata:
                return rows, source, {
                    'data_origin': 'live',
                    'fetched_at_utc': fetched_at_utc,
                }
            return rows, source

        raise last_err or RuntimeError(f'Failed to fetch {sym}')

def analyze(symbol, rows, source, fetched_at_utc=None, data_origin='live'):
    if len(rows)<51: raise ValueError('Need at least 51 trading days for MA50 and 20-day momentum')
    prices=[p for _,p in rows]
    last=prices[-1]; ma20=statistics.mean(prices[-20:]);ma50=statistics.mean(prices[-50:])
    momentum=last/prices[-21]-1
    returns=[prices[i]/prices[i-1]-1 for i in range(len(prices)-19,len(prices))]
    vol=statistics.stdev(returns)*(252**0.5) if len(returns)>1 else 0
    if last>ma20>ma50 and momentum>0.02: stance='Positive trend'
    elif last<ma20<ma50 and momentum< -0.02: stance='Negative trend'
    else: stance='Mixed / watch'
    asof = rows[-1][0]
    fetched_at_utc = fetched_at_utc or _now_utc().isoformat()
    stale = is_data_stale(asof, fetched_at_utc=fetched_at_utc)
    freshness = format_freshness_label(asof, fetched_at_utc=fetched_at_utc, data_origin=data_origin)
    rationale=f'Close ${last:.2f}; MA20 ${ma20:.2f}; MA50 ${ma50:.2f}; 20-session return {momentum:+.1%}; annualized recent volatility {vol:.1%}. Technical trend only; no valuation or news analysis.'
    return dict(
        symbol=symbol,
        asof=asof,
        close=round(last,2),
        change20=round(momentum*100,2),
        ma20=round(ma20,2),
        ma50=round(ma50,2),
        volatility=round(vol*100,2),
        stance=stance,
        rationale=rationale,
        source=source,
        fetched_at_utc=fetched_at_utc,
        data_origin=data_origin,
        stale=stale,
        freshness=freshness,
    )

def store(row):
    init_db()
    keys=['symbol','asof','close','change20','ma20','ma50','volatility','stance','rationale','source','fetched_at_utc','data_origin']
    vals = [row.get(k) for k in keys]
    if not vals[10]:
        vals[10] = _now_utc().isoformat()
    if not vals[11]:
        vals[11] = 'live'
    with _connect_db() as db:
        db.execute(
            'INSERT OR REPLACE INTO results '
            '(symbol,asof,close,change20,ma20,ma50,volatility,stance,rationale,source,fetched_at_utc,data_origin) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            vals
        )

def latest():
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
    for r in raw_rows:
        origin = r.get('data_origin') or 'cached'
        fetched_at = r.get('fetched_at_utc')
        r['stale'] = is_data_stale(r.get('asof'), fetched_at_utc=fetched_at, now_utc=now_utc)
        r['freshness'] = format_freshness_label(r.get('asof'), fetched_at_utc=fetched_at, data_origin=origin, now_utc=now_utc)
    return raw_rows

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
):
    raw_symbols = symbols if symbols is not None else watchlist()
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

    # Step 1: Resolve cached symbols first without touching the network
    to_fetch = []
    for s in ordered_symbols:
        if use_cache and not force_refresh:
            cached = get_cached_daily(s, max_age_seconds=cache_ttl_seconds)
            if cached and cached['rows']:
                try:
                    row = analyze(
                        s,
                        cached['rows'],
                        cached['source'],
                        fetched_at_utc=cached['fetched_at_utc'],
                        data_origin='cached',
                    )
                    store(row)
                    results.append(row)
                    cached_results.append(row)
                    pending.remove(s)
                    _notify()
                    continue
                except Exception as e:
                    errors.append({'symbol': s, 'error': str(e), 'error_type': 'analysis_error'})
                    pending.remove(s)
                    _notify()
                    continue
        to_fetch.append(s)

    # Step 2: Fetch remaining symbols using shared thread-safe request scheduler
    if max_workers and max_workers > 1 and len(to_fetch) > 1:
        stop_event = threading.Event()
        state_lock = threading.Lock()

        def _worker(sym):
            nonlocal quota_exhausted, quota_uncertain
            if stop_event.is_set():
                return
            try:
                rows, source, meta = fetch_daily(
                    sym,
                    key=key,
                    use_cache=use_cache,
                    force_refresh=force_refresh,
                    cache_ttl_seconds=cache_ttl_seconds,
                    max_retries=max_retries,
                    base_backoff=base_backoff,
                    scheduler=scheduler,
                    sleeper=sleeper,
                    return_metadata=True,
                )
                row = analyze(
                    sym,
                    rows,
                    source,
                    fetched_at_utc=meta['fetched_at_utc'],
                    data_origin=meta['data_origin'],
                )
                store(row)
                with state_lock:
                    results.append(row)
                    if meta['data_origin'] == 'cached':
                        cached_results.append(row)
                    else:
                        live_results.append(row)
                    if sym in pending:
                        pending.remove(sym)
                    _notify()
            except DailyQuotaExhaustedError as e:
                stop_event.set()
                with state_lock:
                    quota_exhausted = True
                    errors.append({'symbol': sym, 'error': str(e), 'error_type': 'daily_quota', 'quota_uncertain': False})
                    if sym in pending:
                        pending.remove(sym)
                    _notify()
            except AmbiguousRateLimitError as e:
                with state_lock:
                    quota_uncertain = True
                    errors.append({'symbol': sym, 'error': str(e), 'error_type': 'ambiguous_rate_limit', 'quota_uncertain': True})
                    if sym in pending:
                        pending.remove(sym)
                    _notify()
            except PerSecondRateLimitError as e:
                with state_lock:
                    errors.append({'symbol': sym, 'error': str(e), 'error_type': 'rate_limit', 'quota_uncertain': bool(getattr(e, 'quota_uncertain', False))})
                    if sym in pending:
                        pending.remove(sym)
                    _notify()
            except Exception as e:
                with state_lock:
                    errors.append({'symbol': sym, 'error': str(e), 'error_type': 'api_error', 'quota_uncertain': False})
                    if sym in pending:
                        pending.remove(sym)
                    _notify()

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_worker, to_fetch))
    else:
        for idx, s in enumerate(to_fetch):
            try:
                rows, source, meta = fetch_daily(
                    s,
                    key=key,
                    use_cache=use_cache,
                    force_refresh=force_refresh,
                    cache_ttl_seconds=cache_ttl_seconds,
                    max_retries=max_retries,
                    base_backoff=base_backoff,
                    scheduler=scheduler,
                    sleeper=sleeper,
                    return_metadata=True,
                )
                row = analyze(
                    s,
                    rows,
                    source,
                    fetched_at_utc=meta['fetched_at_utc'],
                    data_origin=meta['data_origin'],
                )
                store(row)
                results.append(row)
                if meta['data_origin'] == 'cached':
                    cached_results.append(row)
                else:
                    live_results.append(row)
                pending.remove(s)
                _notify()
            except DailyQuotaExhaustedError as e:
                quota_exhausted = True
                errors.append({'symbol': s, 'error': str(e), 'error_type': 'daily_quota', 'quota_uncertain': False})
                pending.remove(s)
                _notify()
                # Stop retries and further live API requests immediately when daily quota is confirmed exhausted
                break
            except AmbiguousRateLimitError as e:
                quota_uncertain = True
                errors.append({'symbol': s, 'error': str(e), 'error_type': 'ambiguous_rate_limit', 'quota_uncertain': True})
                pending.remove(s)
                _notify()
            except PerSecondRateLimitError as e:
                errors.append({'symbol': s, 'error': str(e), 'error_type': 'rate_limit', 'quota_uncertain': bool(getattr(e, 'quota_uncertain', False))})
                pending.remove(s)
                _notify()
            except Exception as e:
                errors.append({'symbol': s, 'error': str(e), 'error_type': 'api_error', 'quota_uncertain': False})
                pending.remove(s)
                _notify()

    if quota_exhausted:
        quota_status = 'exhausted'
    elif quota_uncertain:
        quota_status = 'uncertain'

    results.sort(key=lambda r: r['symbol'])
    counts = {
        'success': len(live_results),
        'cached': len(cached_results),
        'failed': len(errors),
        'pending': len(pending),
    }
    return {
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

def report(runout):
    counts = runout.get('counts') or {
        'success': len(runout.get('live_results', runout.get('results', []))),
        'cached': len(runout.get('cached_results', [])),
        'failed': len(runout.get('errors', [])),
        'pending': len(runout.get('pending', [])),
    }
    lines=[
        '# Stock Research Daily Brief',
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
        )
        lines += [
            f"## {r['symbol']} — {r['stance']}",
            f"As of (market close): {r['asof']} | Status: {origin_label}",
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
