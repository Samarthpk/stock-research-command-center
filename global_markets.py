"""Phase 4: Global Market Registry, Maintainable Calendars (2025–2027+), Currencies, and Multi-Market Utilities.

Supports US (NYSE/NASDAQ, USD) and India (NSE/BSE, INR) with strict separation of
instrument identifiers, maintainable trading calendars (2025, 2026, 2027+ rule-based + overrides
+ special trading sessions), timezones (including US DST), and reporting conventions.
Never fabricates prices, fundamentals, valuations, or citations.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

_GLOBAL_MARKETS_VERSION = '4.1'

MARKET_CHOICES = ['All Markets', 'US', 'India']
MARKET_CHOICE_TO_CODE = {
    'All Markets': 'ALL',
    'US': 'US',
    'India': 'IN',
}

US_WATCHLIST_DEFAULT = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'SPY']
INDIA_WATCHLIST_DEFAULT = ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK']

MARKET_CONFIG = {
    'US': {
        'code': 'US',
        'name': 'United States (NYSE / NASDAQ)',
        'exchanges': ['NYSE', 'NASDAQ'],
        'currency': 'USD',
        'currency_symbol': '$',
        'timezone': 'America/New_York',
        'open_hm': (9, 30),
        'close_hm': (16, 0),
        'post_close_hm': (16, 15),
        'accounting_standard': 'US-GAAP (SEC EDGAR 10-K / 10-Q / 8-K)',
        'default_watchlist': US_WATCHLIST_DEFAULT,
    },
    'IN': {
        'code': 'IN',
        'name': 'India (NSE / BSE)',
        'exchanges': ['NSE', 'BSE'],
        'currency': 'INR',
        'currency_symbol': '₹',
        'timezone': 'Asia/Kolkata',
        'open_hm': (9, 15),
        'close_hm': (15, 30),
        'post_close_hm': (15, 45),
        'accounting_standard': 'IND-AS (SEBI LODR Reg 30 / Reg 33 / Reg 34)',
        'default_watchlist': INDIA_WATCHLIST_DEFAULT,
    },
}

# Verified non-interchangeable Indian instrument registry mapping canonical symbol
# to distinct NSE ticker, BSE scrip code, Alpha Vantage BSE ticker, ISIN, and strict company match aliases.
INDIA_INSTRUMENTS = {
    'RELIANCE': {
        'symbol': 'RELIANCE',
        'company': 'Reliance Industries Limited',
        'name': 'Reliance Industries Limited',
        'match_aliases': ['Reliance Industries', 'RIL', 'NSE: RELIANCE', 'NSE:RELIANCE', '500325'],
        'exclude_phrases': [
            'Reliance Power',
            'Reliance Infrastructure',
            'Reliance Industrial Infrastructure',
            'Reliance Communications',
            'Reliance Capital',
            'Reliance Home Finance',
            'Reliance Naval',
            'Reliance Chemotex',
        ],
        'market': 'IN',
        'currency': 'INR',
        'nse_symbol': 'RELIANCE',
        'bse_scrip_code': '500325',
        'bse_code': '500325',
        'bse_ticker': 'RELIANCE.BSE',
        'bse_symbol': 'RELIANCE.BSE',
        'bse_numeric_ticker': '500325.BSE',
        'isin': 'INE002A01018',
        'nse_bse_interchangeable': False,
    },
    'TCS': {
        'symbol': 'TCS',
        'company': 'Tata Consultancy Services Limited',
        'name': 'Tata Consultancy Services Limited',
        'match_aliases': ['Tata Consultancy Services', 'Tata Consultancy', 'TCS'],
        'market': 'IN',
        'currency': 'INR',
        'nse_symbol': 'TCS',
        'bse_scrip_code': '532540',
        'bse_code': '532540',
        'bse_ticker': 'TCS.BSE',
        'bse_symbol': 'TCS.BSE',
        'bse_numeric_ticker': '532540.BSE',
        'isin': 'INE467B01029',
        'nse_bse_interchangeable': False,
    },
    'HDFCBANK': {
        'symbol': 'HDFCBANK',
        'company': 'HDFC Bank Limited',
        'name': 'HDFC Bank Limited',
        'match_aliases': ['HDFC Bank', 'HDFCBANK'],
        'market': 'IN',
        'currency': 'INR',
        'nse_symbol': 'HDFCBANK',
        'bse_scrip_code': '500180',
        'bse_code': '500180',
        'bse_ticker': 'HDFCBANK.BSE',
        'bse_symbol': 'HDFCBANK.BSE',
        'bse_numeric_ticker': '500180.BSE',
        'isin': 'INE040A01034',
        'nse_bse_interchangeable': False,
    },
    'INFY': {
        'symbol': 'INFY',
        'company': 'Infosys Limited',
        'name': 'Infosys Limited',
        'match_aliases': ['Infosys', 'INFY'],
        'market': 'IN',
        'currency': 'INR',
        'nse_symbol': 'INFY',
        'bse_scrip_code': '500209',
        'bse_code': '500209',
        'bse_ticker': 'INFY.BSE',
        'bse_symbol': 'INFY.BSE',
        'bse_numeric_ticker': '500209.BSE',
        'isin': 'INE009A01021',
        'nse_bse_interchangeable': False,
    },
    'ICICIBANK': {
        'symbol': 'ICICIBANK',
        'company': 'ICICI Bank Limited',
        'name': 'ICICI Bank Limited',
        'match_aliases': ['ICICI Bank', 'ICICIBANK'],
        'market': 'IN',
        'currency': 'INR',
        'nse_symbol': 'ICICIBANK',
        'bse_scrip_code': '532174',
        'bse_code': '532174',
        'bse_ticker': 'ICICIBANK.BSE',
        'bse_symbol': 'ICICIBANK.BSE',
        'bse_numeric_ticker': '532174.BSE',
        'isin': 'INE090A01021',
        'nse_bse_interchangeable': False,
    },
}

US_INSTRUMENTS = {
    'AAPL': {'symbol': 'AAPL', 'company': 'Apple Inc.', 'match_aliases': ['Apple', 'AAPL'], 'market': 'US', 'exchange': 'NASDAQ', 'currency': 'USD'},
    'MSFT': {'symbol': 'MSFT', 'company': 'Microsoft Corporation', 'match_aliases': ['Microsoft', 'MSFT'], 'market': 'US', 'exchange': 'NASDAQ', 'currency': 'USD'},
    'NVDA': {'symbol': 'NVDA', 'company': 'NVIDIA Corporation', 'match_aliases': ['NVIDIA', 'NVDA'], 'market': 'US', 'exchange': 'NASDAQ', 'currency': 'USD'},
    'AMZN': {'symbol': 'AMZN', 'company': 'Amazon.com, Inc.', 'match_aliases': ['Amazon', 'AMZN'], 'market': 'US', 'exchange': 'NASDAQ', 'currency': 'USD'},
    'GOOGL': {'symbol': 'GOOGL', 'company': 'Alphabet Inc.', 'match_aliases': ['Alphabet', 'Google', 'GOOGL'], 'market': 'US', 'exchange': 'NASDAQ', 'currency': 'USD'},
    'SPY': {'symbol': 'SPY', 'company': 'SPDR S&P 500 ETF Trust', 'match_aliases': ['SPDR S&P 500', 'SPY'], 'market': 'US', 'exchange': 'NYSE Arca', 'currency': 'USD', 'is_etf': True},
}

INDIA_BENCHMARKS = {
    'NIFTY 50': {
        'name': 'NIFTY 50',
        'exchange': 'NSE',
        'currency': 'INR',
        'alpha_vantage_candidates': ['NIFTY50.BSE', 'NIFTYBEES.BSE'],
        'notes': (
            'NIFTY 50 is an NSE cash index. Alpha Vantage free TIME_SERIES_DAILY covers BSE-listed '
            'securities (.BSE) rather than raw NSE cash index levels; benchmark availability must be '
            'verified explicitly via live provider response before enabling.'
        ),
    },
    'SENSEX': {
        'name': 'BSE SENSEX',
        'exchange': 'BSE',
        'currency': 'INR',
        'alpha_vantage_candidates': ['SENSEX.BSE', 'BSESN.BSE'],
        'notes': (
            'SENSEX is the S&P BSE 30 index. Free Alpha Vantage TIME_SERIES_DAILY may not expose raw '
            'SENSEX index points without a licensed index feed; availability is probed and reported transparently.'
        ),
    },
}


# ---------------------------------------------------------------------------
# Maintainable Calendar Engine (2025, 2026, 2027, and algorithmic 2027+ rules)
# ---------------------------------------------------------------------------

def _easter_sunday(year: int) -> dt.date:
    """Compute Gregorian Easter Sunday for any year (Anonymous Gregorian / Meeus algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> dt.date:
    """Return the n-th (1-based) occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> dt.date:
    """Return the last occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    if month == 12:
        next_first = dt.date(year + 1, 1, 1)
    else:
        next_first = dt.date(year, month + 1, 1)
    last_day = next_first - dt.timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - dt.timedelta(days=offset)


def _us_observed_date(d: dt.date) -> tuple[dt.date, bool]:
    """NYSE/NASDAQ weekend observance rule: Saturday -> preceding Friday; Sunday -> following Monday."""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1), True
    if d.weekday() == 6:
        return d + dt.timedelta(days=1), True
    return d, False


