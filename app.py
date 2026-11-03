import os, math, time, json, codecs, threading, datetime as dt
import requests, pyotp
from flask import Flask, jsonify, render_template_string, request
from SmartApi import SmartConnect

API_KEY = os.environ["API_KEY"]
CLIENT_ID = os.environ["CLIENT_ID"]
PIN = os.environ["PIN"]
TOTP_SECRET = os.environ["TOTP_SECRET"]
WIDTH = 7  # strikes each side of ATM

SCRIP_URLS = [
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
]
# name: (spot exchange, spot token, strike step, option exchange)
INDEX = {
    "NIFTY": ("NSE", "99926000", 50, "NFO"),
    "BANKNIFTY": ("NSE", "99926009", 100, "NFO"),
    "SENSEX": ("BSE", "99919000", 100, "BFO"),
}

import collections
VER = "v15"
BOOT = time.strftime("%H:%M:%S")
EVENTS = collections.deque(maxlen=14)


def ev(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    EVENTS.append(line)
    print("EV", line, flush=True)


app = Flask(__name__)
lock = threading.Lock()
scrip_lock = threading.Lock()
state = {"api": None, "scrip": None, "scrip_day": None, "cache": {},
         "stocks": {}, "lv": {}, "lv_day": None, "tf": {}, "tf_t": 0, "prog": "not started",
         "scan": {"rows": [], "time": None, "ready": 0, "total": 0, "err": None, "closed": False}}


# ---------- login & data ----------
def login():
    api = SmartConnect(api_key=API_KEY)
    r = api.generateSession(CLIENT_ID, PIN, pyotp.TOTP(TOTP_SECRET).now())
    if not r.get("status"):
        raise RuntimeError("Login failed: " + str(r.get("message")))
    state["api"] = api


def _stream(url):
    """Stream the big instrument file object by object (low memory, no long GIL hold)."""
    dec = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")("ignore")
    buf, pos = "", 0
    t0, got = time.time(), 0
    state["prog"] = "connecting to " + url.split("/")[2]
    ev("download start " + url.split("/")[2])
    with requests.get(url, stream=True, timeout=(10, 40)) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=1 << 18):
            got += len(chunk)
            state["prog"] = "downloaded %.1f MB, %ds" % (got / 1e6, time.time() - t0)
            if time.time() - t0 > 420:
                raise TimeoutError("instrument list too slow (>7 min)")
            buf = buf[pos:] + utf8.decode(chunk)
            pos = 0
            while True:
                while pos < len(buf) and buf[pos] in " \r\n\t,[":
                    pos += 1
                if pos >= len(buf) or buf[pos] == "]":
                    break
                try:
                    obj, end = dec.raw_decode(buf, pos)
                except ValueError:
                    break
                yield obj
                pos = end


def iter_scrip():
    """Try each source in a helper thread, so a hung connection can never freeze us."""
    import queue
    DONE = object()
    err = None
    for url in SCRIP_URLS:
        host = url.split("/")[2]
        q, stop = queue.Queue(maxsize=2000), threading.Event()

        def worker(url=url, q=q, stop=stop):
            def put(x):
                while not stop.is_set():
                    try:
                        q.put(x, timeout=1)
                        return
                    except queue.Full:
                        pass
            try:
                for obj in _stream(url):
                    put(obj)
                put(DONE)
            except Exception as e:
                put(e)

        threading.Thread(target=worker, daemon=True).start()
        got = False
        try:
            while True:
                try:
                    item = q.get(timeout=90 if got else 45)
                except queue.Empty:
                    raise TimeoutError("no data from %s" % host)
                if item is DONE:
                    return
                if isinstance(item, Exception):
                    raise item
                got = True
                yield item
        except Exception as e:
            err = e
            stop.set()
            state["prog"] = "%s failed: %s %s" % (host, type(e).__name__, str(e)[:60])
            ev("source failed %s: %r" % (host, e))
            if got:
                raise
    raise err


def scrip_master():
    with scrip_lock:
        today = dt.date.today()
        if state["scrip"] is None or state["scrip_day"] != today:
            opt, fno, eq = [], set(), {}
            for x in iter_scrip():
                it, seg = x.get("instrumenttype"), x.get("exch_seg")
                if it == "OPTIDX":
                    if x.get("name") in INDEX:
                        opt.append({k: x.get(k) for k in ("name", "expiry", "strike", "symbol", "token", "exch_seg")})
                elif it == "OPTSTK" and seg == "NFO":
                    fno.add(x.get("name"))
                elif seg == "NSE" and str(x.get("symbol", "")).endswith("-EQ"):
                    eq[x.get("name")] = x.get("token")
            state["stocks"] = {n: t for n, t in eq.items() if n in fno}
            state["scrip"] = opt
            state["scrip_day"] = today
            state["prog"] = "list ready"
            ev("list ready: %d index options, %d fno stocks" % (len(opt), len(state["stocks"])))
        return state["scrip"]


def parse_exp(s):
    return dt.datetime.strptime(s.title(), "%d%b%Y")


def market(exch_tokens):
    out = []
    for seg, toks in exch_tokens.items():
        for i in range(0, len(toks), 50):
            for attempt in (1, 2):
                if state["api"] is None:
                    login()
                r = state["api"].getMarketData("FULL", {seg: toks[i:i + 50]})
                time.sleep(0.3)
                if r.get("status"):
                    out += r["data"]["fetched"]
                    break
                login()
    return out


# ---------- maths (Black-76) ----------
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def b76(call, F, K, T, v):
    sd = v * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    return F * ncdf(d1) - K * ncdf(d2) if call else K * ncdf(-d2) - F * ncdf(-d1)


