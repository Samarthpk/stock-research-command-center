"""Autonomous research pipeline (Phase 3 & Phase 4 Global): scheduled, auditable, evidence-first; never trades.

Supports US (NYSE/NASDAQ, USD, SEC EDGAR) and India (NSE/BSE, INR, SEBI LODR / BSE-NSE disclosures)
with market-specific calendars (2025–2027+), session deduplication, five-pillar summaries,
separate COMPLETE / PARTIAL / FAILED outcome tracking, and consolidated global reporting.
All network calls occur in the explicit run() path, not on import. No credential logging.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import ssl
import statistics
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

from global_markets import (
    INDIA_WATCHLIST_DEFAULT,
    US_WATCHLIST_DEFAULT,
    currency_for_market,
    format_price,
    get_market_schedule,
    normalize_market,
    resolve_instrument,
)
from intelligence import (
    GENERIC_PORTAL_PATHS,
    company_facts,
    india_disclosures_for,
    india_financial_results_for,
    llm_evidence_review,
    news_for,
    sec_filings,
    sec_ticker_map,
)

DEFAULT_WATCHLIST = ','.join(US_WATCHLIST_DEFAULT)
DEFAULT_INDIA_WATCHLIST = ','.join(INDIA_WATCHLIST_DEFAULT)
SYMBOL_RE = re.compile(r'^[A-Z0-9][A-Z0-9.\-]{0,14}$')


def _default_urlopen(req, timeout=20):
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()['x509_ca'] == 0 and Path('/etc/ssl/cert.pem').exists():
        ctx.load_verify_locations(cafile='/etc/ssl/cert.pem')
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_watchlist(value: str, max_symbols: int = 12, market: str = 'US'):
    mkt = normalize_market(market)
    symbols = list(dict.fromkeys(s.strip().upper() for s in (value or '').split(',') if s.strip()))
    if not symbols or len(symbols) > max_symbols:
        raise ValueError('Watchlist empty, too long, or contains invalid tickers')
    if mkt == 'US':
        us_re = re.compile(r'^[A-Z][A-Z0-9.\-]{0,9}$')
        if not all(us_re.fullmatch(x) and not x.endswith(('.BSE', '.BO', '.NS', '.NSE')) for x in symbols):
            raise ValueError('Watchlist empty, too long, or contains invalid tickers')
        return symbols
    resolved = []
    for sym in symbols:
        inst = resolve_instrument(sym, market=mkt)
        resolved.append(inst['symbol'])
    return list(dict.fromkeys(resolved))


def safe_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code}'
    return type(exc).__name__


def do_with_retries(fn: Callable, attempts: int = 2, sleep: Callable = time.sleep):
    for attempt in range(attempts):
        try:
            return fn()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 403, 404, 429):
                raise
            if attempt + 1 == attempts:
                raise
            sleep(2 ** attempt)


def fetch_alpha(ticker: str, api_key: str, opener=None, market: str = 'US'):
    """One Alpha Vantage request; validates historical OHLCV and preserves USD (US) and INR (India BSE).

    If Indian BSE OHLCV data is unavailable from Alpha Vantage, explicitly marks status as
    UNSUPPORTED_BY_PROVIDER rather than claiming Indian market-data functionality.
    """
    mkt = normalize_market(market)
    curr = currency_for_market(mkt)
    if not api_key:
        return {
            'status': 'UNSUPPORTED_NOT_CONFIGURED' if mkt == 'IN' else 'NOT_CONFIGURED',
            'market': mkt,
            'currency': curr,
            'ohlcv_verified': False,
        }
    opener = opener or _default_urlopen
    inst = resolve_instrument(ticker, market=mkt)
    candidates = [inst['provider_symbol']]
    if mkt == 'IN' and inst.get('bse_numeric_ticker') and inst['bse_numeric_ticker'] not in candidates:
        candidates.append(inst['bse_numeric_ticker'])

    body = None
    used_provider_symbol = candidates[0]
    for cand in candidates:
        used_provider_symbol = cand
        params = urllib.parse.urlencode({
            'function': 'TIME_SERIES_DAILY',
            'symbol': cand,
            'outputsize': 'compact',
            'apikey': api_key,
        })
        req = urllib.request.Request(
            'https://www.alphavantage.co/query?' + params,
            headers={'User-Agent': 'StockResearchCommandCenter/0.4'},
        )
        try:
            with opener(req, timeout=20) as resp:
                body = json.load(resp)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
            return {
                'status': 'ERROR',
                'reason': safe_error(exc),
                'market': mkt,
                'currency': curr,
                'ohlcv_verified': False,
            }
        if 'Time Series (Daily)' in body:
            break
        message = str(body.get('Information') or body.get('Note') or body.get('Error Message') or 'Missing daily series')
        if 'rate' in message.lower() or 'requests' in message.lower() or 'limit' in message.lower():
            return {
                'status': 'RATE_LIMITED',
                'reason': 'Provider quota/response unavailable',
                'market': mkt,
                'currency': curr,
                'provider_symbol': used_provider_symbol,
                'ohlcv_verified': False,
            }

    if not body or 'Time Series (Daily)' not in body:
        return {
            'status': 'UNSUPPORTED_BY_PROVIDER' if mkt == 'IN' else 'UNAVAILABLE',
            'reason': 'No usable daily OHLCV series returned by provider',
            'market': mkt,
            'currency': curr,
            'provider_symbol': used_provider_symbol,
            'ohlcv_verified': False,
        }

    series = body['Time Series (Daily)']
    try:
        dates = sorted(series)
        closes = [float(series[d]['4. close']) for d in dates]
        if len(closes) < 51:
            return {
                'status': 'INSUFFICIENT_HISTORY',
                'sessions': len(closes),
                'market': mkt,
                'currency': curr,
                'ohlcv_verified': False,
            }
        latest_bar = series[dates[-1]]
        ohlcv_keys = ('1. open', '2. high', '3. low', '4. close', '5. volume')
        has_full_ohlcv = all(k in latest_bar for k in ohlcv_keys)
        latest_ohlcv = {
            'open': round(float(latest_bar['1. open']), 4),
            'high': round(float(latest_bar['2. high']), 4),
            'low': round(float(latest_bar['3. low']), 4),
            'close': round(float(latest_bar['4. close']), 4),
            'volume': int(float(latest_bar['5. volume'])),
        } if has_full_ohlcv else None

        close = closes[-1]
        ma20 = sum(closes[-20:]) / 20
        ma50 = sum(closes[-50:]) / 50
        ret20 = (close / closes[-21] - 1) * 100 if closes[-21] else None
        rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 19, len(closes))]
        vol_pct = round(statistics.stdev(rets) * (252 ** 0.5) * 100, 2) if len(rets) > 1 else 0.0
        direction = 'POSITIVE' if close > ma20 > ma50 else 'NEGATIVE' if close < ma20 < ma50 else 'MIXED'
        return {
            'status': 'OK',
            'market': mkt,
            'currency': curr,
            'provider_symbol': used_provider_symbol,
            'ohlcv_verified': has_full_ohlcv,
            'latest_ohlcv': latest_ohlcv,
            'as_of': dates[-1],
            'close': round(close, 4),
            'ma20': round(ma20, 4),
            'ma50': round(ma50, 4),
            'return20_pct': round(ret20, 3) if ret20 is not None else None,
            'volatility_pct': vol_pct,
            'trend': direction,
            'source_url': 'https://www.alphavantage.co/documentation/',
            'retrieved_at': now_iso(),
            'is_realtime': False,
        }
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return {'status': 'INVALID_DATA', 'market': mkt, 'currency': curr, 'ohlcv_verified': False}


def _is_valid_primary_filing_item(item: dict) -> bool:
    """Verify a filing entry is a specific primary regulatory document, not a generic portal homepage."""
    if not isinstance(item, dict):
        return False
    url = (item.get('url') or '').strip()
    published = (item.get('published') or '').strip()
    if not url or not published:
        return False
    try:
        path = urllib.parse.urlparse(url).path or ''
    except Exception:
        return False
    if path in GENERIC_PORTAL_PATHS:
        return False
    return True


def _has_verified_fundamental_metrics(facts: dict) -> bool:
    """Verify fundamental metrics contain at least one numeric value backed by an official document."""
    if not isinstance(facts, dict):
        return False
    metrics = facts.get('metrics')
    if not isinstance(metrics, dict) or not metrics:
        return False
    for entry in metrics.values():
        if not isinstance(entry, dict):
            continue
        val = entry.get('value_usd') if 'value_usd' in entry else entry.get('value_inr')
        if isinstance(val, (int, float)):
            return True
    return False


def quality_gate(agent_output: dict):
    """Conservative gate: ONLY verified company-specific primary filing documents + verified
    company-specific fundamental metrics count as PRIMARY_EVIDENCE_PRESENT.

    Generic portal URLs, secondary news mentions, or headlines alone NEVER satisfy primary evidence.
    """
    filings = agent_output.get('filings', {})
    facts = agent_output.get('fundamentals', {})
    headlines = agent_output.get('news', [])
    primary = filings.get('filings', []) if isinstance(filings, dict) else []
    secondary = filings.get('secondary_disclosure_mentions', []) if isinstance(filings, dict) else []

    num_sources = sum(_is_valid_primary_filing_item(x) for x in primary)
    num_headlines = sum(bool(x.get('url') and x.get('title')) for x in headlines)
    num_secondary = sum(bool(x.get('url') and x.get('title')) for x in secondary)
    has_metrics = _has_verified_fundamental_metrics(facts)

    if num_sources and has_metrics:
        return 'PRIMARY_EVIDENCE_PRESENT'
    if num_sources or num_headlines or num_secondary:
        return 'PARTIAL_EVIDENCE'
    return 'INSUFFICIENT_EVIDENCE'


def classify_research_outcome(record: dict) -> str:
    """Requirement 5: Classify each symbol's research execution into COMPLETE, PARTIAL, or FAILED."""
    quality = record.get('quality', 'INSUFFICIENT_EVIDENCE')
    p_status = (record.get('price') or {}).get('status')
    if quality == 'PRIMARY_EVIDENCE_PRESENT' and p_status == 'OK':
        return 'COMPLETE'
    if quality in ('PRIMARY_EVIDENCE_PRESENT', 'PARTIAL_EVIDENCE') or p_status == 'OK':
        return 'PARTIAL'
    return 'FAILED'


