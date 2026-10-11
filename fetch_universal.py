#!/usr/bin/env python3
"""
Universal fundamental bias engine: forex pairs, commodities, crypto.
Needs fetch_bias.py in the same folder (it reuses that file's data helpers).

    python fetch_universal.py          # live data -> pairs_data.json
    python fetch_universal.py --demo   # sample data, no internet needed

To change the watchlist, create pairs.json next to this file:
  [{"symbol": "EURUSD=X", "name": "EUR/USD", "type": "forex"},
   {"symbol": "GC=F", "name": "Gold", "type": "commodity", "sub": "precious"},
   {"symbol": "ETH-USD", "name": "Ethereum", "type": "crypto"}]
Symbols are Yahoo Finance tickers. Commodity "sub": precious | industrial | energy | agri.
Forex symbols must look like EURUSD=X (two of USD EUR GBP JPY AUD NZD CAD CHF).
"""
import csv, hashlib, io, json, math, os, random, sys, time
import datetime as dt
from statistics import mean, pstdev

import fetch_bias as fb

HERE = os.path.dirname(os.path.abspath(__file__))
clip, pct = fb.clip, fb.pct

DEFAULT_PAIRS = [
    {"symbol": "EURUSD=X", "name": "EUR/USD", "type": "forex"},
    {"symbol": "GBPUSD=X", "name": "GBP/USD", "type": "forex"},
    {"symbol": "USDJPY=X", "name": "USD/JPY", "type": "forex"},
    {"symbol": "AUDUSD=X", "name": "AUD/USD", "type": "forex"},
    {"symbol": "NZDUSD=X", "name": "NZD/USD", "type": "forex"},
    {"symbol": "USDCAD=X", "name": "USD/CAD", "type": "forex"},
    {"symbol": "USDCHF=X", "name": "USD/CHF", "type": "forex"},
    {"symbol": "EURJPY=X", "name": "EUR/JPY", "type": "forex"},
    {"symbol": "GBPJPY=X", "name": "GBP/JPY", "type": "forex"},
    {"symbol": "EURGBP=X", "name": "EUR/GBP", "type": "forex"},
    {"symbol": "GC=F", "name": "Gold", "type": "commodity", "sub": "precious"},
    {"symbol": "SI=F", "name": "Silver", "type": "commodity", "sub": "precious"},
    {"symbol": "PL=F", "name": "Platinum", "type": "commodity", "sub": "precious"},
    {"symbol": "HG=F", "name": "Copper", "type": "commodity", "sub": "industrial"},
    {"symbol": "CL=F", "name": "WTI crude oil", "type": "commodity", "sub": "energy"},
    {"symbol": "BZ=F", "name": "Brent crude oil", "type": "commodity", "sub": "energy"},
    {"symbol": "NG=F", "name": "Natural gas", "type": "commodity", "sub": "energy"},
    {"symbol": "ZW=F", "name": "Wheat", "type": "commodity", "sub": "agri"},
    {"symbol": "BTC-USD", "name": "Bitcoin", "type": "crypto"},
    {"symbol": "ETH-USD", "name": "Ethereum", "type": "crypto"},
    {"symbol": "SOL-USD", "name": "Solana", "type": "crypto"},
    {"symbol": "XRP-USD", "name": "XRP", "type": "crypto"},
    {"symbol": "BNB-USD", "name": "BNB", "type": "crypto"},
]

NAMES = {
    "policy": "Fed policy and rate expectations",
    "yields": "Treasury and real yields",
    "macro": "Inflation and jobs data",
    "usd": "US dollar trend",
    "risk": "Risk appetite",
    "growth": "Global growth (stocks, China, copper)",
    "sent": "Crypto sentiment",
    "btc": "Bitcoin lead",
    "rates": "Interest-rate differential",
    "trend": "Price trend and momentum",
}