def iv_of(call, px, F, K, T):
    if not px or px <= 0 or T <= 0:
        return None
    intrinsic = max(F - K, 0) if call else max(K - F, 0)
    if px <= intrinsic + 0.01 or b76(call, F, K, T, 4.0) < px:
        return None
    lo, hi = 0.005, 4.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if b76(call, F, K, T, mid) > px:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def gamma(F, K, T, v):
    sd = v * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    return npdf(d1) / (F * sd)


# ---------- build dashboard ----------
def build(name):
    sx, stok, step, ox = INDEX[name]
    spot_r = market({sx: [stok]})
    S = float(spot_r[0]["ltp"])
    now = dt.datetime.now()  # server should run in IST (set TZ=Asia/Kolkata)

    rows = [x for x in scrip_master() if x["name"] == name and x["exch_seg"] == ox]
    exps = sorted({parse_exp(x["expiry"]) for x in rows})
    exps = [e.replace(hour=15, minute=30) for e in exps]
    exps = [e for e in exps if e > now]
    e1 = exps[0]
    e1s = e1.strftime("%d%b%Y").upper()
    T = max((e1 - now).total_seconds(), 60) / (365 * 86400)

    atm = round(S / step) * step
    want = {atm + i * step for i in range(-WIDTH, WIDTH + 1)}
    meta = {}  # token -> (strike, 'CE'/'PE')
    for x in rows:
        if x["expiry"].upper() != e1s:
            continue
        k = float(x["strike"]) / 100
        if k in want:
            meta[x["token"]] = (k, x["symbol"][-2:])
    quotes = market({ox: list(meta)})

    ks = sorted(want)
    ch = {k: {"CE": {}, "PE": {}} for k in ks}
    for q in quotes:
        k, t = meta[q["symbolToken"]]
        ch[k][t] = {"ltp": q.get("ltp"), "oi": q.get("opnInterest") or 0, "vol": q.get("tradeVolume") or 0}

    cA, pA = ch[atm]["CE"].get("ltp"), ch[atm]["PE"].get("ltp")
    F = atm + cA - pA if cA and pA else S
    ivs = [v for v in (iv_of(True, cA, F, atm, T), iv_of(False, pA, F, atm, T)) if v]
    atm_iv = sum(ivs) / len(ivs) if ivs else None
    straddle = cA + pA if cA and pA else None

    # per-strike IV (OTM option), GEX, walls
    ivk, gex, cw, pw = {}, {}, (0, None), (0, None)
    for k in ks:
        call = k >= F
        side = "CE" if call else "PE"
        v = iv_of(call, ch[k][side].get("ltp"), F, k, T)
        ivk[k] = v
        if v:
            g1 = gamma(F, k, T, v) * S * S * 0.01
            co, po = ch[k]["CE"].get("oi", 0), ch[k]["PE"].get("oi", 0)
            gex[k] = g1 * (co - po)
            if g1 * co > cw[0]: cw = (g1 * co, k)
            if g1 * po > pw[0]: pw = (g1 * po, k)
    net = sum(gex.values())

    # gamma flip: re-price GEX at different spots
    def net_at(Sx):
        Fx = F * Sx / S
        t = 0
        for k in ks:
            v = ivk.get(k)
            if v:
                t += gamma(Fx, k, T, v) * (ch[k]["CE"].get("oi", 0) - ch[k]["PE"].get("oi", 0)) * Sx * Sx * 0.01
        return t
    flip, px, pg = None, None, None
    n = 8 * WIDTH
    for q in range(n + 1):
        sx_ = ks[0] + (ks[-1] - ks[0]) * q / n
        g = net_at(sx_)
        if pg and g != 0 and (g > 0) != (pg > 0):
            cand = px + (sx_ - px) * (0 - pg) / (g - pg)
            if flip is None or abs(cand - S) < abs(flip - S):
                flip = cand
        px, pg = sx_, g

    # max pain
    best, mp = None, None
    for a in ks:
        pain = sum(ch[b]["CE"].get("oi", 0) * max(0, a - b) + ch[b]["PE"].get("oi", 0) * max(0, b - a) for b in ks)
        if best is None or pain < best:
            best, mp = pain, a

    def pcr(sel, key):
        c = sum(ch[k]["CE"].get(key, 0) for k in sel)
        p = sum(ch[k]["PE"].get(key, 0) for k in sel)
        return p / c if c else None
    near = [k for k in ks if abs(k - atm) <= 3 * step]
    oi_res = max(ks, key=lambda k: ch[k]["CE"].get("oi", 0))
    oi_sup = max(ks, key=lambda k: ch[k]["PE"].get("oi", 0))

    # ---- bias score (decision support only) ----
    reasons, score = [], 0
    def vote(v, txt):
        nonlocal score
        score += v
        reasons.append([txt, v])
    p3 = pcr(near, "oi")
    if p3 is not None:
        vote(1 if p3 >= 1.2 else -1 if p3 <= 0.8 else 0, "PCR ATM±3 %.2f" % p3)
    bp = (F - S) / S * 100
    vote(1 if bp > 0.05 else -1 if bp < -0.05 else 0, "Basis %+.2f%%" % bp)
    rng_ = oi_res - oi_sup
    if rng_ > 0:
        pos = (S - oi_sup) / rng_
        vote(1 if pos < 0.3 else -1 if pos > 0.7 else 0, "Spot in OI range %d%%" % round(pos * 100))
    mpd = (S - mp) / S * 100
    vote(-1 if mpd > 0.3 else 1 if mpd < -0.3 else 0, "Vs max pain %+.2f%%" % mpd)
    pv = pcr(ks, "vol")
    if pv is not None:
        vote(1 if pv >= 1.2 else -1 if pv <= 0.8 else 0, "Volume PCR %.2f" % pv)
    label = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"
    closed = now.weekday() >= 5 or not (dt.time(9, 15) <= now.time() <= dt.time(15, 30))
    regime = "Trending (moves extend)" if net < 0 else "Pinning (moves fade)"

    em_iv = F * atm_iv * math.sqrt(T) if atm_iv else None
    em_st = straddle * 1.25 if straddle else None
    return {
        "name": name, "time": now.strftime("%H:%M:%S"), "expiry": e1.strftime("%d %b"),
        "days_left": round((e1 - now).total_seconds() / 86400, 2),
        "spot": S, "atm": atm, "step": step, "call": cA, "put": pA, "synthetic": F,
        "basis": F - S, "atm_iv": atm_iv, "straddle": straddle,
        "em_iv": em_iv, "em_straddle": em_st,
        "pcr_oi_all": pcr(ks, "oi"), "pcr_oi_near": pcr(near, "oi"),
        "pcr_vol_all": pcr(ks, "vol"),
        "net_gex_cr": net / 1e7, "flip": flip, "call_wall": cw[1], "put_wall": pw[1],
        "max_pain": mp, "oi_support": oi_sup, "oi_resist": oi_res,
        "gex": [{"k": k, "g": gex.get(k, 0) / 1e7} for k in ks],
        "bias": {"label": label, "score": score, "reasons": reasons, "regime": regime, "closed": closed},
    }