def _collect_evidence_citations(record: dict) -> list[dict]:
    citations = []
    p = record.get('price', {})
    if p.get('status') == 'OK' and p.get('as_of'):
        citations.append({
            'type': 'market_data',
            'title': f"Alpha Vantage Daily OHLCV Close ({p.get('provider_symbol') or record.get('ticker')})",
            'as_of': p.get('as_of'),
            'url': p.get('source_url', 'https://www.alphavantage.co/documentation/'),
        })
    filings_dict = record.get('filings') if isinstance(record.get('filings'), dict) else {}
    for f in (filings_dict.get('filings') or [])[:4]:
        if _is_valid_primary_filing_item(f):
            citations.append({
                'type': 'regulatory_filing',
                'title': f['title'],
                'as_of': f.get('published', 'unknown'),
                'url': f['url'],
            })
    for m in (filings_dict.get('secondary_disclosure_mentions') or [])[:2]:
        if m.get('url') and m.get('title'):
            citations.append({
                'type': 'secondary_disclosure_mention',
                'title': m['title'],
                'as_of': m.get('published', 'unknown'),
                'url': m['url'],
            })
    for n in (record.get('news') or [])[:3]:
        if n.get('url') and n.get('title'):
            citations.append({
                'type': 'headline_discovery',
                'title': n['title'],
                'as_of': n.get('published', 'unknown'),
                'url': n['url'],
            })
    return citations