def generate_us_holidays_for_year(year: int) -> dict[str, str]:
    """Generate NYSE / NASDAQ exchange holidays for any year (2025, 2026, 2027, and 2027+)."""
    holidays: dict[str, str] = {}

    # 1. New Year's Day (Jan 1; if Sunday -> Jan 2; note: NYSE does not close Dec 31 if Jan 1 is Saturday unless rules specify,
    #    wait: NYSE Rule 7.2 closes Friday only when Jan 1 is not crossing accounting year-end, so Jan 1 Saturday is not observed Dec 31)
    nyd = dt.date(year, 1, 1)
    if nyd.weekday() < 5:
        holidays[nyd.isoformat()] = "New Year's Day"
    elif nyd.weekday() == 6:
        holidays[(nyd + dt.timedelta(days=1)).isoformat()] = "New Year's Day (Observed)"

    # 2. Martin Luther King, Jr. Day (3rd Monday in January)
    mlk = _nth_weekday_of_month(year, 1, 0, 3)
    holidays[mlk.isoformat()] = 'Martin Luther King, Jr. Day'

    # 3. Washington's Birthday / Presidents' Day (3rd Monday in February)
    pres = _nth_weekday_of_month(year, 2, 0, 3)
    holidays[pres.isoformat()] = "Washington's Birthday"

    # 4. Good Friday (2 days before Easter Sunday)
    good_friday = _easter_sunday(year) - dt.timedelta(days=2)
    holidays[good_friday.isoformat()] = 'Good Friday'

    # 5. Memorial Day (last Monday in May)
    mem = _last_weekday_of_month(year, 5, 0)
    holidays[mem.isoformat()] = 'Memorial Day'

    # 6. Juneteenth National Independence Day (June 19)
    june_obs, june_shifted = _us_observed_date(dt.date(year, 6, 19))
    holidays[june_obs.isoformat()] = (
        'Juneteenth National Independence Day (Observed)' if june_shifted else 'Juneteenth National Independence Day'
    )

    # 7. Independence Day (July 4)
    july_obs, july_shifted = _us_observed_date(dt.date(year, 7, 4))
    holidays[july_obs.isoformat()] = 'Independence Day (Observed)' if july_shifted else 'Independence Day'

    # 8. Labor Day (1st Monday in September)
    labor = _nth_weekday_of_month(year, 9, 0, 1)
    holidays[labor.isoformat()] = 'Labor Day'

    # 9. Thanksgiving Day (4th Thursday in November)
    thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)
    holidays[thanksgiving.isoformat()] = 'Thanksgiving Day'

    # 10. Christmas Day (Dec 25)
    xmas_obs, xmas_shifted = _us_observed_date(dt.date(year, 12, 25))
    holidays[xmas_obs.isoformat()] = 'Christmas Day (Observed)' if xmas_shifted else 'Christmas Day'

    return holidays


