#!/usr/bin/env python3
"""
Bias engine 2. Same watchlist and rules as the first site, plus three extra drivers:

  liquidity  Fed balance sheet minus Treasury cash (TGA) minus reverse repo (FRED: WALCL, WTREGEN, RRPONTSYD)
  credit     high-yield credit spreads (FRED: BAMLH0A0HYM2)
  fci        Chicago Fed financial conditions index (FRED: NFCI)

    python fetch_v2.py           # live data -> pairs_v2.json
    python fetch_v2.py --demo    # sample data, no internet needed

Needs fetch_bias.py and fetch_universal.py in the same folder. It reads and writes only its own files.
"""
import json, os, random, sys, time
import datetime as dt

import fetch_bias as fb
import fetch_universal as fu

HERE = os.path.dirname(os.path.abspath(__file__))
clip = fb.clip

LABEL = {"liquidity": "Fed liquidity", "credit": "credit spreads", "fci": "financial conditions"}
fu.NAMES.update({
    "liquidity": "Fed liquidity (balance sheet, Treasury cash, reverse repo)",
    "credit": "Credit spreads (high-yield)",
    "fci": "Financial conditions (Chicago Fed index)",
})

# Driver states: liquidity positive = easing, credit positive = tight spreads (risk-on),
# fci positive = looser financial conditions.
EXTRA = {
    "precious":   {"liquidity": 1.0, "credit": -0.5, "fci": 0.5},
    "industrial": {"liquidity": 0.5, "credit": 1.0, "fci": 0.5},
    "energy":     {"liquidity": 0.5, "credit": 1.0, "fci": 0.5},
    "agri":       {},
    "index":      {"liquidity": 1.5, "credit": 2.0, "fci": 1.5},
    "bond":       {"liquidity": 0.5, "credit": -1.0},
    "crypto":     {"liquidity": 2.0, "credit": 1.5, "fci": 1.0},
}


def extra_sens(inst):
    t = inst["type"]
    if t == "forex":
        expo = 1 if inst["symbol"][:3] == "USD" else -1 if inst["symbol"][3:6] == "USD" else 0
        return {"liquidity": -1.0 * expo, "credit": -0.5 * expo} if expo else {}
    if t == "commodity":
        return dict(EXTRA.get(inst.get("sub", "precious"), {}))
    if t == "index":
        return dict(EXTRA["index"])
    if t == "bond":
        return dict(EXTRA["bond"])
    return dict(EXTRA["crypto"])


# ---------------------------------------------------------------- scoring rules (also used by the backtest)
def score_liquidity(now, prev):
    chg = (now / prev - 1) * 100 if prev else 0.0
    return (2 if chg > 2 else 1 if chg > 0.5 else -2 if chg < -2 else -1 if chg < -0.5 else 0), chg


def score_credit(level, chg):
    return (1 if chg < -0.3 else -1 if chg > 0.5 else 0) + (-1 if level > 5.5 else 1 if level < 3.5 else 0)


def score_fci(v, chg):
    return (1 if v < -0.4 else -1 if v > -0.1 else 0) + (1 if chg < -0.05 else -1 if chg > 0.05 else 0)


def value_at(series, date):
    for d, v in reversed(series):
        if d <= date:
            return v
    return None


