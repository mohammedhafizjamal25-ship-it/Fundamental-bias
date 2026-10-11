#!/usr/bin/env python3
"""
Daily fundamental bias engine for Gold (XAUUSD) and Bitcoin (BTCUSD).

Pulls free public data (FRED, Yahoo Finance, CFTC, alternative.me), scores 7
fundamental factors from -2 (bearish) to +2 (bullish), weights them per asset,
and writes data.json for index.html.

Usage:
    python fetch_bias.py            # live data
    python fetch_bias.py --demo     # sample data, no internet needed

Optional manual.json (same folder) for things no free feed provides:
    {"geopolitics": 1.5, "central_bank_buying": 1, "note": "why"}
    values are -2..+2 (positive = bullish for gold)
"""
import csv, io, json, os, sys, urllib.parse, urllib.request
import datetime as dt
from statistics import mean, correlation

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (bias-dashboard)"}
ERRORS = []


# ---------------------------------------------------------------- helpers
def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def clip(x, lo=-2.0, hi=2.0):
    return max(lo, min(hi, x))


def pct(a, b):
    return (a / b - 1) * 100


def fred(series, n=400):
    """Free FRED CSV endpoint, no API key. Returns [(date, value)]."""
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}")
    out = []
    for row in list(csv.reader(io.StringIO(txt)))[1:]:
        try:
            out.append((row[0], float(row[1])))
        except (ValueError, IndexError):
            pass
    return out[-n:]