# Movable lunar/festival holidays for NSE/BSE (published + projected schedules for 2025, 2026, 2027, 2028)
# Fixed-date national holidays (Jan 26, May 1, Aug 15, Oct 2, Dec 25) and Good Friday are computed automatically for any year!
_INDIA_MOVABLE_HOLIDAYS_BY_YEAR: dict[int, dict[str, str]] = {
    2025: {
        '2025-02-26': 'Mahashivratri',
        '2025-03-14': 'Holi',
        '2025-03-31': 'Id-Ul-Fitr (Ramzan Id)',
        '2025-04-10': 'Shri Mahavir Jayanti',
        '2025-04-14': 'Dr. Baba Saheb Ambedkar Jayanti',
        '2025-08-27': 'Ganesh Chaturthi',
        '2025-10-02': 'Mahatma Gandhi Jayanti / Dussehra',
        '2025-10-21': 'Diwali Laxmi Pujan',
        '2025-10-22': 'Diwali Balipratipada',
        '2025-11-05': 'Prakash Gurpurb Sri Guru Nanak Dev',
    },
    2026: {
        '2026-02-15': 'Mahashivratri',
        '2026-03-03': 'Holi',
        '2026-03-20': 'Id-Ul-Fitr (Ramzan Id)',
        '2026-03-26': 'Shri Ram Navami',
        '2026-03-31': 'Shri Mahavir Jayanti',
        '2026-04-14': 'Dr. Baba Saheb Ambedkar Jayanti',
        '2026-09-14': 'Ganesh Chaturthi',
        '2026-10-20': 'Dussehra',
        '2026-11-08': 'Diwali Laxmi Pujan',
        '2026-11-09': 'Diwali Balipratipada',
        '2026-11-24': 'Guru Nanak Jayanti',
    },
    2027: {
        '2027-03-06': 'Mahashivratri',
        '2027-03-10': 'Id-Ul-Fitr (Ramzan Id)',
        '2027-03-22': 'Holi',
        '2027-04-14': 'Dr. Baba Saheb Ambedkar Jayanti',
        '2027-04-15': 'Shri Ram Navami',
        '2027-04-19': 'Shri Mahavir Jayanti',
        '2027-05-17': 'Bakri Id (Eid al-Adha)',
        '2027-09-04': 'Ganesh Chaturthi',
        '2027-10-11': 'Dussehra',
        '2027-10-29': 'Diwali Laxmi Pujan',
        '2027-11-15': 'Guru Nanak Jayanti',
    },
    2028: {
        '2028-02-23': 'Mahashivratri',
        '2028-02-28': 'Id-Ul-Fitr (Ramzan Id)',
        '2028-03-11': 'Holi',
        '2028-04-03': 'Shri Ram Navami',
        '2028-04-07': 'Shri Mahavir Jayanti',
        '2028-04-14': 'Dr. Baba Saheb Ambedkar Jayanti / Good Friday',
        '2028-08-23': 'Ganesh Chaturthi',
        '2028-09-29': 'Dussehra',
        '2028-10-17': 'Diwali Laxmi Pujan',
        '2028-11-02': 'Guru Nanak Jayanti',
    },
}