# How each kind of instrument reacts to the shared market drivers.
# Driver states: policy/yields/macro positive = easing (dovish), usd positive = weaker dollar,
# risk positive = risk-on, growth positive = stronger growth.
# A positive weight means the instrument rises when the driver is positive; negative means the opposite.
COMMODITY = {
    "precious":   {"policy": 1.5, "yields": 2.0, "macro": 1.0, "usd": 1.5, "risk": -1.0, "growth": 0.0},
    "industrial": {"policy": 0.5, "yields": 0.5, "macro": 0.5, "usd": 1.5, "risk": 1.5, "growth": 2.0},
    "energy":     {"policy": 0.5, "yields": 0.0, "macro": 0.0, "usd": 1.0, "risk": 1.0, "growth": 2.0},
    "agri":       {"policy": 0.0, "yields": 0.0, "macro": 0.0, "usd": 1.0, "risk": 0.5, "growth": 0.5},
}
CRYPTO = {"policy": 1.5, "yields": 1.0, "macro": 1.0, "usd": 1.0, "risk": 1.5, "growth": 0.5, "sent": 1.5}
RISK_BETA = {"USD": -0.5, "EUR": 0.25, "GBP": 0.5, "JPY": -1.0, "CHF": -1.0, "AUD": 1.2, "NZD": 1.0, "CAD": 0.7}

# FRED series per currency (first one that works is used)
SHORT = {
    "USD": ["DFF"], "EUR": ["ECBDFR"], "GBP": ["IUDSOIA", "IRSTCB01GBM156N"],
    "JPY": ["IRSTCB01JPM156N", "IRSTCI01JPM156N"], "AUD": ["IRSTCB01AUM156N", "IRSTCI01AUM156N"],
    "NZD": ["IRSTCB01NZM156N", "IRSTCI01NZM156N"], "CAD": ["IRSTCB01CAM156N", "IRSTCI01CAM156N"],
    "CHF": ["IRSTCB01CHM156N", "IRSTCI01CHM156N"],
}
LONG = {
    "USD": ["DGS10"], "EUR": ["IRLTLT01DEM156N", "IRLTLT01EZM156N"], "GBP": ["IRLTLT01GBM156N"],
    "JPY": ["IRLTLT01JPM156N"], "AUD": ["IRLTLT01AUM156N"], "NZD": ["IRLTLT01NZM156N"],
    "CAD": ["IRLTLT01CAM156N"], "CHF": ["IRLTLT01CHM156N"],
}


# ---------------------------------------------------------------- data helpers
def yq(sym, rng="1y"):
    last = None
    for a in range(3):
        try:
            return fb.yahoo(sym, rng)
        except Exception as e:
            last = e
            time.sleep(2 * (a + 1))
    raise last