def yahoo(symbol, rng="1y"):
    """Daily bars from Yahoo Finance chart endpoint. Returns list of dicts."""
    sym = urllib.parse.quote(symbol)
    j = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"))
    r = j["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    bars = []
    for ts, c, v in zip(r["timestamp"], q["close"], q.get("volume") or [0] * len(r["timestamp"])):
        if c is not None:
            d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
            bars.append({"d": d, "c": c, "v": v or 0})
    return bars


def closes(bars):
    return [b["c"] for b in bars]


def ret_corr(a, b, n=60):
    da = {x["d"]: x["c"] for x in a}
    db = {x["d"]: x["c"] for x in b}
    days = sorted(set(da) & set(db))[-(n + 1):]
    if len(days) < 20:
        return None
    ra = [da[days[i]] / da[days[i - 1]] - 1 for i in range(1, len(days))]
    rb = [db[days[i]] / db[days[i - 1]] - 1 for i in range(1, len(days))]
    return correlation(ra, rb)


def res(gold, btc=None, detail="", ok=True):
    return {"gold": round(clip(gold), 2), "btc": round(clip(gold if btc is None else btc), 2),
            "detail": detail, "ok": ok}


def missing(why):
    ERRORS.append(why)
    return {"gold": 0.0, "btc": 0.0, "detail": "Data unavailable: " + why, "ok": False}


def label(score):
    if score >= 35: return "Strong bullish"
    if score >= 12: return "Bullish"
    if score > -12: return "Neutral"
    if score > -35: return "Bearish"
    return "Strong bearish"


# ---------------------------------------------------------------- factors
def f_policy():
    """1. Interest rates and Fed policy: what the 2y yield says vs the actual Fed rate."""
    ff = fred("DFF", 120)
    y2 = fred("DGS2", 120)
    rate, y = ff[-1][1], y2[-1][1]
    spread = y - rate
    s = 2 if spread < -0.75 else 1 if spread < -0.25 else -2 if spread > 0.75 else -1 if spread > 0.25 else 0
    d = (y - y2[-21][1]) * 100
    s += 1 if d < -10 else -1 if d > 10 else 0
    stance = "pricing cuts" if spread < -0.25 else "pricing hikes" if spread > 0.25 else "pricing a hold"
    return res(s * 2 / 3, detail=f"Fed funds {rate:.2f}%, 2y yield {y:.2f}% ({spread*100:+.0f}bp). Market is {stance}. 2y moved {d:+.0f}bp in 1 month.")


def f_yields():
    """2. Treasury yields and real yields (strongest driver for gold)."""
    real = fred("DFII10", 120)
    nom = fred("DGS10", 120)
    dr = (real[-1][1] - real[-21][1]) * 100
    dn = (nom[-1][1] - nom[-21][1]) * 100
    sr = 2 if dr < -15 else 1 if dr < -5 else -2 if dr > 15 else -1 if dr > 5 else 0
    sn = 1 if dn < -15 else -1 if dn > 15 else 0
    return res(sr + 0.5 * sn, detail=f"10y real yield {real[-1][1]:.2f}% ({dr:+.0f}bp in 1m), 10y nominal {nom[-1][1]:.2f}% ({dn:+.0f}bp). Falling real yields support gold and BTC.")


def f_macro():
    """3. CPI, PCE, NFP, unemployment. Cooling inflation / weak jobs = dovish = bullish."""
    parts, notes = [], []
    for sid, name in (("CPIAUCSL", "CPI"), ("PCEPI", "PCE")):
        v = [x[1] for x in fred(sid, 60)]
        now, before = pct(v[-1], v[-13]), pct(v[-4], v[-16])
        ch = now - before
        parts.append(-1 if ch > 0.15 else 1 if ch < -0.15 else 0)
        notes.append(f"{name} {now:.1f}% y/y ({ch:+.2f}pt vs 3m ago)")
    p = [x[1] for x in fred("PAYEMS", 24)]
    avg3 = (p[-1] - p[-4]) / 3
    parts.append(-1 if avg3 > 200 else 1 if avg3 < 50 else 0)
    notes.append(f"NFP 3m avg {avg3:+.0f}k")
    u = [x[1] for x in fred("UNRATE", 24)]
    du = u[-1] - u[-4]
    parts.append(1 if du >= 0.3 else -1 if du <= -0.2 else 0)
    notes.append(f"unemployment {u[-1]:.1f}% ({du:+.1f}pt in 3m)")
    return res(mean(parts) * 2, detail="; ".join(notes) + ". Hot data = hawkish = bearish; soft data = dovish = bullish.")


def f_dollar(cache):
    """4. DXY and its relationship to gold."""
    dx = yahoo("DX-Y.NYB", "1y")
    cache["dxy"] = dx
    c = closes(dx)
    ma50, p = mean(c[-50:]), c[-1]
    ch = pct(p, c[-21])
    s = (1 if p < ma50 else -1) + (1 if ch < -1 else -1 if ch > 1 else 0)
    corr = ret_corr(dx, cache.get("gold", []))
    ctext = f" 60d correlation with gold: {corr:+.2f}." if corr is not None else ""
    return res(s, detail=f"DXY {p:.2f}, {'below' if p < ma50 else 'above'} its 50d average ({ma50:.2f}), {ch:+.1f}% in 1m.{ctext}")


def f_risk():
    """5. Geopolitical risk / safe-haven demand, proxied by VIX and equity drawdown. Manual override supported."""
    vix = closes(yahoo("^VIX", "3mo"))
    spx = closes(yahoo("^GSPC", "6mo"))
    v = vix[-1]
    dd = pct(spx[-1], max(spx[-63:]))
    g = (2 if v > 28 else 1 if v > 20 else -1 if v < 14 else 0) + (1 if dd < -7 else 0)
    b = (-2 if v > 28 else -1 if v > 20 else 1 if v < 15 else 0) + (-1 if dd < -7 else 0)
    man = load_manual()
    note = ""
    if "geopolitics" in man:
        g += float(man["geopolitics"])
        note = f" Manual geopolitics input: {man['geopolitics']:+}" + (f" ({man['note']})" if man.get("note") else "") + "."
    return res(g, b, f"VIX {v:.1f}, S&P 500 {dd:.1f}% from 3m high. Fear lifts gold, hurts BTC.{note}")


def etf_signal(bars):
    c = closes(bars)
    v = [b["v"] for b in bars]
    r20 = pct(c[-1], c[-21])
    vr = mean(v[-20:]) / mean(v[-60:]) if mean(v[-60:]) else 1
    s = 0
    if r20 > 1: s = 1 + (1 if vr > 1.1 else 0)
    elif r20 < -1: s = -1 - (1 if vr > 1.1 else 0)
    return s, r20, vr


def f_demand():
    """6. Central banks, ETFs, physical demand. ETF flow is proxied by price x volume."""
    man = load_manual()
    cb = float(man.get("central_bank_buying", 1))
    gs, gr, gv = etf_signal(yahoo("GLD", "6mo"))
    gold = (cb + gs) / 2
    btc, text_b = 0.0, "BTC ETF data unavailable."
    try:
        bs, br, bv = etf_signal(yahoo("IBIT", "6mo"))
        btc, text_b = bs, f"IBIT {br:+.1f}% in 1m, volume x{bv:.2f} vs 3m avg."
    except Exception as e:
        ERRORS.append(f"IBIT: {e}")
    return res(gold, btc, f"Central-bank buying input {cb:+.0f} (structural net buying since 2022; edit manual.json). GLD {gr:+.1f}% in 1m, volume x{gv:.2f}. {text_b}")


def cot_percentile():
    q = urllib.parse.urlencode({
        "$where": "market_and_exchange_names like 'GOLD - COMMODITY EXCHANGE%'",
        "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "156"})
    rows = json.loads(get("https://publicreporting.cftc.gov/resource/72hh-3qpy.json?" + q))
    nets = [int(r["m_money_positions_long_all"]) - int(r["m_money_positions_short_all"]) for r in rows]
    return sum(1 for n in nets if n <= nets[0]) / len(nets) * 100


def fear_greed():
    j = json.loads(get("https://api.alternative.me/fng/?limit=1"))
    return int(j["data"][0]["value"])


def f_sentiment(cache):
    """7. Market expectations, positioning, news reaction (trend + COT + fear/greed)."""
    def trend(bars):
        c = closes(bars)
        return ((1 if c[-1] > mean(c[-50:]) else -1) + (1 if c[-1] > mean(c[-200:]) else -1)) / 2

    g_tr = trend(cache["gold"])
    g_notes = [f"gold {'above' if g_tr > 0 else 'below' if g_tr < 0 else 'mixed vs'} its 50d/200d averages"]
    g = g_tr
    try:
        p = cot_percentile()
        g += -1 if p > 90 else 1 if p < 10 else 0
        g_notes.append(f"managed-money gold positioning at {p:.0f}th percentile (3y)")
    except Exception as e:
        ERRORS.append(f"COT: {e}")
    b_bars = yahoo("BTC-USD", "1y")
    cache["btc"] = b_bars
    b_tr = trend(b_bars)
    b = b_tr
    b_notes = [f"BTC {'above' if b_tr > 0 else 'below' if b_tr < 0 else 'mixed vs'} its 50d/200d averages"]
    try:
        fg = fear_greed()
        b += 1 if fg < 20 else -1 if fg > 80 else 0
        b_notes.append(f"crypto Fear & Greed {fg}")
    except Exception as e:
        ERRORS.append(f"Fear&Greed: {e}")
    return res(g, b, "Gold: " + ", ".join(g_notes) + ". BTC: " + ", ".join(b_notes) + ". Crowded positioning is treated as a contrarian warning.")


# ---------------------------------------------------------------- engine
FACTORS = [
    ("policy", "Rates and Fed policy"),
    ("yields", "Treasury and real yields"),
    ("macro", "CPI, PCE, NFP, unemployment"),
    ("dollar", "US dollar (DXY)"),
    ("risk", "Geopolitical risk and safe haven"),
    ("demand", "Central banks, ETFs, physical demand"),
    ("sentiment", "Positioning and market reaction"),
]
WEIGHTS = {
    "gold": {"policy": 1.5, "yields": 2.0, "macro": 1.0, "dollar": 1.5, "risk": 1.0, "demand": 1.0, "sentiment": 1.0},
    "btc":  {"policy": 1.5, "yields": 1.0, "macro": 1.0, "dollar": 1.0, "risk": 1.5, "demand": 1.5, "sentiment": 1.5},
}


def load_manual():
    try:
        with open(os.path.join(HERE, "manual.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def run_factor(key, fn):
    try:
        return fn()
    except Exception as e:
        return missing(f"{key}: {e}")


def collect_live():
    cache = {}
    snap = {}
    try:
        cache["gold"] = yahoo("GC=F", "1y")
    except Exception as e:
        ERRORS.append(f"gold price: {e}")
        cache["gold"] = []
    out = {
        "policy": run_factor("policy", f_policy),
        "yields": run_factor("yields", f_yields),
        "macro": run_factor("macro", f_macro),
        "dollar": run_factor("dollar", lambda: f_dollar(cache)),
        "risk": run_factor("risk", f_risk),
        "demand": run_factor("demand", f_demand),
        "sentiment": run_factor("sentiment", lambda: f_sentiment(cache)),
    }
    try:
        snap["gold"] = round(cache["gold"][-1]["c"], 2)
        snap["dxy"] = round(cache["dxy"][-1]["c"], 2)
        snap["btc"] = round(cache["btc"][-1]["c"], 0)
        snap["us10y"] = fred("DGS10", 5)[-1][1]
        snap["real10y"] = fred("DFII10", 5)[-1][1]
    except Exception:
        pass
    return out, snap


def collect_demo():
    d = lambda g, b, t: {"gold": g, "btc": b, "detail": t, "ok": True}
    return ({
        "policy": d(1.0, 1.0, "DEMO: Fed funds 3.75%, 2y yield 3.40% (-35bp). Market pricing cuts."),
        "yields": d(1.5, 1.5, "DEMO: 10y real yield 1.70% (-12bp in 1m)."),
        "macro": d(0.7, 0.7, "DEMO: CPI 2.9% y/y cooling; NFP 3m avg +70k; unemployment 4.5%."),
        "dollar": d(1.0, 1.0, "DEMO: DXY 97.8, below its 50d average. 60d correlation with gold: -0.41."),
        "risk": d(0.0, -0.0, "DEMO: VIX 17.5, S&P 500 -1.2% from 3m high."),
        "demand": d(1.5, 0.5, "DEMO: central-bank buying input +1; GLD +3.1% in 1m on rising volume."),
        "sentiment": d(-0.5, 1.0, "DEMO: gold extended, positioning crowded (93rd percentile). BTC above 50d, Fear & Greed 28."),
    }, {"gold": 4020.0, "dxy": 97.8, "btc": 112000, "us10y": 4.05, "real10y": 1.70})


def upcoming_events(today):
    # 2026 FOMC decision days. Verify at federalreserve.gov/monetarypolicy/fomccalendars.htm
    fomc = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
    ev = [{"date": d, "name": "FOMC rate decision"} for d in fomc if d >= today.isoformat()][:2]
    # NFP: normally first Friday of the month (BLS may shift it)
    for add in (0, 1):
        y, m = today.year + (today.month + add - 1) // 12, (today.month + add - 1) % 12 + 1
        d = dt.date(y, m, 1)
        while d.weekday() != 4:
            d += dt.timedelta(days=1)
        if d >= today:
            ev.append({"date": d.isoformat(), "name": "US jobs report (NFP), usual date"})
            break
    return sorted(ev, key=lambda e: e["date"])


def build(demo=False):
    today = dt.datetime.now(dt.timezone.utc)
    factors, snap = collect_demo() if demo else collect_live()
    try:
        with open(os.path.join(HERE, "data.json")) as f:
            history = json.load(f).get("history", [])
    except Exception:
        history = []
    history = [h for h in history if h["date"] != today.date().isoformat()]
    entry = {"date": today.date().isoformat()}
    assets = {}
    for asset in ("gold", "btc"):
        rows, tot, wsum = [], 0.0, 0.0
        for key, name in FACTORS:
            f, w = factors[key], WEIGHTS[asset][key]
            sc = f[asset]
            if f["ok"]:
                tot += sc * w
                wsum += w
            rows.append({"key": key, "name": name, "score": sc, "weight": w, "ok": f["ok"], "detail": f["detail"]})
        comp = round(tot / wsum / 2 * 100, 1) if wsum else 0.0
        entry[asset] = comp
        past = [h[asset] for h in history if asset in h][-5:]
        exp = clip(comp + 0.5 * (comp - past[0]), -100, 100) if past else comp
        live = [r for r in rows if r["ok"]]
        agree = sum(1 for r in live if r["score"] * comp > 0) / len(live) if live and comp else 0
        up = sorted(live, key=lambda r: -r["score"] * r["weight"])
        sup = [r["name"] for r in up[:2] if r["score"] > 0]
        head = [r["name"] for r in up[::-1][:2] if r["score"] < 0]
        assets[asset] = {
            "composite": comp, "bias": label(comp), "expected": round(exp, 1), "expected_bias": label(exp),
            "confidence": round(agree * 100), "factors": rows, "supports": sup, "headwinds": head,
            "coverage": f"{len(live)}/{len(rows)} factors live",
        }
    history.append(entry)
    return {
        "generated_utc": today.strftime("%Y-%m-%d %H:%M"), "demo": demo, "market": snap,
        "assets": assets, "events": upcoming_events(today.date()), "errors": ERRORS,
        "history": history[-60:],
    }


if __name__ == "__main__":
    data = build(demo="--demo" in sys.argv)
    with open(os.path.join(HERE, "data.json"), "w") as f:
        json.dump(data, f, indent=1)
    for a, v in data["assets"].items():
        print(f"{a.upper()}: {v['bias']} ({v['composite']:+.1f}), expected {v['expected_bias']}, confidence {v['confidence']}%")
    if ERRORS:
        print("Warnings:", *ERRORS, sep="\n  ")
