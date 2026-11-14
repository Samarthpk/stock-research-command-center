"""Stock research engine. Research signals only; not trading advice."""
import os, json, sqlite3, urllib.request, urllib.parse, datetime, statistics
from pathlib import Path

DB = Path(os.getenv('STOCK_DB', str(Path(__file__).with_name('research.db'))))
API = 'https://www.alphavantage.co/query'
DEFAULT = ['AAPL','MSFT','NVDA','GOOGL','AMZN','SPY']

def init_db():
    with sqlite3.connect(DB) as db:
        db.execute('CREATE TABLE IF NOT EXISTS results (symbol TEXT, asof TEXT, close REAL, change20 REAL, ma20 REAL, ma50 REAL, volatility REAL, stance TEXT, rationale TEXT, source TEXT, PRIMARY KEY(symbol,asof))')
        db.execute('CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY)')
        if not db.execute('SELECT 1 FROM watchlist LIMIT 1').fetchone():
            db.executemany('INSERT INTO watchlist VALUES (?)', [(s,) for s in DEFAULT])

def watchlist():
    init_db()
    with sqlite3.connect(DB) as db:
        return [r[0] for r in db.execute('SELECT symbol FROM watchlist ORDER BY symbol')]

def set_watchlist(symbols):
    vals = list(dict.fromkeys(x.strip().upper() for x in symbols if x.strip()))
    if any(not s.replace('.','').replace('-','').isalnum() or len(s)>12 for s in vals):
        raise ValueError('Invalid ticker')
    if len(vals)>20: raise ValueError('Maximum 20 tickers')
    init_db()
    with sqlite3.connect(DB) as db:
        db.execute('DELETE FROM watchlist')
        db.executemany('INSERT INTO watchlist VALUES (?)',[(s,) for s in vals])

def request_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'StockResearchCommandCenter/0.1 (personal research)'})
    with urllib.request.urlopen(req,timeout=25) as response:
        return json.load(response)

def fetch_daily(symbol, key=None):
    key=key or os.environ.get('ALPHAVANTAGE_API_KEY')
    if not key: raise RuntimeError('Missing ALPHAVANTAGE_API_KEY')
    url=API+'?'+urllib.parse.urlencode({'function':'TIME_SERIES_DAILY','symbol':symbol,'outputsize':'compact','apikey':key})
    obj=request_json(url)
    if 'Note' in obj or 'Information' in obj: raise RuntimeError(obj.get('Note') or obj['Information'])
    data=obj.get('Time Series (Daily)')
    if not data: raise RuntimeError(f'No valid daily data for {symbol}: {str(obj)[:150]}')
    rows=sorted([(d,float(v['4. close'])) for d,v in data.items()],key=lambda x:x[0])
    return rows, 'https://www.alphavantage.co/documentation/#daily'

def analyze(symbol, rows, source):
    if len(rows)<51: raise ValueError('Need at least 51 trading days for MA50 and 20-day momentum')
    prices=[p for _,p in rows]
    last=prices[-1]; ma20=statistics.mean(prices[-20:]);ma50=statistics.mean(prices[-50:])
    momentum=last/prices[-21]-1
    returns=[prices[i]/prices[i-1]-1 for i in range(len(prices)-19,len(prices))]
    vol=statistics.stdev(returns)*(252**0.5) if len(returns)>1 else 0
    if last>ma20>ma50 and momentum>0.02: stance='Positive trend'
    elif last<ma20<ma50 and momentum< -0.02: stance='Negative trend'
    else: stance='Mixed / watch'
    rationale=f'Close ${last:.2f}; MA20 ${ma20:.2f}; MA50 ${ma50:.2f}; 20-session return {momentum:+.1%}; annualized recent volatility {vol:.1%}. Technical trend only; no valuation or news analysis.'
    return dict(symbol=symbol,asof=rows[-1][0],close=round(last,2),change20=round(momentum*100,2),ma20=round(ma20,2),ma50=round(ma50,2),volatility=round(vol*100,2),stance=stance,rationale=rationale,source=source)

def store(row):
    init_db()
    keys=['symbol','asof','close','change20','ma20','ma50','volatility','stance','rationale','source']
    with sqlite3.connect(DB) as db:
        db.execute('INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?)',[row[k] for k in keys])

def latest():
    init_db()
    with sqlite3.connect(DB) as db:
        db.row_factory=sqlite3.Row
        return [dict(r) for r in db.execute('SELECT r.* FROM results r INNER JOIN (SELECT symbol, MAX(asof) AS asof FROM results GROUP BY symbol) q ON r.symbol=q.symbol AND r.asof=q.asof ORDER BY symbol')]

def run(symbols=None):
    symbols= symbols if symbols is not None else watchlist()
    results=[]; errors=[]
    for s in symbols:
        try:
            rows,source=fetch_daily(s)
            row=analyze(s,rows,source);store(row);results.append(row)
        except Exception as e:
            errors.append({'symbol':s,'error':str(e)})
    return {'results':results,'errors':errors,'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}

def report(runout):
    lines=['# Stock Research Daily Brief','',f"Checked (UTC): {runout['checked_at_utc']}",'', 'Educational technical screening only — not investment advice, real-time quotes, or verified buy/sell recommendations.','']
    for r in runout['results']:
        lines += [f"## {r['symbol']} — {r['stance']}",f"As of: {r['asof']}",r['rationale'],f"Data method: {r['source']}",'']
    if runout['errors']:
        lines += ['## Unavailable / not checked']+[f"- {e['symbol']}: {e['error']}" for e in runout['errors']]
    if not runout['results']:lines += ['**No successful live data fetches. No signals issued.**']
    return '\n'.join(lines)

if __name__=='__main__':
    init_db();out=run();print(report(out))