def generate_india_holidays_for_year(year: int) -> dict[str, str]:
    """Generate NSE / BSE exchange holidays for any year (2025, 2026, 2027, and 2027+)."""
    holidays: dict[str, str] = {
        f'{year}-01-26': 'Republic Day',
        f'{year}-04-14': 'Dr. Baba Saheb Ambedkar Jayanti',
        f'{year}-05-01': 'Maharashtra Day',
        f'{year}-08-15': 'Independence Day',
        f'{year}-10-02': 'Mahatma Gandhi Jayanti',
        f'{year}-12-25': 'Christmas',
    }
    good_friday = _easter_sunday(year) - dt.timedelta(days=2)
    holidays[good_friday.isoformat()] = 'Good Friday'
    if year in _INDIA_MOVABLE_HOLIDAYS_BY_YEAR:
        holidays.update(_INDIA_MOVABLE_HOLIDAYS_BY_YEAR[year])
    return dict(sorted(holidays.items()))


# Special trading sessions (e.g. Diwali Muhurat Trading in India; NYSE early-close sessions in US)
# Format: date_iso -> {'name': ..., 'type': 'SPECIAL_OPEN' | 'EARLY_CLOSE', 'open_hm': (H, M), 'close_hm': (H, M), 'post_close_hm': (H, M)}
def generate_special_sessions_for_year(market: str, year: int) -> dict[str, dict]:
    mkt = normalize_market(market)
    sessions: dict[str, dict] = {}
    if mkt == 'US':
        # Day after Thanksgiving (Black Friday) early close at 13:00 ET
        thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)
        black_friday = thanksgiving + dt.timedelta(days=1)
        sessions[black_friday.isoformat()] = {
            'name': 'Day After Thanksgiving (Early Close 13:00 ET)',
            'type': 'EARLY_CLOSE',
            'open_hm': (9, 30),
            'close_hm': (13, 0),
            'post_close_hm': (13, 15),
        }
        # Christmas Eve (Dec 24) if weekday and not already the observed Christmas holiday
        dec24 = dt.date(year, 12, 24)
        us_hols = generate_us_holidays_for_year(year)
        if dec24.weekday() < 5 and dec24.isoformat() not in us_hols:
            sessions[dec24.isoformat()] = {
                'name': 'Christmas Eve (Early Close 13:00 ET)',
                'type': 'EARLY_CLOSE',
                'open_hm': (9, 30),
                'close_hm': (13, 0),
                'post_close_hm': (13, 15),
            }
    elif mkt == 'IN':
        # Diwali Muhurat Trading (1-hour special evening session on Diwali Laxmi Pujan)
        muhurat_dates = {
            2025: '2025-10-21',
            2026: '2026-11-08',
            2027: '2027-10-29',
            2028: '2028-10-17',
        }
        if year in muhurat_dates:
            sessions[muhurat_dates[year]] = {
                'name': 'Diwali Laxmi Pujan — Muhurat Trading Session',
                'type': 'SPECIAL_OPEN',
                'open_hm': (18, 0),
                'close_hm': (19, 0),
                'post_close_hm': (19, 15),
            }
    return sessions