def decide_signal(record: dict):
    """Market-specific research priority ranking (INVESTIGATE, WATCH, CAUTION, INSUFFICIENT_EVIDENCE).

    Requirement 6: Never rank a company based only on source URLs, generic news headlines,
    or incomplete financial evidence. Both US and Indian companies MUST satisfy
    `quality == 'PRIMARY_EVIDENCE_PRESENT'` (verified company-specific official filings AND
    verified company-specific fundamental metrics) plus verified historical price data (`status == 'OK'`)
    before receiving INVESTIGATE, WATCH, or CAUTION.
    """
    mkt = normalize_market(record.get('market', 'US'))
    curr = record.get('currency') or currency_for_market(mkt)
    p = record.get('price', {})
    as_of = p.get('as_of') or 'unavailable'
    citations = _collect_evidence_citations(record)

    if record.get('quality') != 'PRIMARY_EVIDENCE_PRESENT':
        reason = (
            'Primary official exchange filings and verified company-specific IND-AS financial metrics not both present; '
            'companies are never ranked on portal URLs or news headlines alone'
            if mkt == 'IN' else
            'Primary filings and comparable filed metrics not both present'
        )
        return {
            'label': 'INSUFFICIENT_EVIDENCE',
            'reason': reason,
            'market': mkt,
            'currency': curr,
            'as_of': as_of,
            'evidence_count': len(citations),
            'evidence_citations': citations,
            'uncertainty': (
                'Incomplete primary financial evidence: verified official filing documents and company-specific '
                'accounting metrics are required before issuing a research ranking.'
            ),
        }

    if p.get('status') != 'OK':
        return {
            'label': 'INSUFFICIENT_EVIDENCE' if mkt == 'IN' else 'RESEARCH_ONLY',
            'reason': 'No usable technical price data',
            'market': mkt,
            'currency': curr,
            'as_of': as_of,
            'evidence_count': len(citations),
            'evidence_citations': citations,
            'uncertainty': 'Primary filings and metrics exist, but historical daily price series was unavailable or unsupported.',
        }

    label = 'INVESTIGATE' if p.get('trend') == 'POSITIVE' else 'WATCH' if p.get('trend') == 'MIXED' else 'CAUTION'
    return {
        'label': label,
        'reason': f'Historical moving-average trend ({curr}) backed by verified primary filings and official financial metrics; not investment advice',
        'market': mkt,
        'currency': curr,
        'as_of': as_of,
        'evidence_count': len(citations),
        'evidence_citations': citations,
        'uncertainty': (
            f'Historical end-of-day technicals ({curr}) and point-in-time reported accounting facts only; '
            'no forward earnings estimates or valuation multiples are fabricated.'
        ),
    }


