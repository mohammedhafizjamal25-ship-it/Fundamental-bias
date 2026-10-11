Mohammed Hafiz Jamal, [Oct 11, 2026 at 3:05 AM]
#!/usr/bin/env python3
“””
Daily fundamental bias engine for Gold (XAUUSD) and Bitcoin (BTCUSD).

Pulls free public data (FRED, Yahoo Finance, CFTC, alternative.me), scores 7
fundamental factors from -2 (bearish) to +2 (bullish), weights them per asset,
and writes data.json for index.html.

Usage:
python fetch_bias.py            # live data
python fetch_bias.py –demo     # sample data, no internet needed

Optional manual.json (same folder) for things no free feed provides:
{“geopolitics”: 1.5, “central_bank_buying”: 1, “note”: “why”}
values are -2..+2 (positive = bullish for gold)
“””
import csv, io, json, os, sys, time, urllib.parse, urllib.request
import datetime as dt
from statistics import mean, correlation

HERE = os.path.dirname(os.path.abspath(**file**))
UA = {“User-Agent”: “Mozilla/5.0 (bias-dashboard)”}
ERRORS = []

# –––––––––––––––––––––––––––––––– helpers

def get(url, timeout=30):
req = urllib.request.Request(url, headers=UA)
with urllib.request.urlopen(req, timeout=timeout) as r:
return r.read().decode(“utf-8”, “replace”)

def clip(x, lo=-2.0, hi=2.0):
return max(lo, min(hi, x))

def pct(a, b):
return (a / b - 1) * 100

def fred(series, n=400):
“”“FRED data with retries. Uses FRED_API_KEY if set (most reliable), else the keyless CSV.”””
key = os.environ.get(“FRED_API_KEY”, “”).strip()
since = (dt.date.today() - dt.timedelta(days=1500)).isoformat()
last = None
for attempt in range(4):
try:
out = []
if key:
url = (“https://api.stlouisfed.org/fred/series/observations?series_id=%s&api_key=%s”
“&file_type=json&observation_start=%s” % (series, key, since))
for o in json.loads(get(url, 45))[“observations”]:
try:
out.append((o[“date”], float(o[“value”])))
except ValueError:
pass
else:
txt = get(“https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s” % (series, since), 45)
for row in list(csv.reader(io.StringIO(txt)))[1:]:
try:
out.append((row[0], float(row[1])))
except (ValueError, IndexError):
pass
if out:
return out[-n:]
last = RuntimeError(“empty response”)
except Exception as e:
last = e
time.sleep(4 * (attempt + 1))
raise last

def yahoo(symbol, rng=“1y”):
“”“Daily bars from Yahoo Finance chart endpoint. Returns list of dicts.”””
sym = urllib.parse.quote(symbol)
j = json.loads(get(f”https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d”))
r = j[“chart”][“result”][0]
q = r[“indicators”][“quote”][0]
bars = []
for ts, c, v in zip(r[“timestamp”], q[“close”], q.get(“volume”) or [0] * len(r[“timestamp”])):
if c is not None:
d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime(”%Y-%m-%d”)
bars.append({“d”: d, “c”: c, “v”: v or 0})
return bars

def closes(bars):
return [b[“c”] for b in bars]

def ret_corr(a, b, n=60):
da = {x[“d”]: x[“c”] for x in a}
db = {x[“d”]: x[“c”] for x in b}
days = sorted(set(da) & set(db))[-(n + 1):]
if len(days) < 20:
return None
ra = [da[days[i]] / da[days[i - 1]] - 1 for i in range(1, len(days))]
rb = [db[days[i]] / db[days[i - 1]] - 1 for i in range(1, len(days))]
return correlation(ra, rb)

def res(gold, btc=None, detail=””, ok=True):
return {“gold”: round(clip(gold), 2), “btc”: round(clip(gold if btc is None else btc), 2),
“detail”: detail, “ok”: ok}

def missing(why):
ERRORS.append(why)
return {“gold”: 0.0, “btc”: 0.0, “detail”: “Data unavailable: “ + why, “ok”: False}

def label(score):
if score >= 35: return “Strong bullish”
if score >= 12: return “Bullish”
if score > -12: return “Neutral”
if score > -35: return “Bearish”
return “Strong bearish”

# –––––––––––––––––––––––––––––––– factors

def f_policy():
“”“1. Interest rates and Fed policy: what the 2y yield says vs the actual Fed rate.”””
ff = fred(“DFF”, 120)
y2 = fred(“DGS2”, 120)
rate, y = ff[-1][1], y2[-1][1]
spread = y - rate
s = 2 if spread < -0.75 else 1 if spread < -0.25 else -2 if spread > 0.75 else -1 if spread > 0.25 else 0
d = (y - y2[-21][1]) * 100
s += 1 if d < -10 else -1 if d > 10 else 0
stance = “pricing cuts” if spread < -0.25 else “pricing hikes” if spread > 0.25 else “pricing a hold”
return res(s * 2 / 3, detail=f”Fed funds {rate:.2f}%, 2y yield {y:.2f}% ({spread*100:+.