def minus(d, days):
    return (dt.date.fromisoformat(d) - dt.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- live extra drivers
def live_extras():
    E = {}

    def put(key, score, detail, ok=True):
        E[key] = {"key": key, "name": fu.NAMES[key], "score": round(clip(score), 2), "detail": detail, "ok": ok}

    def guard(key, fn):
        try:
            s, d = fn()
            put(key, s, d)
        except Exception as e:
            fb.ERRORS.append("%s: %s" % (key, e))
            put(key, 0, "Data unavailable: %s" % e, ok=False)

    def liquidity():
        wal = fu.fred_try("WALCL", 250)
        tga = fu.fred_try("WTREGEN", 250)
        rrp = fu.fred_try("RRPONTSYD", 500)
        d_now = wal[-1][0]

        def nl(d):
            w, t, r = value_at(wal, d), value_at(tga, d), value_at(rrp, d)
            if w is None or t is None:
                raise RuntimeError("liquidity series do not overlap")
            return w / 1000 - t / 1000 - (r or 0.0)
        now, prev = nl(d_now), nl(minus(d_now, 91))
        s, chg = score_liquidity(now, prev)
        return s, "Fed net liquidity about %s bn, %+.1f%% in 13 weeks (balance sheet minus Treasury cash and reverse repo). Positive means easing." % (
            format(now, ",.0f"), chg)

    def credit():
        oas = fu.fred_try("BAMLH0A0HYM2", 400)
        lvl = oas[-1][1]
        prev = value_at(oas, minus(oas[-1][0], 30))
        chg = lvl - prev if prev is not None else 0.0
        return score_credit(lvl, chg), "High-yield spread %.2f points, %+.2f in 1 month. Tight and tightening is risk-on. Positive means calm credit." % (lvl, chg)

    def fci():
        n = fu.fred_try("NFCI", 120)
        v = n[-1][1]
        prev = value_at(n, minus(n[-1][0], 91))
        chg = v - prev if prev is not None else 0.0
        return score_fci(v, chg), "Chicago Fed index %.2f, %+.2f in 13 weeks. Below zero is looser than average. Positive means easier conditions." % (v, chg)

    guard("liquidity", liquidity)
    guard("credit", credit)
    guard("fci", fci)
    return E


# ---------------------------------------------------------------- build
def enable():
    orig = fu.sensitivities

    def sens2(inst):
        s = orig(inst)
        s.update(extra_sens(inst))
        return s
    fu.sensitivities = sens2


def build():
    enable()
    pairs = fu.load_pairs()
    D = fu.build_drivers(any(p["type"] == "crypto" for p in pairs))
    D.update(live_extras())
    R = fu.build_rates(pairs)
    today = dt.datetime.now(dt.timezone.utc)
    try:
        with open(os.path.join(HERE, "pairs_v2.json")) as f:
            history = json.load(f).get("history", [])
    except Exception:
        history = []
    history = [h for h in history if h["date"] != today.date().isoformat()]
    entry = {"date": today.date().isoformat()}
    out = []
    for p in pairs:
        try:
            a = fu.analyze(p, D, R)
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


# ---------------------------------------------------------------- demo helpers (also used by experiment.py)
DEMO = {"WALCL": (6600000, 7, 15000), "WTREGEN": (800000, 7, 40000), "RRPONTSYD": (400, 1, 25),
        "BAMLH0A0HYM2": (3.6, 1, 0.05), "NFCI": (-0.45, 7, 0.02)}


def demo_series(series, years=10, n=None):
    base, step, vol = DEMO[series.upper()]
    rnd = random.Random(sum(map(ord, series)))
    cnt = int(years * 365 / step)
    v, out = base, []
    today = dt.date.today()
    for i in range(cnt):
        v = max(1e-6 if base > 0 else -5, v + vol * rnd.gauss(0, 1))
        out.append(((today - dt.timedelta(days=step * (cnt - 1 - i))).isoformat(), v))
    return out[-n:] if n else out


def install_demo():
    fu.install_demo()
    base_mock = fu.fred_try

    def mock(series, n=400, tries=2):
        if series.upper() in DEMO:
            return demo_series(series, 6, n)
        return base_mock(series, n, tries)
    fu.fred_try = mock


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    if demo:
        install_demo()
    data = build()
    data["demo"] = demo
    with open(os.path.join(HERE, "pairs_v2.json"), "w") as f:
        json.dump(data, f, indent=1)
    ranked = sorted(data["instruments"], key=lambda i: -i["composite"])
    print("Most bullish:", ", ".join("%s %+.0f" % (i["name"], i["composite"]) for i in ranked[:3]))
    print("Most bearish:", ", ".join("%s %+.0f" % (i["name"], i["composite"]) for i in ranked[-3:]))
    print("%d instruments scored" % len(ranked))
    if fb.ERRORS:
        print("Warnings:", *fb.ERRORS, sep="\n  ")