def fred_try(series, n=400, tries=2):
    """One FRED series with few retries (bad series ids are skipped quickly)."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    since = (dt.date.today() - dt.timedelta(days=1500)).isoformat()
    last = None
    for a in range(tries):
        try:
            out = []
            if key:
                url = ("https://api.stlouisfed.org/fred/series/observations?series_id=%s&api_key=%s"
                       "&file_type=json&observation_start=%s" % (series, key, since))
                for o in json.loads(fb.get(url, 40))["observations"]:
                    try:
                        out.append((o["date"], float(o["value"])))
                    except ValueError:
                        pass
            else:
                txt = fb.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (series, since), 40)
                for row in list(csv.reader(io.StringIO(txt)))[1:]:
                    try:
                        out.append((row[0], float(row[1])))
                    except (ValueError, IndexError):
                        pass
            if out:
                return out[-n:]
            last = RuntimeError("empty response")
        except Exception as e:
            last = e
            if "400" in str(e) or "404" in str(e):
                break
        time.sleep(3)
    raise last


def first_series(ids):
    last = None
    for sid in ids:
        try:
            return fred_try(sid, 400)
        except Exception as e:
            last = e
    raise last or RuntimeError("no series")


def trend_score(c):
    if len(c) < 25:
        return 0
    p = c[-1]
    return (1 if p > mean(c[-50:]) else -1) + (1 if pct(p, c[-21]) > 2 else -1 if pct(p, c[-21]) < -2 else 0)


def momentum(c):
    """Trend and volatility-adjusted 20-day move. Range -2..+2."""
    p = c[-1]
    ma50, ma200 = mean(c[-50:]), mean(c[-200:])
    r20 = c[-1] / c[-21] - 1
    rets = [c[i] / c[i - 1] - 1 for i in range(max(1, len(c) - 60), len(c))]
    z = r20 / ((pstdev(rets) or 1e-9) * math.sqrt(20))
    s = ((1 if p > ma50 else -1) + (1 if p > ma200 else -1)) / 2 + (1 if z > 0.75 else -1 if z < -0.75 else 0)
    d = "Price is %s its 50-day and %s its 200-day average; 20-day move %+.1f%%." % (
        "above" if p > ma50 else "below", "above" if p > ma200 else "below", r20 * 100)
    return s, d


# ---------------------------------------------------------------- shared drivers
def build_drivers(need_crypto):
    D = {}

    def put(key, score, detail, ok=True):
        D[key] = {"key": key, "name": NAMES[key], "score": round(clip(score), 2), "detail": detail, "ok": ok}

    def guard(key, fn):
        try:
            s, d = fn()
            put(key, s, d)
        except Exception as e:
            fb.ERRORS.append("%s: %s" % (key, e))
            put(key, 0, "Data unavailable: %s" % e, ok=False)

    for key, fn in (("policy", fb.f_policy), ("yields", fb.f_yields), ("macro", fb.f_macro)):
        r = fb.run_factor(key, fn)
        put(key, r["gold"], r["detail"], r["ok"])

    def usd():
        c = fb.closes(yq("DX-Y.NYB"))
        p, ma, ch = c[-1], mean(c[-50:]), pct(c[-1], c[-21])
        s = (1 if p < ma else -1) + (1 if ch < -1 else -1 if ch > 1 else 0)
        return s, "DXY %.2f, %s its 50-day average, %+.1f%% in 1 month. Positive means a weaker dollar." % (
            p, "below" if p < ma else "above", ch)

    def risk():
        v = fb.closes(yq("^VIX", "3mo"))[-1]
        spx = fb.closes(yq("^GSPC", "6mo"))
        dd = pct(spx[-1], max(spx[-63:]))
        s = (-2 if v > 28 else -1 if v > 20 else 1 if v < 15 else 0) + (-1 if dd < -7 else 0)
        return s, "VIX %.1f, S&P 500 %.1f%% from its 3-month high. Positive means risk-on." % (v, dd)

    def growth():
        parts, notes = [], []
        for sym, nm in (("^GSPC", "US stocks"), ("FXI", "China stocks"), ("HG=F", "copper")):
            try:
                t = trend_score(fb.closes(yq(sym, "6mo")))
                parts.append(t)
                notes.append("%s %+d" % (nm, t))
            except Exception as e:
                fb.ERRORS.append("growth %s: %s" % (sym, e))
        if not parts:
            raise RuntimeError("no growth inputs")
        return mean(parts), "Trend scores: %s. Positive means stronger global growth." % ", ".join(notes)

    guard("usd", usd)
    guard("risk", risk)
    guard("growth", growth)

    if need_crypto:
        def sent():
            fg = fb.fear_greed()
            s = 2 if fg < 20 else 1 if fg < 35 else -2 if fg > 80 else -1 if fg > 65 else 0
            return s, "Crypto Fear and Greed %d. Extreme fear counts as a contrarian positive, extreme greed as a negative." % fg

        def btc():
            t = trend_score(fb.closes(yq("BTC-USD")))
            return t, "Bitcoin trend score %+d. Altcoins usually follow Bitcoin." % t

        guard("sent", sent)
        guard("btc", btc)
    return D


def age_days(d):
    try:
        return (dt.date.today() - dt.date.fromisoformat(d)).days
    except Exception:
        return 0


def ccy_rates(ccy):
    out = {}
    s = first_series(SHORT[ccy])
    out["short"], out["short_date"] = s[-1][1], s[-1][0]
    try:
        l = first_series(LONG[ccy])
        out["long"], out["long_date"] = l[-1][1], l[-1][0]
        cutoff = (dt.date.fromisoformat(l[-1][0]) - dt.timedelta(days=90)).isoformat()
        prev = [v for d, v in l if d <= cutoff]
        out["long_prev"] = prev[-1] if prev else None
    except Exception as e:
        fb.ERRORS.append("%s 10y yield: %s" % (ccy, e))
    return out


def build_rates(pairs):
    ccys = set()
    for p in pairs:
        if p["type"] == "forex":
            ccys |= {p["symbol"][:3], p["symbol"][3:6]}
    R = {}
    for c in sorted(ccys):
        if c not in SHORT:
            continue
        try:
            R[c] = ccy_rates(c)
        except Exception as e:
            fb.ERRORS.append("%s policy rate: %s" % (c, e))
    return R


def rate_factor(base, quote, R):
    b, q = R.get(base), R.get(quote)
    if not b or not q:
        missing = [x for x in (base, quote) if x not in R]
        return None, "Rate data unavailable for %s." % "/".join(missing)
    diff = b["short"] - q["short"]
    lvl = 2 if diff > 1.5 else 1 if diff > 0.5 else -2 if diff < -1.5 else -1 if diff < -0.5 else 0
    tr, note = 0, ""
    if all(x.get("long") is not None and x.get("long_prev") is not None for x in (b, q)):
        dl = ((b["long"] - q["long"]) - (b["long_prev"] - q["long_prev"])) * 100
        tr = 1 if dl > 15 else -1 if dl < -15 else 0
        note = " The 10-year spread (%s minus %s) moved %+.0fbp in about 3 months." % (base, quote, dl)
    old = max(age_days(b["short_date"]), age_days(q["short_date"]))
    stale = " Rate data is up to %d days old." % old if old > 60 else ""
    return clip(lvl + tr), "Short rates: %s %.2f%% vs %s %.2f%% (%+.2f pts). Higher carry favours the first currency.%s%s" % (
        base, b["short"], quote, q["short"], diff, note, stale)


# ---------------------------------------------------------------- per-instrument analysis
def sensitivities(inst):
    typ = inst["type"]
    if typ == "forex":
        base, quote = inst["symbol"][:3], inst["symbol"][3:6]
        expo = 1 if base == "USD" else -1 if quote == "USD" else 0
        s = {"policy": -1.0 * expo, "yields": -1.0 * expo, "macro": -0.5 * expo, "usd": -2.0 * expo,
             "risk": RISK_BETA.get(base, 0) - RISK_BETA.get(quote, 0)}
        return s
    if typ == "commodity":
        return dict(COMMODITY.get(inst.get("sub", "precious"), COMMODITY["precious"]))
    s = dict(CRYPTO)
    if inst["symbol"] != "BTC-USD":
        s["btc"] = 1.5
    return s


def analyze(inst, D, R):
    sym, typ = inst["symbol"], inst["type"]
    c = fb.closes(yq(sym, "1y"))
    if len(c) < 60:
        raise RuntimeError("not enough price history")
    rows = []

    def add(key, score, weight, detail, ok=True):
        rows.append({"key": key, "name": NAMES[key], "score": round(clip(score), 2),
                     "weight": round(weight, 2), "ok": ok, "detail": detail})

    for key, w in sensitivities(inst).items():
        if abs(w) < 0.2:
            continue
        st = D.get(key)
        if st and st["ok"]:
            pre = "" if w > 0 else "Inverse effect on this instrument. "
            add(key, st["score"] * (1 if w > 0 else -1), abs(w), pre + st["detail"])
        else:
            add(key, 0, abs(w), "Data unavailable.", ok=False)

    if typ == "forex":
        sc, d = rate_factor(sym[:3], sym[3:6], R)
        add("rates", sc or 0, 2.0, d, ok=sc is not None)

    ms, md = momentum(c)
    add("trend", ms, 1.5, md)

    live = [r for r in rows if r["ok"]]
    wsum = sum(r["weight"] for r in live)
    comp = round(sum(r["score"] * r["weight"] for r in live) / wsum / 2 * 100, 1) if wsum else 0.0
    agree = sum(1 for r in live if r["score"] * comp > 0) / len(live) if live and comp else 0
    ranked = sorted(live, key=lambda r: -r["score"] * r["weight"])
    return {
        "symbol": sym, "name": inst.get("name", sym), "type": typ, "sub": inst.get("sub", ""),
        "price": round(c[-1], 5 if c[-1] < 10 else 3 if c[-1] < 100 else 2),
        "chg1d": round(pct(c[-1], c[-2]), 2), "chg5d": round(pct(c[-1], c[-6]), 2),
        "composite": comp, "bias": fb.label(comp), "confidence": round(agree * 100),
        "coverage": "%d/%d factors live" % (len(live), len(rows)),
        "supports": [r["name"] for r in ranked[:2] if r["score"] > 0],
        "headwinds": [r["name"] for r in ranked[::-1][:2] if r["score"] < 0],
        "factors": rows,
    }


# ---------------------------------------------------------------- build
def load_pairs():
    try:
        with open(os.path.join(HERE, "pairs.json")) as f:
            p = json.load(f)
        ok = [x for x in p if x.get("symbol") and x.get("type") in ("forex", "commodity", "crypto")]
        return ok or DEFAULT_PAIRS
    except Exception:
        return DEFAULT_PAIRS


def build():
    pairs = load_pairs()
    D = build_drivers(any(p["type"] == "crypto" for p in pairs))
    R = build_rates(pairs)
    today = dt.datetime.now(dt.timezone.utc)
    try:
        with open(os.path.join(HERE, "pairs_data.json")) as f:
            history = json.load(f).get("history", [])
    except Exception:
        history = []
    history = [h for h in history if h["date"] != today.date().isoformat()]
    entry = {"date": today.date().isoformat()}
    out = []
    for p in pairs:
        try:
            a = analyze(p, D, R)
        except Exception as e:
            fb.ERRORS.append("%s: %s" % (p["symbol"], e))
            continue
        past = [h[p["symbol"]] for h in history if p["symbol"] in h][-5:]
        exp = clip(a["composite"] + 0.5 * (a["composite"] - past[0]), -100, 100) if past else a["composite"]
        a["expected"], a["expected_bias"] = round(exp, 1), fb.label(exp)
        entry[p["symbol"]] = a["composite"]
        out.append(a)
        time.sleep(0.3)
    history.append(entry)
    return {
        "generated_utc": today.strftime("%Y-%m-%d %H:%M"), "demo": False,
        "instruments": out, "drivers": list(D.values()),
        "events": fb.upcoming_events(today.date()), "errors": fb.ERRORS, "history": history[-60:],
    }


# ---------------------------------------------------------------- demo (offline test data)
def install_demo():
    today = dt.date.today()
    bases = {"EURUSD=X": 1.16, "GBPUSD=X": 1.33, "USDJPY=X": 150, "AUDUSD=X": 0.66, "NZDUSD=X": 0.6,
             "USDCAD=X": 1.38, "USDCHF=X": 0.8, "EURJPY=X": 174, "GBPJPY=X": 200, "EURGBP=X": 0.87,
             "GC=F": 4000, "SI=F": 48, "PL=F": 1500, "HG=F": 5, "CL=F": 62, "BZ=F": 66, "NG=F": 3.2,
             "ZW=F": 540, "BTC-USD": 110000, "ETH-USD": 4000, "SOL-USD": 190, "XRP-USD": 2.5, "BNB-USD": 1000}

    def mock_yahoo(sym, rng="1y"):
        rnd = random.Random(int(hashlib.md5(sym.encode()).hexdigest()[:8], 16))
        if sym == "^VIX": base, mu, vol = 17.5, 0.0, 0.04
        elif sym == "DX-Y.NYB": base, mu, vol = 98, -0.0004, 0.004
        elif sym == "^GSPC": base, mu, vol = 6600, 0.0005, 0.007
        elif sym == "FXI": base, mu, vol = 40, 0.0003, 0.012
        else:
            base = bases.get(sym, 100)
            mu = rnd.uniform(-0.0005, 0.0007)
            vol = 0.005 if sym.endswith("=X") else 0.03 if sym.endswith("-USD") else 0.012
        bars, p = [], base
        for i in range(260):
            p *= 1 + mu + vol * rnd.gauss(0, 1)
            d = (today - dt.timedelta(days=259 - i)).isoformat()
            bars.append({"d": d, "c": p, "v": 1_000_000 * (1 + 0.2 * rnd.random())})
        return bars

    def mock_fred(series, n=400, tries=2):
        s = series.upper()
        daily = s in ("DFF", "DGS2", "DGS10", "DFII10", "ECBDFR", "IUDSOIA")
        cnt = 200 if daily else 60
        step = 1 if daily else 30
        if s == "DFF": lo, hi = 3.75, 3.75
        elif s == "DGS2": lo, hi = 3.6, 3.4
        elif s == "DGS10": lo, hi = 4.15, 4.05
        elif s == "DFII10": lo, hi = 1.85, 1.7
        elif s == "ECBDFR": lo, hi = 2.0, 2.0
        elif s == "IUDSOIA": lo, hi = 4.0, 3.9
        elif "01JP" in s: lo, hi = (0.25, 0.5) if "IRST" in s else (1.4, 1.6)
        elif "01AU" in s: lo, hi = (3.85, 3.6) if "IRST" in s else (4.3, 4.2)
        elif "01NZ" in s: lo, hi = (3.0, 2.5) if "IRST" in s else (4.4, 4.3)
        elif "01CA" in s: lo, hi = (2.75, 2.5) if "IRST" in s else (3.3, 3.2)
        elif "01CH" in s: lo, hi = (0.25, 0.0) if "IRST" in s else (0.5, 0.4)
        elif "01GB" in s: lo, hi = 4.4, 4.3
        elif "01DE" in s or "01EZ" in s: lo, hi = 2.5, 2.6
        elif s in ("CPIAUCSL", "PCEPI"): lo, hi = 320, 332
        elif s == "PAYEMS": lo, hi = 159000, 159450
        elif s == "UNRATE": lo, hi = 4.2, 4.5
        else: lo, hi = 1, 1
        out = []
        for i in range(cnt):
            f = i / (cnt - 1)
            d = (today - dt.timedelta(days=step * (cnt - 1 - i))).isoformat()
            out.append((d, lo + (hi - lo) * f))
        return out[-n:]

    fb.yahoo = mock_yahoo
    fb.fred = mock_fred
    fb.fear_greed = lambda: 27
    globals()["fred_try"] = mock_fred


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    if demo:
        install_demo()
    data = build()
    data["demo"] = demo
    with open(os.path.join(HERE, "pairs_data.json"), "w") as f:
        json.dump(data, f, indent=1)
    ranked = sorted(data["instruments"], key=lambda i: -i["composite"])
    print("Most bullish:", ", ".join("%s %+.0f" % (i["name"], i["composite"]) for i in ranked[:3]))
    print("Most bearish:", ", ".join("%s %+.0f" % (i["name"], i["composite"]) for i in ranked[-3:]))
    print("%d instruments scored" % len(ranked))
    if fb.ERRORS:
        print("Warnings:", *fb.ERRORS, sep="\n  ")