# Custom runtime overrides (can also be loaded from config/market_calendars.json if present)
_CUSTOM_HOLIDAYS: dict[str, dict[str, str]] = {'US': {}, 'IN': {}}
_CUSTOM_SPECIAL_SESSIONS: dict[str, dict[str, dict]] = {'US': {}, 'IN': {}}


def register_market_holiday(market: str, date_iso: str, name: str) -> None:
    """Register or override a market holiday at runtime without code changes."""
    mkt = normalize_market(market)
    dt.date.fromisoformat(date_iso[:10])
    _CUSTOM_HOLIDAYS.setdefault(mkt, {})[date_iso[:10]] = name


def register_special_session(
    market: str,
    date_iso: str,
    name: str,
    open_hm: tuple[int, int],
    close_hm: tuple[int, int],
    session_type: str = 'SPECIAL_OPEN',
) -> None:
    """Register a special trading session (e.g., weekend budget session, Muhurat trading, or early close)."""
    mkt = normalize_market(market)
    dt.date.fromisoformat(date_iso[:10])
    post_m = close_hm[1] + 15
    post_h = close_hm[0] + (post_m // 60)
    post_m = post_m % 60
    _CUSTOM_SPECIAL_SESSIONS.setdefault(mkt, {})[date_iso[:10]] = {
        'name': name,
        'type': session_type,
        'open_hm': tuple(open_hm),
        'close_hm': tuple(close_hm),
        'post_close_hm': (post_h, post_m),
    }


def _load_optional_calendar_config() -> None:
    cfg_path = Path(__file__).resolve().parent / 'config' / 'market_calendars.json'
    if not cfg_path.exists():
        return
    try:
        raw = json.loads(cfg_path.read_text(encoding='utf-8'))
        for mkt in ('US', 'IN'):
            for d_iso, h_name in (raw.get(mkt, {}).get('holidays') or {}).items():
                register_market_holiday(mkt, d_iso, h_name)
            for d_iso, sess in (raw.get(mkt, {}).get('special_sessions') or {}).items():
                register_special_session(
                    mkt,
                    d_iso,
                    sess.get('name', 'Special Session'),
                    tuple(sess.get('open_hm', (9, 15))),
                    tuple(sess.get('close_hm', (15, 30))),
                    session_type=sess.get('type', 'SPECIAL_OPEN'),
                )
    except Exception:
        pass


_load_optional_calendar_config()

# Pre-populated multi-year dictionaries for backwards compatibility (2025–2028)
US_MARKET_HOLIDAYS: dict[str, str] = {}
INDIA_MARKET_HOLIDAYS: dict[str, str] = {}
for _y in (2025, 2026, 2027, 2028):
    US_MARKET_HOLIDAYS.update(generate_us_holidays_for_year(_y))
    INDIA_MARKET_HOLIDAYS.update(generate_india_holidays_for_year(_y))


def normalize_market(market: str) -> str:
    m = (market or 'US').strip().upper()
    if m in ('IN', 'INDIA', 'NSE', 'BSE'):
        return 'IN'
    if m in ('US', 'USA', 'NYSE', 'NASDAQ'):
        return 'US'
    if m in ('ALL', 'ALL MARKETS', 'GLOBAL'):
        return 'ALL'
    raise ValueError(f'Unsupported market: {market}')


def currency_for_market(market: str) -> str:
    m = normalize_market(market)
    return 'INR' if m == 'IN' else 'USD'


def format_price(value: float | int | None, currency: str = 'USD') -> str:
    if value is None:
        return 'N/A'
    curr = (currency or 'USD').upper()
    sym = '₹' if curr == 'INR' else '$'
    return f'{curr} {sym}{float(value):,.2f}'


def resolve_instrument(raw_symbol: str, market: str = 'US') -> dict:
    """Resolve a user or watchlist ticker into explicit market/exchange identifiers.

    Strictly preserves the distinction between NSE symbols and BSE scrip codes/tickers.
    Rejects cross-exchange conflation (e.g. passing an NSE suffix as a BSE code).
    """
    mkt = normalize_market(market)
    s = (raw_symbol or '').strip().upper()
    if not s:
        raise ValueError('Empty ticker symbol')

    if mkt == 'US':
        if s.endswith(('.BSE', '.BO', '.NS', '.NSE')):
            raise ValueError(f'Indian exchange suffix not valid in US market watchlist: {s}')
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', s):
            raise ValueError(f'Invalid US ticker: {s}')
        base = US_INSTRUMENTS.get(s, {})
        return {
            'symbol': s,
            'company': base.get('company', s),
            'name': base.get('company', s),
            'match_aliases': base.get('match_aliases', [s]),
            'market': 'US',
            'exchange': base.get('exchange', 'NYSE/NASDAQ'),
            'currency': 'USD',
            'provider_symbol': s,
            'nse_symbol': None,
            'bse_scrip_code': None,
            'bse_code': None,
            'bse_ticker': None,
            'bse_symbol': None,
            'isin': None,
            'nse_bse_interchangeable': False,
        }

    # India market resolution: do not treat NSE and BSE symbols as interchangeable
    explicit_exchange = None
    clean = s
    if s.endswith('.NSE') or s.endswith('.NS'):
        explicit_exchange = 'NSE'
        clean = s.rsplit('.', 1)[0]
    elif s.endswith('.BSE') or s.endswith('.BO'):
        explicit_exchange = 'BSE'
        clean = s.rsplit('.', 1)[0]

    if not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,11}', clean):
        raise ValueError(f'Invalid India ticker: {s}')

    if clean in INDIA_INSTRUMENTS:
        info = dict(INDIA_INSTRUMENTS[clean])
    else:
        by_scrip = next((dict(v) for v in INDIA_INSTRUMENTS.values() if v['bse_scrip_code'] == clean), None)
        if by_scrip:
            if explicit_exchange == 'NSE':
                raise ValueError(f'BSE scrip code {clean} is not a valid NSE symbol (NSE and BSE identifiers are not interchangeable)')
            info = by_scrip
        else:
            if explicit_exchange == 'NSE' and clean.isdigit():
                raise ValueError(f'Numeric BSE scrip code {clean} cannot be used with .NSE suffix')
            info = {
                'symbol': clean,
                'company': clean,
                'name': clean,
                'match_aliases': [clean],
                'market': 'IN',
                'currency': 'INR',
                'nse_symbol': clean if explicit_exchange != 'BSE' and not clean.isdigit() else None,
                'bse_scrip_code': clean if clean.isdigit() else None,
                'bse_code': clean if clean.isdigit() else None,
                'bse_ticker': f'{clean}.BSE',
                'bse_symbol': f'{clean}.BSE',
                'bse_numeric_ticker': f'{clean}.BSE' if clean.isdigit() else None,
                'isin': None,
                'nse_bse_interchangeable': False,
            }

    info['requested_identifier'] = s
    info['requested_exchange'] = explicit_exchange or 'NSE/BSE'
    info['provider_symbol'] = info['bse_ticker']
    info['exchange'] = 'BSE (Alpha Vantage .BSE) / NSE (' + (info.get('nse_symbol') or 'N/A') + ')'
    return info