def build_five_pillar_summary(record: dict) -> dict:
    """Integrate technical, fundamental, valuation, sentiment, and risk analysis with explicit provenance."""
    mkt = normalize_market(record.get('market', 'US'))
    curr = record.get('currency') or currency_for_market(mkt)
    p = record.get('price', {})
    fundamentals = record.get('fundamentals', {}) if isinstance(record.get('fundamentals'), dict) else {}
    filings = record.get('filings', {}) if isinstance(record.get('filings'), dict) else {}
    news = record.get('news', []) if isinstance(record.get('news'), list) else []

    # 1. Technical Pillar
    if p.get('status') == 'OK':
        technical = {
            'status': 'VERIFIED_HISTORICAL_OHLCV' if p.get('ohlcv_verified') else 'VERIFIED_HISTORICAL_CLOSE',
            'as_of': p.get('as_of'),
            'currency': curr,
            'close_formatted': format_price(p.get('close'), curr),
            'ma20_formatted': format_price(p.get('ma20'), curr),
            'ma50_formatted': format_price(p.get('ma50'), curr),
            'return20_pct': p.get('return20_pct'),
            'trend': p.get('trend'),
            'latest_ohlcv': p.get('latest_ohlcv'),
        }
    else:
        technical = {
            'status': p.get('status', 'UNAVAILABLE'),
            'as_of': None,
            'currency': curr,
            'note': 'Historical daily OHLCV series not verified for this run; no prices fabricated.',
        }

    # 2. Fundamental Pillar
    metrics = fundamentals.get('metrics', {})
    filing_items = filings.get('filings', [])
    secondary_items = filings.get('secondary_disclosure_mentions', [])
    if mkt == 'US':
        fundamental = {
            'accounting_standard': 'US-GAAP (SEC EDGAR)',
            'currency': 'USD',
            'sec_filings_count': len(filing_items),
            'xbrl_metrics_count': len(metrics),
            'metrics': metrics,
            'status': 'VERIFIED_XBRL_AND_FILINGS' if (filing_items and metrics) else ('FILINGS_ONLY' if filing_items else 'UNAVAILABLE'),
        }
    else:
        fundamental = {
            'accounting_standard': 'IND-AS (SEBI LODR Reg 30/33/34)',
            'currency': 'INR',
            'verified_official_filings_count': len(filing_items),
            'verified_official_metrics_count': len(metrics),
            'secondary_disclosure_mentions_count': len(secondary_items),
            'metrics': metrics,
            'official_exchange_portals': fundamentals.get('official_exchange_portals', {}),
            'status': (
                'VERIFIED_OFFICIAL_IND_AS_AND_FILINGS'
                if (filing_items and metrics)
                else ('OFFICIAL_FILINGS_ONLY' if filing_items else ('SECONDARY_MENTIONS_ONLY' if secondary_items else 'UNAVAILABLE'))
            ),
            'note': fundamentals.get('warning'),
        }

    # 3. Valuation Pillar (never fabricate P/E, EV/EBITDA, or intrinsic value)
    valuation = {
        'status': 'NOT_COMPUTED_INSUFFICIENT_VERIFIED_INPUTS',
        'currency': curr,
        'note': (
            'Valuation multiples (P/E, P/B, EV/EBITDA) and target prices are never invented; '
            'computing verified multiples requires diluted share count and period-aligned earnings.'
        ),
    }

    # 4. Sentiment / Headline Discovery Pillar
    sentiment = {
        'status': 'HEADLINE_DISCOVERY_ONLY' if news else 'NO_HEADLINES_FETCHED',
        'headlines_count': len(news),
        'latest_published': news[0].get('published') if news else None,
        'note': 'Company-filtered RSS headlines indicate topic activity only; article claims are unverified and never used alone to rank stocks.',
    }

    # 5. Risk & Uncertainty Pillar
    uncertainties = []
    if p.get('status') != 'OK':
        uncertainties.append(f"Market OHLCV price series unavailable/unverified ({p.get('status')}).")
    if mkt == 'IN' and not metrics:
        uncertainties.append('No verified company-specific official IND-AS financial document metrics parsed; stock ranking withheld (INSUFFICIENT_EVIDENCE).')
    elif mkt == 'US' and not metrics:
        uncertainties.append('No US-GAAP XBRL metrics extracted (common for ETFs like SPY or when SEC_USER_AGENT is unset).')
    if not news:
        uncertainties.append('No recent company-specific news headlines retrieved.')

    risk = {
        'annualized_volatility_20d_pct': p.get('volatility_pct') if p.get('status') == 'OK' else None,
        'data_quality_tier': record.get('quality'),
        'research_outcome': record.get('research_outcome'),
        'uncertainties': uncertainties,
    }

    return {
        'technical': technical,
        'fundamental': fundamental,
        'valuation': valuation,
        'sentiment': sentiment,
        'risk': risk,
    }