# ---------- key levels + scanner ----------
def market_closed(now=None):
    now = now or dt.datetime.now()
    return now.weekday() >= 5 or not (dt.time(9, 15) <= now.time() <= dt.time(15, 30))


# ---------- HARSI (Heikin-Ashi RSI), port of the HARSI Pine indicator ----------
def _rsi(src, n):
    out = [None] * len(src)
    if len(src) <= n:
        return out
    up = [0.0] * len(src)
    dn = [0.0] * len(src)
    for i in range(1, len(src)):
        ch = src[i] - src[i - 1]
        up[i], dn[i] = max(ch, 0.0), max(-ch, 0.0)
    au, ad = sum(up[1:n + 1]) / n, sum(dn[1:n + 1]) / n

    def val(a, b):
        return 100.0 if b == 0 else 0.0 if a == 0 else 100 - 100 / (1 + a / b)
    out[n] = val(au, ad)
    for i in range(n + 1, len(src)):
        au, ad = (au * (n - 1) + up[i]) / n, (ad * (n - 1) + dn[i]) / n
        out[i] = val(au, ad)
    return out


def _roll(x, n, fn):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if None not in w:
            out[i] = fn(w)
    return out


def _wma(x, n):
    tot = n * (n + 1) / 2
    return _roll(x, n, lambda w: sum(v * (j + 1) for j, v in enumerate(w)) / tot)


def _sma(x, n):
    return _roll(x, n, lambda w: sum(w) / n)


def _stoch(x, n):
    def f(w):
        lo, hi = min(w), max(w)
        return None if hi == lo else 100 * (w[-1] - lo) / (hi - lo)
    return _roll(x, n, f)


def harsi_series(c, ha_len=14, smooth=7, rsi_len=7, scale=0.8):
    """Per candle: HARSI candle (o,h,l,c), smoothed RSI (r), stochastic K/D. None where not ready."""
    cols = [[x[i] for x in c] for i in (1, 2, 3, 4)]
    ro, rh, rl, rc = [[None if v is None else v - 50 for v in _rsi(col, ha_len)] for col in cols]
    src = [(x[1] + x[2] + x[3] + x[4]) / 4 for x in c]
    base = _rsi(src, rsi_len)
    rv = _wma([None if v is None else v - 50 for v in base], 5)
    k = _sma(_stoch(base, 14), 3)
    d = _sma(k, 3)
    rd = lambda v: None if v is None else round(v, 2)
    out, ho_p, hc_p = [], None, None
    for i in range(len(c)):
        it = {"o": None, "h": None, "l": None, "c": None, "r": rd(rv[i]),
              "k": rd(None if k[i] is None else (k[i] - 50) * scale),
              "d": rd(None if d[i] is None else (d[i] - 50) * scale)}
        if None not in (ro[i], rh[i], rl[i], rc[i]):
            hcl = (ro[i] + rh[i] + rl[i] + rc[i]) / 4
            hop = (ro[i] + rc[i]) / 2 if ho_p is None else (ho_p * smooth + hc_p) / (smooth + 1)
            it.update(o=rd(hop), h=rd(max(rh[i], hop, hcl)), l=rd(min(rl[i], hop, hcl)), c=rd(hcl))
            ho_p, hc_p = hop, hcl
        out.append(it)
    return out


def harsi_summary(c):
    try:
        last = harsi_series(c)[-1]
    except Exception:
        return None
    if last["c"] is None or last["r"] is None:
        return None
    r = last["r"]
    zone = "OB ext" if r >= 40 else "OB" if r >= 20 else "OS ext" if r <= -40 else "OS" if r <= -20 else "mid"
    return {"bull": last["c"] >= last["o"], "rsi": r, "zone": zone}


