"""Full market scanner: approved bulk licensed CSV feed, cron and manual dispatch.

The endpoint must be configured and owned by an authorized market-data vendor.
Never claim full-market coverage unless exchange directory coverage and all
eligible symbols are analyzed. Public snapshots exclude private API secrets.
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from listed_universe import load_directory
from market_scanner import validate_bulk_upload,screen_bulk,MAX_CSV_BYTES

def fetch_bulk_csv(endpoint,token):
    parsed=urllib.parse.urlparse(endpoint or "")
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Approved HTTPS bulk data endpoint required")
    if parsed.hostname in ("localhost","127.0.0.1","::1") or parsed.hostname.endswith((".internal",".local")):
        raise ValueError("Private network endpoints not supported")
    if not token:
        raise ValueError("Licensed bulk data token required")
    req=urllib.request.Request(endpoint,headers={
        "Authorization":"Bearer "+token,
        "Accept":"text/csv",
        "User-Agent":"StockResearchCommandCenter/0.7"
    })
    with urllib.request.urlopen(req,timeout=60) as resp:
        b=resp.read(MAX_CSV_BYTES+1)
        if len(b)>MAX_CSV_BYTES:raise ValueError("Bulk feed larger than 100MB")
    return b

def mission(*,market="ALL",csv_bytes=None,source="licensed_bulk_feed",today=None):
    directory=load_directory()
    if not directory["rows"]:
        return {"status":"UNIVERSE_UNAVAILABLE","sources":directory["sources"],"ranked":[]}
    if csv_bytes is None:
        return {"status":"DATA_FEED_UNAVAILABLE","sources":directory["sources"],
                "note":"Provide licensed bulk OHLCV. No synthetic stock results.","ranked":[]}
    parsed=validate_bulk_upload(csv_bytes,directory["rows"])
    result=screen_bulk(parsed,directory["rows"],market=market,today=today)
    result["scan_time_utc"]=dt.datetime.now(dt.timezone.utc).isoformat()
    result["feed_origin"]=source
    result["directory_status"]=directory["sources"]
    if not directory["complete_coverage"]:
        result["status"]="PARTIAL_UNIVERSE"
    return result

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--market",choices=["US","IN","ALL"],default="ALL")
    p.add_argument("--output",default="reports/scanner/market_scan_latest.json")
    a=p.parse_args()
    endpoint=os.getenv("LICENSED_BULK_CSV_URL","")
    token=os.getenv("LICENSED_BULK_CSV_TOKEN","")
    if not endpoint or not token:
        result={"status":"DATA_FEED_NOT_CONFIGURED","ranked":[],
                "note":"No licensed full-market bulk feed configured. The free Alpha Vantage key cannot supply it."}
    else:
        try:
            b=fetch_bulk_csv(endpoint,token)
            result=mission(market=a.market,csv_bytes=b,source="licensed_bulk_feed")
        except (OSError,ValueError,TimeoutError) as exc:
            result={"status":"DATA_FEED_ERROR","ranked":[],"error_type":type(exc).__name__}
    dest=Path(a.output)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print("Scan status:",result["status"],"analyzed:",result.get("analyzed",0),
          "universe:",result.get("universe_count","unknown"))
    return 0

if __name__=="__main__":
    sys.exit(main())