def analyze_symbol(ticker, *, fetch=None, sec_user_agent='', cik_map=None, price=None, ai_key='', market='US'):
    """Run agents with isolated failures, market-specific filing pipelines, and no fabricated completion."""
    from intelligence import fetch_bytes
    fetch = fetch or fetch_bytes
    mkt = normalize_market(market)
    inst = resolve_instrument(ticker, market=mkt)
    canon = inst['symbol']
    curr = inst['currency']

    default_price = price or {
        'status': 'UNSUPPORTED_NOT_CONFIGURED' if mkt == 'IN' else 'NOT_CONFIGURED',
        'market': mkt,
        'currency': curr,
        'ohlcv_verified': False,
    }
    if 'currency' not in default_price:
        default_price = dict(default_price, currency=curr, market=mkt)

    record = {
        'ticker': canon,
        'company': inst.get('company', canon),
        'market': mkt,
        'exchange': inst.get('exchange', 'NSE/BSE' if mkt == 'IN' else 'NYSE/NASDAQ'),
        'currency': curr,
        'instrument': inst,
        'started_at': now_iso(),
        'agents': {},
        'news': [],
        'filings': {},
        'fundamentals': {},
        'price': default_price,
        'quality': 'INSUFFICIENT_EVIDENCE',
        'research_outcome': 'FAILED',
        'ai': {'status': 'NOT_CONFIGURED'},
    }

    if mkt == 'IN':
        # Never use US SEC filing logic for Indian companies
        steps = [
            ('news', lambda: news_for(canon, fetch=fetch, limit=6, market='IN')),
            ('filings', lambda: india_disclosures_for(canon, fetch=fetch, limit=6)),
            ('fundamentals', lambda: india_financial_results_for(canon, fetch=fetch, disclosures=record.get('filings') or None)),
        ]
        for name, fn in steps:
            try:
                result = do_with_retries(fn)
                record[name] = result
                record['agents'][name] = {'status': 'COMPLETE', 'at': now_iso(), 'market': 'IN'}
            except Exception as exc:
                record['agents'][name] = {'status': 'FAILED', 'reason': safe_error(exc), 'market': 'IN'}
    else:
        steps = [
            ('news', lambda: news_for(canon, fetch=fetch, limit=6, market='US')),
            ('filings', lambda: sec_filings(canon, fetch=fetch, user_agent=sec_user_agent, ticker_map=cik_map, limit=6, market='US')),
            ('fundamentals', lambda: company_facts(canon, fetch=fetch, user_agent=sec_user_agent, ticker_map=cik_map, market='US')),
        ]
        for name, fn in steps:
            if name != 'news' and (not sec_user_agent or not cik_map):
                record['agents'][name] = {'status': 'SKIPPED', 'reason': 'SEC_USER_AGENT missing or CIK map unavailable'}
                continue
            try:
                result = do_with_retries(fn)
                record[name] = result
                record['agents'][name] = {'status': 'COMPLETE', 'at': now_iso()}
            except Exception as exc:
                record['agents'][name] = {'status': 'FAILED', 'reason': safe_error(exc)}

    record['agents']['market_data'] = {
        'status': 'COMPLETE' if record['price'].get('status') == 'OK' else 'SKIPPED',
        'reason': record['price'].get('status'),
        'currency': curr,
    }
    record['quality'] = quality_gate(record)
    record['research_outcome'] = classify_research_outcome(record)
    record['signal'] = decide_signal(record)
    record['pillars'] = build_five_pillar_summary(record)
    record['five_pillar_summary'] = record['pillars']

    if ai_key and record['quality'] == 'PRIMARY_EVIDENCE_PRESENT':
        review_input = {
            'ticker': canon,
            'market': mkt,
            'currency': curr,
            'news': record['news'],
            'sec': record['filings'],
            'fundamentals': record['fundamentals'],
        }
        record['ai'] = llm_evidence_review(review_input, ai_key)
        record['agents']['ai_analyst'] = {
            'status': 'COMPLETE' if record['ai']['status'] == 'UNVERIFIED_LLM_SYNTHESIS' else 'FAILED',
            'at': now_iso(),
        }
    else:
        record['agents']['ai_analyst'] = {'status': 'SKIPPED', 'reason': 'No API key or insufficient primary evidence'}

    record['agents']['validator'] = {
        'status': 'COMPLETE',
        'quality': record['quality'],
        'research_outcome': record['research_outcome'],
    }
    record['finished_at'] = now_iso()
    return record


def summarize_mission_outcomes(results: list[dict]) -> tuple[str, dict[str, int], dict[str, list[str]]]:
    """Requirement 5: Compute separate COMPLETE, PARTIAL, and FAILED counts and symbol lists."""
    by_symbol: dict[str, list[str]] = {'COMPLETE': [], 'PARTIAL': [], 'FAILED': []}
    for r in results:
        outcome = r.get('research_outcome') or classify_research_outcome(r)
        r['research_outcome'] = outcome
        bucket = outcome if outcome in by_symbol else 'FAILED'
        by_symbol[bucket].append(r.get('ticker', 'UNKNOWN'))

    counts = {k: len(v) for k, v in by_symbol.items()}
    if not results or counts['FAILED'] == len(results):
        overall = 'FAILED'
    elif counts['COMPLETE'] == len(results):
        overall = 'COMPLETE'
    else:
        overall = 'PARTIAL'
    return overall, counts, by_symbol