def get_holidays(market: str, year: int | None = None) -> dict[str, str]:
    """Return holidays for the given market (dynamically generated for any year, including 2027+)."""
    mkt = normalize_market(market)
    years = [year] if year is not None else [2025, 2026, 2027, 2028]
    out: dict[str, str] = {}
    for y in years:
        if mkt == 'IN':
            out.update(generate_india_holidays_for_year(y))
        else:
            out.update(generate_us_holidays_for_year(y))
    for d_iso, h_name in _CUSTOM_HOLIDAYS.get(mkt, {}).items():
        if year is None or d_iso.startswith(f'{year}-'):
            out[d_iso] = h_name
    return out


def get_special_session(market: str, date_obj: dt.date | str) -> dict | None:
    """Return special trading session metadata (e.g. Muhurat Trading or Early Close) if scheduled on date_obj."""
    mkt = normalize_market(market)
    d = date_obj if isinstance(date_obj, dt.date) else dt.date.fromisoformat(str(date_obj)[:10])
    iso = d.isoformat()
    if iso in _CUSTOM_SPECIAL_SESSIONS.get(mkt, {}):
        return _CUSTOM_SPECIAL_SESSIONS[mkt][iso]
    year_sessions = generate_special_sessions_for_year(mkt, d.year)
    return year_sessions.get(iso)