def key_levels(c, L=6, M=10, N=4, tolp=0.006):
    """Approximation of the Key Levels indicator: pivot highs/lows (length L),
    last M pivots, clustered into N levels, strength = share of pivots."""
    H = [x[2] for x in c]
    Lo = [x[3] for x in c]
    piv = []
    for i in range(L, len(c) - L):
        if H[i] == max(H[i - L:i + L + 1]):
            piv.append((i, H[i]))
        if Lo[i] == min(Lo[i - L:i + L + 1]):
            piv.append((i, Lo[i]))
    piv = sorted(piv)[-M:]
    if not piv:
        return []
    tol = c[-1][4] * tolp
    prices = sorted(p[1] for p in piv)
    cl = [[prices[0]]]
    for p in prices[1:]:
        if p - sum(cl[-1]) / len(cl[-1]) <= tol:
            cl[-1].append(p)
        else:
            cl.append([p])
    cl.sort(key=len, reverse=True)
    out = [{"p": sum(x) / len(x), "s": round(len(x) / len(piv) * 100)} for x in cl[:N]]
    return sorted(out, key=lambda x: x["p"])


def get_signal(q, levels, k=1.0):
    """k scales the distance thresholds: 1.0 daily, smaller for intraday candles."""
    ltp, op, hi, lo, pc = (q.get(x) for x in ("ltp", "open", "high", "low", "close"))
    if not all([ltp, op, hi, lo, pc]):
        return None
    best = None
    for lv in levels:
        p = lv["p"]
        kind = d = None
        if pc <= p and ltp >= p * (1 + 0.002 * k) and ltp > op:
            kind, d = "Breakout", "CALL"
        elif pc >= p and ltp <= p * (1 - 0.002 * k) and ltp < op:
            kind, d = "Breakdown", "PUT"
        elif p < ltp and p * (1 - 0.01 * k) <= lo <= p * (1 + 0.003 * k) and ltp >= p * (1 + 0.004 * k) and ltp > op:
            kind, d = "Support bounce", "CALL"
        elif p > ltp and p * (1 - 0.003 * k) <= hi <= p * (1 + 0.01 * k) and ltp <= p * (1 - 0.004 * k) and ltp < op:
            kind, d = "Resistance reject", "PUT"
        if kind and (best is None or lv["s"] > best["strength"]):
            best = {"dir": d, "kind": kind, "level": p, "strength": lv["s"]}
    return best


