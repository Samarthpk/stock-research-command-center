"""Run via cron / scheduled task once a day; persists and prints a report."""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import run,report
out=run()
p=Path(__file__).with_name('reports');p.mkdir(exist_ok=True)
filename=p / (datetime.datetime.now().strftime('%Y-%m-%d_%H%M')+'.md')
filename.write_text(report(out),encoding='utf-8')
print(filename)