def verify_report_sanitized(content: str) -> bool:
    """Requirement 7: Confirm JSON and Markdown reports contain no credentials or secret tokens."""
    if not content:
        return True
    forbidden_patterns = [
        r'apikey=[A-Za-z0-9_\-]{6,}',
        r'Bearer\s+[A-Za-z0-9_\-\.]{8,}',
        r'ghp_[A-Za-z0-9]{20,}',
        r'gho_[A-Za-z0-9]{20,}',
        r'gsk_[A-Za-z0-9]{20,}',
        r'ALPHAVANTAGE_API_KEY\s*=\s*["\']?[A-Za-z0-9]{8,}',
    ]
    for pat in forbidden_patterns:
        if re.search(pat, content, flags=re.IGNORECASE):
            raise ValueError(f'Report sanitization check failed: matched forbidden credential pattern {pat}')
    return True


def render_mission_markdown(mission: dict) -> str:
    """Render an evidence-backed daily Markdown brief identifying COMPLETE, PARTIAL, and FAILED research separately."""
    mkt = mission.get('market', 'US')
    curr = mission.get('currency', currency_for_market(mkt) if mkt in ('US', 'IN') else 'USD / INR')
    sched = mission.get('market_schedule', {})
    counts = mission.get('outcome_counts') or {'COMPLETE': 0, 'PARTIAL': 0, 'FAILED': 0}
    outcomes_by_sym = mission.get('outcomes_by_symbol') or {'COMPLETE': [], 'PARTIAL': [], 'FAILED': []}

    lines = [
        f"# Autonomous Equity Research Report — Market: {mkt}",
        "",
        f"- **Mission ID:** `{mission.get('mission_id', 'unknown')}`",
        f"- **Overall Mission Status:** `{mission.get('status', 'unknown')}`",
        f"- **Market Session Date:** `{mission.get('market_session_date', sched.get('latest_completed_session', 'unknown'))}`",
        f"- **Native Currency:** `{curr}`",
        f"- **Started (UTC):** `{mission.get('started_at', 'unknown')}`",
        f"- **Completed (UTC):** `{mission.get('finished_at', 'unknown')}`",
        f"- **Policy:** {mission.get('data_policy', 'historical and public sources; no trading')}",
        "",
        "## Research Outcome Breakdown (COMPLETE vs. PARTIAL vs. FAILED)",
        f"- **COMPLETE ({counts.get('COMPLETE', 0)}):** {', '.join(outcomes_by_sym.get('COMPLETE', [])) or 'None'}",
        f"- **PARTIAL ({counts.get('PARTIAL', 0)}):** {', '.join(outcomes_by_sym.get('PARTIAL', [])) or 'None'}",
        f"- **FAILED ({counts.get('FAILED', 0)}):** {', '.join(outcomes_by_sym.get('FAILED', [])) or 'None'}",
        "",
        "> Screening categories (`INVESTIGATE`, `WATCH`, `CAUTION`, `INSUFFICIENT_EVIDENCE`) are research priorities, not buy/sell advice. No company is ever ranked without verified primary filings, company-specific official financial metrics, and historical price data. Automated trading is disabled.",
        "",
    ]
    for rec in mission.get('results', []):
        sig = rec.get('signal', {})
        p = rec.get('price', {})
        rec_curr = rec.get('currency', 'USD')
        outcome = rec.get('research_outcome', 'PARTIAL')
        lines += [
            f"## [{rec.get('market', mkt)}] {rec.get('ticker')} ({rec.get('company', rec.get('ticker'))}) — Outcome: `{outcome}` | Ranking: `{sig.get('label', 'INSUFFICIENT_EVIDENCE')}`",
            f"- **Evidence Tier:** `{rec.get('quality')}` | **As-Of Date:** `{sig.get('as_of', p.get('as_of', 'unavailable'))}` | **Currency:** `{rec_curr}`",
            f"- **Signal Rationale:** {sig.get('reason', '')}",
            f"- **Uncertainty Disclosure:** {sig.get('uncertainty', 'Standard historical data lags apply.')}",
        ]
        if p.get('status') == 'OK':
            lines.append(
                f"- **Technical Summary ({rec_curr}):** Close {format_price(p.get('close'), rec_curr)} | "
                f"MA20 {format_price(p.get('ma20'), rec_curr)} | MA50 {format_price(p.get('ma50'), rec_curr)} | "
                f"20D Return {p.get('return20_pct')}% | 20D Ann. Vol {p.get('volatility_pct', 'N/A')}% | Trend: `{p.get('trend')}`"
            )
        else:
            lines.append(f"- **Technical Summary ({rec_curr}):** Status `{p.get('status')}` (no prices fabricated)")

        citations = sig.get('evidence_citations', [])
        if citations:
            lines.append("- **Verified Citations:**")
            for c in citations[:5]:
                lines.append(f"  - [{c.get('title')}]({c.get('url')}) (as of `{c.get('as_of')}`)")
        lines.append("")
    md_text = "\n".join(lines)
    verify_report_sanitized(md_text)
    return md_text