def candles(ex, tok, interval="ONE_DAY", days=220, keep_today=False):
    now = dt.datetime.now()
    params = {"exchange": ex, "symboltoken": tok, "interval": interval,
              "fromdate": (now - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
              "todate": now.strftime("%Y-%m-%d %H:%M")}
    for _ in range(3):
        with lock:
            if state["api"] is None:
                login()
            r = state["api"].getCandleData(params)
        if r.get("status") and r.get("data"):
            if keep_today:
                return r["data"]
            today = now.date().isoformat()
            return [x for x in r["data"] if x[0][:10] != today]
        if "token" in str(r.get("message", "")).lower():
            state["api"] = None
        time.sleep(1.5)
    return None


# timeframe -> (Angel interval, lookback days, level clustering %, signal scale)
TFS = {"15m": ("FIFTEEN_MINUTE", 15, 0.0015, 0.25), "1h": ("ONE_HOUR", 60, 0.003, 0.5)}


def refresh_tf():
    """Intraday key levels for the 3 indices (15 min and 1 hour candles)."""
    for n, v in INDEX.items():
        for key, (iv, days, tolp, k) in TFS.items():
            c = candles(v[0], v[1], iv, days, keep_today=True)
            time.sleep(0.5)
            if not c or len(c) < 40:
                continue
            hist = c[:-1]
            state["tf"].setdefault(n, {})[key] = {"levels": key_levels(hist, tolp=tolp),
                                                  "pc": hist[-1][4], "cur": c[-1], "k": k,
                                                  "harsi": harsi_summary(c)}
    state["tf_t"] = time.time()
    ev("intraday levels refreshed")


def live_scan():
    lv = state["lv"]
    groups, names = {}, {}
    for n, v in lv.items():
        groups.setdefault(v["ex"], []).append(v["tok"])
        names[v["tok"]] = n
    with lock:
        quotes = market(groups)
    rows = []
    for q in quotes:
        n = names.get(q.get("symbolToken"))
        if not n or not q.get("ltp"):
            continue
        v = lv[n]
        ltp = float(q["ltp"])
        pc = q.get("close") or v["pc"]
        below = [x["p"] for x in v["levels"] if x["p"] < ltp]
        above = [x["p"] for x in v["levels"] if x["p"] >= ltp]
        sig = get_signal(q, v["levels"])
        row = {"name": n, "idx": n in INDEX, "ltp": ltp,
               "chg": (ltp - pc) / pc * 100 if pc else 0, "sig": sig,
               "sup": max(below) if below else None, "res": min(above) if above else None}
        if n in INDEX:
            tfs = {"1d": sig}
            for key in TFS:
                t = state["tf"].get(n, {}).get(key)
                if t:
                    cur = t["cur"]
                    q2 = {"ltp": ltp, "open": cur[1], "high": max(cur[2], ltp),
                          "low": min(cur[3], ltp), "close": t["pc"]}
                    tfs[key] = get_signal(q2, t["levels"], t["k"])
                else:
                    tfs[key] = None
            row["tfsig"] = tfs
            lvmap = {"1d": v["levels"]}
            for key in TFS:
                t = state["tf"].get(n, {}).get(key)
                if t:
                    lvmap[key] = t["levels"]
            row["tflv"] = {}
            for key, lvs in lvmap.items():
                dn = [x["p"] for x in lvs if x["p"] < ltp]
                up = [x["p"] for x in lvs if x["p"] >= ltp]
                row["tflv"][key] = {"sup": max(dn) if dn else None, "res": min(up) if up else None, "n": len(lvs)}
        rows.append(row)
    rows.sort(key=lambda r: (not r["idx"], -(r["sig"]["strength"] if r["sig"] else 0)))
    state["scan"] = {"rows": rows, "time": dt.datetime.now().strftime("%H:%M:%S"),
                     "ready": len(lv), "total": state["scan"].get("total", 0),
                     "err": None, "closed": market_closed()}


def scan_loop():
    ev("scan thread started pid=%d" % os.getpid())
    while True:
        try:
            now = dt.datetime.now()
            if state["lv_day"] != now.date():
                scrip_master()
                uni = {n: (v[0], v[1]) for n, v in INDEX.items()}
                for n, t in state["stocks"].items():
                    uni[n] = ("NSE", t)
                state["scan"]["total"] = len(uni)
                ev("levels: start %d instruments" % len(uni))
                for n, (ex, tok) in uni.items():
                    if n in state["lv"] and state["lv"][n].get("day") == now.date():
                        continue
                    c = candles(ex, tok)
                    if c and len(c) > 30:
                        state["lv"][n] = {"ex": ex, "tok": tok, "levels": key_levels(c),
                                          "pc": c[-1][4], "day": now.date(),
                                          "harsi": harsi_summary(c)}
                    time.sleep(0.5)
                    if len(state["lv"]) > len(INDEX) and len(state["lv"]) % 25 == 0:
                        live_scan()  # show stocks progressively while the rest load
                    if n == list(INDEX)[-1]:
                        refresh_tf()
                        live_scan()
                ev("levels: done %d/%d" % (len(state["lv"]), len(uni)))
                if len(state["lv"]) > 0.8 * len(uni):
                    state["lv_day"] = now.date()
            if state["lv"]:
                if not state["tf"] or (not market_closed() and time.time() - state["tf_t"] > 60):
                    refresh_tf()
                live_scan()
        except Exception as e:
            state["scan"]["err"] = str(e)[:200]
            ev("scan_loop error: %r" % (e,))
            state["api"] = None
        time.sleep(20)


def final_signal(name, bias, tf="1d"):
    row = next((r for r in state["scan"]["rows"] if r["name"] == name), None)
    kl = (row.get("tfsig") or {}).get(tf) if row else None
    if not kl:
        return {"text": "WAIT", "why": "No key-level signal yet"}
    want = "BULLISH" if kl["dir"] == "CALL" else "BEARISH"
    desc = "%s @%d (%d%%)" % (kl["kind"], kl["level"], kl["strength"])
    if bias["label"] == want:
        return {"text": "BUY " + kl["dir"], "why": desc + " + bias " + bias["label"]}
    if bias["label"] == "NEUTRAL":
        return {"text": "WEAK " + kl["dir"], "why": desc + ", bias neutral"}
    return {"text": "WAIT", "why": desc + " vs bias " + bias["label"]}


_scan_pid = None
_start_lock = threading.Lock()


def ensure_scanner():
    """Start the scanner thread inside the process that actually serves requests
    (a thread started before gunicorn forks its worker would silently vanish)."""
    global _scan_pid
    if _scan_pid != os.getpid():
        with _start_lock:
            if _scan_pid != os.getpid():
                _scan_pid = os.getpid()
                threading.Thread(target=scan_loop, daemon=True).start()


@app.before_request
def _boot_scanner():
    ensure_scanner()


@app.route("/api/scan")
def api_scan():
    return jsonify(dict(state["scan"], prog=state["prog"], ver=VER, boot=BOOT, pid=os.getpid(), events=list(EVENTS)))


def with_signals(name, d):
    sg = {tf: final_signal(name, d["bias"], tf) for tf in ("15m", "1h", "1d")}
    row = next((r for r in state["scan"]["rows"] if r["name"] == name), None)
    hs = {"1d": (state["lv"].get(name) or {}).get("harsi")}
    for key in TFS:
        hs[key] = (state["tf"].get(name, {}).get(key) or {}).get("harsi")
    return dict(d, signals=sg, klv=(row or {}).get("tflv", {}), harsi=hs)


@app.route("/api/<name>")
def api(name):
    name = name.upper()
    if name == "SCAN":
        return jsonify(dict(state["scan"], prog=state["prog"], ver=VER, boot=BOOT, pid=os.getpid(), events=list(EVENTS)))
    if name not in INDEX:
        return jsonify(error="unknown index"), 404
    if state["scrip"] is None:
        return jsonify(error="Starting up: loading instrument list (" + state["prog"] + "). " + (state["scan"].get("err") or "Wait, it refreshes by itself.")), 503
    with lock:
        c = state["cache"].get(name)
        if c and time.time() - c[0] < 3:
            return jsonify(with_signals(name, c[1]))
        try:
            d = build(name)
        except Exception as e:
            state["api"] = None
            return jsonify(error=str(e)), 500
        state["cache"][name] = (time.time(), d)
        return jsonify(with_signals(name, d))


CHART_TF = {"15m": ("FIFTEEN_MINUTE", 10), "1h": ("ONE_HOUR", 45), "1d": ("ONE_DAY", 260)}
CHART_CACHE = {}


@app.route("/api/chart/<name>")
def api_chart(name):
    tf = request.args.get("tf", "1d")
    v = state["lv"].get(name) or state["lv"].get(name.upper())
    if tf not in CHART_TF or not v:
        return jsonify(error="no data for %s yet (levels still loading?)" % name), 404
    key = (name, tf)
    hit = CHART_CACHE.get(key)
    if hit and time.time() - hit[0] < 20:
        return jsonify(hit[1])
    iv, days = CHART_TF[tf]
    c = candles(v["ex"], v["tok"], iv, days, keep_today=True)
    if not c:
        return jsonify(error="candles not available"), 502
    if tf == "1d":
        lvs = v["levels"]
    elif state["tf"].get(name, {}).get(tf):
        lvs = state["tf"][name][tf]["levels"]
    else:
        lvs = key_levels(c[:-1], tolp=TFS[tf][2])
    out = []
    for x in c:
        t = int(dt.datetime.fromisoformat(x[0]).timestamp()) + 19800  # show IST on a UTC axis
        out.append({"time": t, "open": x[1], "high": x[2], "low": x[3], "close": x[4]})
    data = {"name": name, "tf": tf, "candles": out, "levels": lvs, "last": out[-1]["close"],
            "harsi": harsi_series(c)}
    CHART_CACHE[key] = (time.time(), data)
    return jsonify(data)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes"><title>Options Desk</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#f1f3f8;--card:#fff;--tx:#0b0e14;--mu:#5b6275;--up:#12915f;--dn:#e03131}
@media(prefers-color-scheme:dark){:root{--bg:#0d1016;--card:#161a23;--tx:#f2f3f8;--mu:#9aa0b4}}
body{margin:0;background:var(--bg);color:var(--tx);font:14px system-ui,sans-serif;padding:8px}
.tabs{display:flex;gap:6px;margin-bottom:8px}
.tabs button{flex:1;padding:10px;border:0;border-radius:8px;background:var(--card);color:var(--tx);font-weight:600}
.tabs button.on{background:#0874b0;color:#fff}
.card{background:var(--card);border-radius:10px;margin-bottom:8px;overflow:hidden}
.card h3{margin:0;padding:8px 12px;font-size:13px;color:#fff}
.r{display:flex;justify-content:space-between;padding:6px 12px;border-top:1px solid #8882}
.r span:first-child{color:var(--mu)}.up{color:var(--up)}.dn{color:var(--dn)}
#st{color:var(--mu);font-size:12px;margin:4px 2px}
#cv{display:none;position:fixed;inset:0;background:var(--bg);z-index:9}
#cvh{display:flex;gap:6px;padding:8px;align-items:center}
#cvh button{padding:8px 12px;border:0;border-radius:8px;background:var(--card);color:var(--tx);font-weight:600}
#cvh button.on{background:#0874b0;color:#fff}
#cvc{position:absolute;top:52px;bottom:34px;left:0;right:0;display:flex;flex-direction:column}
#cvp{flex:1.25;min-height:0}#cvr{flex:1;min-height:0}
#cvs{position:absolute;bottom:0;left:0;right:0;height:34px;color:var(--mu);font-size:11px;padding:4px 8px;overflow:hidden}
</style></head><body>
<div id="cv"><div id="cvh"><b id="cvt" style="flex:1"></b>
<button id="b15m" onclick="cvTf('15m')">15m</button><button id="b1h" onclick="cvTf('1h')">1H</button><button id="b1d" onclick="cvTf('1d')">1D</button>
<button id="bst" onclick="cvSt()">Stoch</button><button onclick="closeChart()">&#10005;</button></div><div id="cvc"><div id="cvp"></div><div id="cvr"></div></div><div id="cvs"></div></div>
<div class="tabs" id="tabs"></div><div id="st">Loading...</div><div id="out"></div>
<script>
let cur="NIFTY";const N=["NIFTY","BANKNIFTY","SENSEX","SCAN"];
const T=document.getElementById("tabs");
N.forEach(n=>{const b=document.createElement("button");b.textContent=n;b.onclick=()=>{cur=n;load()};b.id="t"+n;T.appendChild(b)});
const f=(x,d=1)=>x==null?"-":Number(x).toLocaleString("en-IN",{maximumFractionDigits:d,minimumFractionDigits:d});
const pc=x=>x==null?"-":(x*100).toFixed(1)+"%";
const R=(a,b,c="",n="")=>`<div class="r"${n?` onclick="openChart(this.dataset.n)" data-n="${n.replace(/&/g,"&amp;")}" style="cursor:pointer"`:""}><span>${a}</span><span class="${c}">${b}</span></div>`;
const C=(t,col,rows)=>`<div class="card"><h3 style="background:${col}">${t}</h3>${rows.join("")}</div>`;
function draw(d){
 const pcrc=x=>x==null?"":x>=1.2?"up":x<=0.8?"dn":"";
 const em=(c,m)=>m==null?"-":`${f(c-m,0)} to ${f(c+m,0)}`;
 let mx=Math.max(...d.gex.map(g=>Math.abs(g.g)),1e-9);
 const bars=d.gex.map(g=>`<div class="r"><span>${f(g.k,0)}</span><span style="flex:1;margin:0 8px;display:flex;align-items:center"><i style="display:block;height:8px;width:${Math.abs(g.g)/mx*100}%;background:${g.g>=0?"var(--up)":"var(--dn)"}"></i></span><span class="${g.g>=0?"up":"dn"}">${f(g.g,0)}</span></div>`).join("");
 out.innerHTML=
 C("SINGLE SIGNAL","#0b0e14",[["15m","15 Min"],["1h","1 Hour"],["1d","1 Day"]].map(x=>{const g=d.signals[x[0]];const c=g.text.startsWith("BUY CALL")?"up":g.text.startsWith("BUY PUT")?"dn":"";return R(x[1],g.text,c)+R("&nbsp;&nbsp;why",g.why)}))+
 C("KEY LEVELS (support | resistance)","#334155",[["15m","15 Min"],["1h","1 Hour"],["1d","1 Day"]].map(x=>{const k=(d.klv||{})[x[0]];return R(x[1],k?`${f(k.sup,0)} | ${f(k.res,0)}`:"loading...")}))+
 C("HARSI (candle colour | RSI)","#008b7f",[["15m","15 Min"],["1h","1 Hour"],["1d","1 Day"]].map(x=>{const g=(d.harsi||{})[x[0]];return g?R(x[1],(g.bull?"Bullish":"Bearish")+" | RSI "+(g.rsi>=0?"+":"")+f(g.rsi,1)+" "+g.zone,g.bull?"up":"dn"):R(x[1],"loading...")}))+
 C("BIAS SCORE","#334155",[R("Bias",d.bias.label+" ("+(d.bias.score>0?"+":"")+d.bias.score+")",d.bias.score>=2?"up":d.bias.score<=-2?"dn":""),R("Regime",d.bias.regime),...d.bias.reasons.map(x=>R(x[0],x[1]>0?"+1":x[1]<0?"-1":"0",x[1]>0?"up":x[1]<0?"dn":"")),d.bias.closed?R("Warning","Market closed: stale data","dn"):R("Note","Not advice. Use with price action.")])+
 C(`${d.name} | Spot ${f(d.spot,2)}`,"#0874b0",[R("ATM",f(d.atm,0)),R("Expiry",d.expiry+" ("+d.days_left+"d)"),R("Updated",d.time),R("Chart with key levels","Open &rsaquo;","up",d.name)])+
 C("SPOT vs SYNTHETIC","#0ea5e9",[R("ATM call",f(d.call,2)),R("ATM put",f(d.put,2)),R("Synthetic",f(d.synthetic,1)),R("Basis",(d.basis>=0?"+":"")+f(d.basis,1),d.basis>=0?"up":"dn")])+
 C("EXPECTED MOVE","#7c5ce0",[R("ATM IV",pc(d.atm_iv)),R("Straddle",f(d.straddle,2)),R("IV 1σ",em(d.spot,d.em_iv)),R("Straddle x1.25",em(d.spot,d.em_straddle))])+
 C("PCR","#0f9f8f",[R("OI whole (loaded)",f(d.pcr_oi_all,2),pcrc(d.pcr_oi_all)),R("OI ATM ±3",f(d.pcr_oi_near,2),pcrc(d.pcr_oi_near)),R("Volume",f(d.pcr_vol_all,2),pcrc(d.pcr_vol_all))])+
 C("GAMMA & MAX PAIN","#ea6a12",[R("Net GEX ₹cr/1%",f(d.net_gex_cr,0),d.net_gex_cr>=0?"up":"dn"),R("Mode",d.net_gex_cr>=0?"Pinning":"Trending"),R("Gamma flip",f(d.flip,0)),R("Call wall",f(d.call_wall,0),"dn"),R("Put wall",f(d.put_wall,0),"up"),R("Max pain",f(d.max_pain,0)),R("OI support | resist",f(d.oi_support,0)+" | "+f(d.oi_resist,0))])+
 C("NET GEX BY STRIKE","#16a34a",[bars]);
}
function drawScan(s){
 const rows=s.rows||[];const calls=rows.filter(r=>r.sig&&r.sig.dir=="CALL"),puts=rows.filter(r=>r.sig&&r.sig.dir=="PUT");
 const line=r=>R(`${r.name}  ${f(r.ltp,2)} (${r.chg>=0?"+":""}${f(r.chg,2)}%)`,`${r.sig.kind} @${f(r.sig.level,0)} (${r.sig.strength}%)`,r.sig.dir=="CALL"?"up":"dn",r.name);
 const mk=(t,col,a)=>C(t+" ("+a.length+")",col,a.length?a.map(line):[R("None right now","-")]);
 const t=x=>x?x.dir:"-";const idx=rows.filter(r=>r.idx).map(r=>R(`${r.name} ${f(r.ltp,2)}`,`15m ${t(r.tfsig&&r.tfsig["15m"])} | 1H ${t(r.tfsig&&r.tfsig["1h"])} | 1D ${t(r.tfsig&&r.tfsig["1d"])}`,"",r.name));
 out.innerHTML=C("SCANNER STATUS","#334155",[R("Levels ready",(s.ready||0)+"/"+(s.total||0)),R("Updated",s.time||"loading..."),R("Progress",s.prog||"-"),s.closed?R("Warning","Market closed: stale","dn"):R("Note","Indices: 15m/1H/1D. Stocks: 1D only"),s.err?R("Error",s.err,"dn"):""])+
 C("INDICES","#0874b0",idx.length?idx:[R("Loading levels, wait 2-3 min","-")])+mk("BUY CALL SIGNALS (daily)","#12915f",calls)+mk("BUY PUT SIGNALS (daily)","#e03131",puts);
}
let CH=null,CH2=null,cvName=null,cvTfv="1d",stOn=false,STS=[];
function openChart(n){cvName=n;document.getElementById("cv").style.display="block";document.getElementById("cvt").textContent=n;loadChart()}
function killCharts(){if(CH){CH.remove();CH=null}if(CH2){CH2.remove();CH2=null}}
function closeChart(){document.getElementById("cv").style.display="none";killCharts()}
function cvTf(t){cvTfv=t;loadChart()}
function cvSt(){stOn=!stOn;document.getElementById("bst").className=stOn?"on":"";STS.forEach(x=>x.applyOptions({visible:stOn}))}
async function loadChart(){
 const S=document.getElementById("cvs"),P=document.getElementById("cvp"),Q=document.getElementById("cvr");
 ["15m","1h","1d"].forEach(t=>document.getElementById("b"+t).className=t==cvTfv?"on":"");
 S.textContent="Loading chart...";
 try{
  const r=await fetch("/api/chart/"+encodeURIComponent(cvName)+"?tf="+cvTfv);const d=await r.json();
  if(d.error){S.textContent="Error: "+d.error;return}
  if(typeof LightweightCharts==="undefined"){S.textContent="Chart library did not load (check internet)";return}
  killCharts();
  const dk=matchMedia("(prefers-color-scheme: dark)").matches;
  const base={layout:{background:{color:dk?"#0d1016":"#f1f3f8"},textColor:dk?"#9aa0b4":"#5b6275"},grid:{vertLines:{color:"#8883"},horzLines:{color:"#8883"}},timeScale:{timeVisible:cvTfv!="1d",borderColor:"#8885"},rightPriceScale:{borderColor:"#8885",minimumWidth:64}};
  CH=LightweightCharts.createChart(P,Object.assign({width:P.clientWidth,height:P.clientHeight},base));
  CH2=LightweightCharts.createChart(Q,Object.assign({width:Q.clientWidth,height:Q.clientHeight},base));
  const cs=CH.addCandlestickSeries({upColor:"#12915f",downColor:"#e03131",borderVisible:false,wickUpColor:"#12915f",wickDownColor:"#e03131"});
  cs.setData(d.candles);
  d.levels.forEach(l=>cs.createPriceLine({price:l.p,color:l.p>d.last?"#e03131":"#12915f",lineWidth:2,lineStyle:2,axisLabelVisible:true,title:l.s+"%"}));
  const T=d.candles.map(c=>c.time),h=d.harsi;
  const hc=CH2.addCandlestickSeries({upColor:"#008b7f",downColor:"#f23645",borderUpColor:"#008b7f",borderDownColor:"#f23645",wickUpColor:"#787b86",wickDownColor:"#787b86",priceLineVisible:false,lastValueVisible:false});
  const hi=CH2.addHistogramSeries({priceFormat:{type:"price",precision:2,minMove:0.01},priceLineVisible:false,lastValueVisible:false});
  const rl=CH2.addLineSeries({color:"#fbc02d",lineWidth:1,priceLineVisible:false,lastValueVisible:false});
  const sk=CH2.addLineSeries({color:"#2196f3",lineWidth:1,visible:stOn,priceLineVisible:false,lastValueVisible:false});
  const sd=CH2.addLineSeries({color:"#ff6d00",lineWidth:1,visible:stOn,priceLineVisible:false,lastValueVisible:false});
  STS=[sk,sd];
  const W=(i,x,v)=>x&&v!=null?v:null;
  hc.setData(h.map((x,i)=>x.c!=null?{time:T[i],open:x.o,high:x.h,low:x.l,close:x.c}:{time:T[i]}));
  hi.setData(h.map((x,i)=>x.r!=null?{time:T[i],value:x.r,color:x.r>=0?"#00897b80":"#f2364580"}:{time:T[i]}));
  rl.setData(h.map((x,i)=>x.r!=null?{time:T[i],value:x.r}:{time:T[i]}));
  sk.setData(h.map((x,i)=>x.k!=null?{time:T[i],value:x.k}:{time:T[i]}));
  sd.setData(h.map((x,i)=>x.d!=null?{time:T[i],value:x.d}:{time:T[i]}));
  [40,20,0,-20,-40].forEach(v=>hc.createPriceLine({price:v,color:v==0?"#ff9800":"#8887",lineWidth:1,lineStyle:v==0?3:2,axisLabelVisible:v!=0,title:""}));
  CH.timeScale().fitContent();CH2.timeScale().fitContent();
  let busy=false;
  const sync=(a,b)=>a.timeScale().subscribeVisibleLogicalRangeChange(g=>{if(g&&!busy){busy=true;b.timeScale().setVisibleLogicalRange(g);busy=false}});
  sync(CH,CH2);sync(CH2,CH);
  const lx=[...h].reverse().find(x=>x.c!=null&&x.r!=null);
  const hs=lx?("HARSI: "+(lx.c>=lx.o?"Bullish":"Bearish")+", RSI "+(lx.r>=0?"+":"")+f(lx.r,1)+" | "):"";
  S.textContent=hs+"Levels ("+cvTfv+"): "+d.levels.map(l=>f(l.p,0)+" ("+l.s+"%)").join(", ");
 }catch(e){S.textContent="Chart error: "+e}
}
window.addEventListener("resize",()=>{const P=document.getElementById("cvp"),Q=document.getElementById("cvr");if(CH)CH.resize(P.clientWidth,P.clientHeight);if(CH2)CH2.resize(Q.clientWidth,Q.clientHeight)});
async function load(){
 N.forEach(n=>document.getElementById("t"+n).className=n==cur?"on":"");
 try{const r=await fetch("/api/"+cur);const d=await r.json();
  if(d.error){st.textContent="Error: "+d.error}else{st.textContent="Live, refresh 5s";cur=="SCAN"?drawScan(d):draw(d)}}
 catch(e){st.textContent="Network error"}
}
load();setInterval(load,5000);
</script></body></html>"""


@app.route("/")
def home():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