def is_market_holiday(market: str, date_obj: dt.date | str) -> str | None:
    d = date_obj if isinstance(date_obj, dt.date) else dt.date.fromisoformat(str(date_obj)[:10])
    return get_holidays(market, year=d.year).get(d.isoformat())


def is_trading_day(date_obj: dt.date | str, market: str = 'US') -> bool:
    if isinstance(date_obj, str):
        if date_obj.upper() in ('US', 'IN', 'INDIA', 'USA') and isinstance(market, dt.date):
            date_obj, market = market, date_obj
        else:
            date_obj = dt.date.fromisoformat(date_obj[:10])
    mkt = normalize_market(market)
    special = get_special_session(mkt, date_obj)
    if special and special.get('type') == 'SPECIAL_OPEN':
        return True
    if date_obj.weekday() >= 5:
        return False
    return is_market_holiday(mkt, date_obj) is None


def latest_completed_session_date(market: str = 'US', now_utc: dt.datetime | None = None) -> str:
    """Return the YYYY-MM-DD date of the most recently completed trading session for the given market."""
    mkt = normalize_market(market)
    cfg = MARKET_CONFIG[mkt]
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    local_dt = now_utc.astimezone(ZoneInfo(cfg['timezone']))
    candidate = local_dt.date()

    special = get_special_session(mkt, candidate)
    close_hm = special['close_hm'] if special else cfg['close_hm']
    close_time = dt.time(*close_hm)
    if not is_trading_day(candidate, mkt) or local_dt.time() < close_time:
        candidate -= dt.timedelta(days=1)
        while not is_trading_day(candidate, mkt):
            candidate -= dt.timedelta(days=1)
    return candidate.isoformat()