def update_consolidated_global_report(output_dir: str | Path = 'reports/autonomous') -> dict:
    """Combine separate US and India mission reports into a consolidated global report."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    markets_data = {}
    combined_results = []
    for mkt, fname in (('US', 'us_latest.json'), ('IN', 'india_latest.json')):
        fpath = output / fname
        if fpath.exists():
            try:
                mdata = json.loads(fpath.read_text(encoding='utf-8'))
                markets_data[mkt] = mdata
                combined_results.extend(mdata.get('results', []))
            except Exception:
                pass

    overall_status, outcome_counts, outcomes_by_symbol = summarize_mission_outcomes(combined_results)
    global_mission = {
        'schema_version': 2,
        'mission_id': datetime.now(timezone.utc).strftime('GLOBAL-%Y%m%dT%H%M%SZ'),
        'market': 'ALL',
        'updated_at': now_iso(),
        'started_at': min((m.get('started_at', now_iso()) for m in markets_data.values()), default=now_iso()),
        'finished_at': now_iso(),
        'data_policy': 'historical and public sources; native currencies preserved (USD/INR); no trading',
        'markets_included': list(markets_data.keys()),
        'market_summaries': {
            mkt: {
                'mission_id': m.get('mission_id'),
                'status': m.get('status'),
                'outcome_counts': m.get('outcome_counts'),
                'outcomes_by_symbol': m.get('outcomes_by_symbol'),
                'market_session_date': m.get('market_session_date'),
                'currency': m.get('currency'),
                'symbols': m.get('symbols', []),
                'finished_at': m.get('finished_at'),
            }
            for mkt, m in markets_data.items()
        },
        'symbols': [r.get('ticker') for r in combined_results],
        'outcome_counts': outcome_counts,
        'outcomes_by_symbol': outcomes_by_symbol,
        'results': combined_results,
        'status': overall_status,
    }
    json_str = json.dumps(global_mission, indent=2)
    verify_report_sanitized(json_str)
    (output / 'global_latest.json').write_text(json_str, encoding='utf-8')
    (output / 'global_daily_brief.md').write_text(render_mission_markdown(global_mission), encoding='utf-8')
    return global_mission


def run_mission(
    symbols,
    *,
    output_dir='reports/autonomous',
    sec_user_agent='',
    alpha_key='',
    groq_key='',
    fetch=None,
    price_fetch=None,
    sleep=time.sleep,
    market='US',
    enforce_once_per_session=False,
    now_utc=None,
):
    mkt = normalize_market(market)
    curr = currency_for_market(mkt)
    sched_info = get_market_schedule(mkt, now_utc=now_utc)
    session_date = sched_info['latest_completed_session']
    symbols = parse_watchlist(','.join(symbols), market=mkt)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    market_prefix = 'india' if mkt == 'IN' else 'us'
    market_latest_path = output / f'{market_prefix}_latest.json'
    market_status_path = output / f'{market_prefix}_status.json'
    market_brief_path = output / f'{market_prefix}_daily_brief.md'

    # Requirement 17 / 8: Do not allow more than one scheduled mission per market session
    if enforce_once_per_session and market_latest_path.exists():
        try:
            existing = json.loads(market_latest_path.read_text(encoding='utf-8'))
            if (
                existing.get('market') == mkt
                and existing.get('market_session_date') == session_date
                and existing.get('status') in ('COMPLETE', 'PARTIAL')
            ):
                existing['skipped_duplicate_session'] = True
                return existing
        except Exception:
            pass

    mission = {
        'schema_version': 2,
        'mission_id': datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + f'-{mkt}',
        'market': mkt,
        'currency': curr,
        'market_session_date': session_date,
        'market_schedule': sched_info,
        'started_at': now_iso(),
        'data_policy': 'historical and public sources; native currencies preserved (USD/INR); no trading',
        'symbols': symbols,
        'results': [],
        'status': 'RUNNING',
    }
    status_running = {'status': 'RUNNING', 'market': mkt, 'market_session_date': session_date, 'started_at': mission['started_at']}
    (output / 'status.json').write_text(json.dumps(status_running, indent=2), encoding='utf-8')
    market_status_path.write_text(json.dumps(status_running, indent=2), encoding='utf-8')

    cik_map = None
    sec_available = bool(mkt == 'US' and sec_user_agent and '@' in sec_user_agent)
    if sec_available:
        try:
            from intelligence import fetch_bytes
            cik_map = do_with_retries(lambda: sec_ticker_map(fetch=fetch or fetch_bytes, user_agent=sec_user_agent), sleep=sleep)
        except Exception:
            cik_map = None

    def _default_price_fetcher(sym, key):
        return fetch_alpha(sym, key, market=mkt)

    fetch_price = price_fetch or _default_price_fetcher
    for idx, symbol in enumerate(symbols):
        if idx and alpha_key and not mission.get('price_quota_blocked'):
            sleep(1.7)
        if alpha_key and not mission.get('price_quota_blocked'):
            try:
                price = fetch_price(symbol, alpha_key)
                if price.get('status') == 'RATE_LIMITED':
                    mission['price_quota_blocked'] = True
            except Exception as exc:
                price = {'status': 'ERROR', 'reason': safe_error(exc), 'market': mkt, 'currency': curr, 'ohlcv_verified': False}
        else:
            price = {
                'status': 'QUOTA_BLOCKED' if mission.get('price_quota_blocked') else ('UNSUPPORTED_NOT_CONFIGURED' if mkt == 'IN' else 'NOT_CONFIGURED'),
                'market': mkt,
                'currency': curr,
                'ohlcv_verified': False,
            }
        record = analyze_symbol(
            symbol,
            fetch=fetch,
            sec_user_agent=sec_user_agent,
            cik_map=cik_map,
            price=price,
            ai_key=groq_key,
            market=mkt,
        )
        mission['results'].append(record)
        (output / 'checkpoint.json').write_text(json.dumps(mission, indent=2), encoding='utf-8')

    overall_status, outcome_counts, outcomes_by_symbol = summarize_mission_outcomes(mission['results'])
    mission['finished_at'] = now_iso()
    mission['outcome_counts'] = outcome_counts
    mission['outcomes_by_symbol'] = outcomes_by_symbol
    mission['status'] = overall_status

    json_str = json.dumps(mission, indent=2)
    verify_report_sanitized(json_str)
    market_latest_path.write_text(json_str, encoding='utf-8')
    market_brief_path.write_text(render_mission_markdown(mission), encoding='utf-8')

    done_status = {
        'status': mission['status'],
        'outcome_counts': outcome_counts,
        'outcomes_by_symbol': outcomes_by_symbol,
        'market': mkt,
        'market_session_date': session_date,
        'completed_at': mission['finished_at'],
        'mission_id': mission['mission_id'],
    }
    status_str = json.dumps(done_status, indent=2)
    verify_report_sanitized(status_str)
    market_status_path.write_text(status_str, encoding='utf-8')
    (output / 'latest.json').write_text(json_str, encoding='utf-8')
    (output / 'status.json').write_text(status_str, encoding='utf-8')
    if (output / 'checkpoint.json').exists():
        try:
            (output / 'checkpoint.json').unlink()
        except OSError:
            pass
    update_consolidated_global_report(output)
    return mission


def main(argv=None):
    parser = argparse.ArgumentParser(description='Autonomous Post-Close Multi-Market Equity Research')
    parser.add_argument('--market', default=os.getenv('RESEARCH_MARKET', 'US'), help='US, IN, or ALL')
    parser.add_argument('--output-dir', default='reports/autonomous')
    parser.add_argument('--enforce-session-lock', action='store_true', default=False)
    args = parser.parse_args(argv)

    target = normalize_market(args.market)
    markets_to_run = ['US', 'IN'] if target == 'ALL' else [target]
    for mkt in markets_to_run:
        env_var = 'RESEARCH_WATCHLIST_INDIA' if mkt == 'IN' else 'RESEARCH_WATCHLIST'
        default_wl = DEFAULT_INDIA_WATCHLIST if mkt == 'IN' else DEFAULT_WATCHLIST
        raw_wl = os.getenv(env_var) or os.getenv('INDIA_RESEARCH_WATCHLIST' if mkt == 'IN' else 'US_RESEARCH_WATCHLIST') or default_wl
        symbols = parse_watchlist(raw_wl, market=mkt)
        mission = run_mission(
            symbols,
            output_dir=args.output_dir,
            sec_user_agent=os.getenv('SEC_USER_AGENT', ''),
            alpha_key=os.getenv('ALPHAVANTAGE_API_KEY', ''),
            groq_key=os.getenv('GROQ_API_KEY', ''),
            market=mkt,
            enforce_once_per_session=args.enforce_session_lock,
        )
        counts = mission.get('outcome_counts', {})
        print(
            f"[{mkt}] Mission status: {mission['status']} | session: {mission['market_session_date']} | "
            f"COMPLETE: {counts.get('COMPLETE', 0)}, PARTIAL: {counts.get('PARTIAL', 0)}, FAILED: {counts.get('FAILED', 0)}"
        )
        if mission.get('skipped_duplicate_session'):
            print(f"[{mkt}] Skipped duplicate scheduled run for already-completed session {mission['market_session_date']}")
        elif mission['status'] in ('PARTIAL', 'FAILED'):
            print(f"[{mkt}] {mission['status']} evidence; incomplete findings retained, no fabricated scores")


if __name__ == '__main__':
    main()