def get_market_schedule(market: str = 'US', now_utc: dt.datetime | None = None) -> dict:
    """Return market timezone, DST status, trading session state, special session, and holiday details."""
    mkt = normalize_market(market)
    cfg = MARKET_CONFIG[mkt]
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    tz = ZoneInfo(cfg['timezone'])
    local_dt = now_utc.astimezone(tz)
    date_iso = local_dt.date().isoformat()
    holiday_name = is_market_holiday(mkt, local_dt.date())
    special = get_special_session(mkt, local_dt.date())
    is_wknd = local_dt.weekday() >= 5
    is_hol = holiday_name is not None
    trading_today = is_trading_day(local_dt.date(), mkt)

    open_hm = special['open_hm'] if special else cfg['open_hm']
    close_hm = special['close_hm'] if special else cfg['close_hm']
    post_hm = special['post_close_hm'] if special else cfg['post_close_hm']

    open_t = dt.time(*open_hm)
    close_t = dt.time(*close_hm)
    post_t = dt.time(*post_hm)
    now_t = local_dt.time()

    if not trading_today:
        phase = 'CLOSED_HOLIDAY' if is_hol else 'CLOSED_WEEKEND'
    elif now_t < open_t:
        phase = 'PRE_MARKET'
    elif open_t <= now_t < close_t:
        phase = 'SPECIAL_SESSION_OPEN' if (special and special.get('type') == 'SPECIAL_OPEN') else 'MARKET_OPEN'
    elif close_t <= now_t < post_t:
        phase = 'CLOSING_SETTLEMENT'
    else:
        phase = 'POST_CLOSE'

    dst_offset = local_dt.dst()
    is_dst = bool(dst_offset and dst_offset.total_seconds() != 0)
    return {
        'market': mkt,
        'market_name': cfg['name'],
        'currency': cfg['currency'],
        'currency_symbol': cfg['currency_symbol'],
        'timezone': cfg['timezone'],
        'tz_abbrev': local_dt.tzname() or cfg['timezone'],
        'is_dst': is_dst,
        'local_time': local_dt.strftime('%Y-%m-%d %H:%M %Z'),
        'local_time_iso': local_dt.isoformat(),
        'local_date': date_iso,
        'is_weekend': is_wknd,
        'is_holiday': is_hol,
        'holiday_name': holiday_name,
        'special_session': special,
        'is_trading_day': trading_today,
        'is_open_now': phase in ('MARKET_OPEN', 'SPECIAL_SESSION_OPEN'),
        'market_phase': phase,
        'ready_for_post_close_run': bool(trading_today and phase == 'POST_CLOSE'),
        'latest_completed_session': latest_completed_session_date(mkt, now_utc),
    }


def get_market_schedule_status(market: str = 'US', now_utc: dt.datetime | None = None) -> dict:
    return get_market_schedule(market=market, now_utc=now_utc)
