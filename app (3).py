import os, math, time, threading, datetime as dt
import requests, pyotp
from flask import Flask, jsonify, render_template_string
from SmartApi import SmartConnect

API_KEY = os.environ["API_KEY"]
CLIENT_ID = os.environ["CLIENT_ID"]
PIN = os.environ["PIN"]
TOTP_SECRET = os.environ["TOTP_SECRET"]
WIDTH = 7  # strikes each side of ATM

SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
# name: (spot exchange, spot token, strike step, option exchange)
INDEX = {
    "NIFTY": ("NSE", "99926000", 50, "NFO"),
    "BANKNIFTY": ("NSE", "99926009", 100, "NFO"),
    "SENSEX": ("BSE", "99919000", 100, "BFO"),
}

app = Flask(__name__)
lock = threading.Lock()
state = {"api": None, "scrip": None, "scrip_day": None, "cache": {},
         "stocks": {}, "lv": {}, "lv_day": None,
         "scan": {"rows": [], "time": None, "ready": 0, "total": 0, "err": None, "closed": False}}


# ---------- login & data ----------
def login():
    api = SmartConnect(api_key=API_KEY)
    r = api.generateSession(CLIENT_ID, PIN, pyotp.TOTP(TOTP_SECRET).now())
    if not r.get("status"):
        raise RuntimeError("Login failed: " + str(r.get("message")))
    state["api"] = api


def scrip_master():
    today = dt.date.today()
    if state["scrip"] is None or state["scrip_day"] != today:
        data = requests.get(SCRIP_URL, timeout=120).json()
        state["scrip"] = [x for x in data if x.get("instrumenttype") == "OPTIDX"]
        fno = {x["name"] for x in data if x.get("instrumenttype") == "OPTSTK" and x.get("exch_seg") == "NFO"}
        state["stocks"] = {x["name"]: x["token"] for x in data
                           if x.get("exch_seg") == "NSE" and x.get("symbol", "").endswith("-EQ") and x["name"] in fno}
        del data
        state["scrip_day"] = today
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


def key_levels(c, L=6, M=10, N=4):
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
    tol = c[-1][4] * 0.006
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


def get_signal(q, levels):
    ltp, op, hi, lo, pc = (q.get(k) for k in ("ltp", "open", "high", "low", "close"))
    if not all([ltp, op, hi, lo, pc]):
        return None
    best = None
    for lv in levels:
        p = lv["p"]
        kind = d = None
        if pc <= p and ltp >= p * 1.002 and ltp > op:
            kind, d = "Breakout", "CALL"
        elif pc >= p and ltp <= p * 0.998 and ltp < op:
            kind, d = "Breakdown", "PUT"
        elif p < ltp and p * 0.99 <= lo <= p * 1.003 and ltp >= p * 1.004 and ltp > op:
            kind, d = "Support bounce", "CALL"
        elif p > ltp and p * 0.997 <= hi <= p * 1.01 and ltp <= p * 0.996 and ltp < op:
            kind, d = "Resistance reject", "PUT"
        if kind and (best is None or lv["s"] > best["strength"]):
            best = {"dir": d, "kind": kind, "level": p, "strength": lv["s"]}
    return best


def candles(ex, tok):
    now = dt.datetime.now()
    params = {"exchange": ex, "symboltoken": tok, "interval": "ONE_DAY",
              "fromdate": (now - dt.timedelta(days=220)).strftime("%Y-%m-%d %H:%M"),
              "todate": now.strftime("%Y-%m-%d %H:%M")}
    for _ in range(3):
        with lock:
            if state["api"] is None:
                login()
            r = state["api"].getCandleData(params)
        if r.get("status") and r.get("data"):
            today = now.date().isoformat()
            return [x for x in r["data"] if x[0][:10] != today]
        if "token" in str(r.get("message", "")).lower():
            state["api"] = None
        time.sleep(1.5)
    return None


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
        rows.append({"name": n, "idx": n in INDEX, "ltp": ltp,
                     "chg": (ltp - pc) / pc * 100 if pc else 0,
                     "sig": get_signal(q, v["levels"]),
                     "sup": max(below) if below else None, "res": min(above) if above else None})
    rows.sort(key=lambda r: (not r["idx"], -(r["sig"]["strength"] if r["sig"] else 0)))
    state["scan"] = {"rows": rows, "time": dt.datetime.now().strftime("%H:%M:%S"),
                     "ready": len(lv), "total": state["scan"].get("total", 0),
                     "err": None, "closed": market_closed()}


def scan_loop():
    while True:
        try:
            now = dt.datetime.now()
            if state["lv_day"] != now.date():
                with lock:
                    scrip_master()
                uni = {n: ("NSE", t) for n, t in state["stocks"].items()}
                for n, v in INDEX.items():
                    uni[n] = (v[0], v[1])
                state["scan"]["total"] = len(uni)
                for n, (ex, tok) in uni.items():
                    if n in state["lv"] and state["lv"][n].get("day") == now.date():
                        continue
                    c = candles(ex, tok)
                    if c and len(c) > 30:
                        state["lv"][n] = {"ex": ex, "tok": tok, "levels": key_levels(c),
                                          "pc": c[-1][4], "day": now.date()}
                    time.sleep(0.5)
                if len(state["lv"]) > 0.8 * len(uni):
                    state["lv_day"] = now.date()
            if state["lv"]:
                live_scan()
        except Exception as e:
            state["scan"]["err"] = str(e)[:200]
            state["api"] = None
        time.sleep(20)


def final_signal(name, bias):
    row = next((r for r in state["scan"]["rows"] if r["name"] == name), None)
    kl = row["sig"] if row else None
    if not kl:
        return {"text": "WAIT", "why": "No key-level signal yet"}
    want = "BULLISH" if kl["dir"] == "CALL" else "BEARISH"
    desc = "%s @%d (%d%%)" % (kl["kind"], kl["level"], kl["strength"])
    if bias["label"] == want:
        return {"text": "BUY " + kl["dir"], "why": desc + " + bias " + bias["label"]}
    if bias["label"] == "NEUTRAL":
        return {"text": "WEAK " + kl["dir"], "why": desc + ", bias neutral"}
    return {"text": "WAIT", "why": desc + " conflicts with bias " + bias["label"]}


threading.Thread(target=scan_loop, daemon=True).start()


@app.route("/api/scan")
def api_scan():
    return jsonify(state["scan"])


@app.route("/api/<name>")
def api(name):
    name = name.upper()
    if name not in INDEX:
        return jsonify(error="unknown index"), 404
    with lock:
        c = state["cache"].get(name)
        if c and time.time() - c[0] < 3:
            return jsonify(c[1])
        try:
            d = build(name)
        except Exception as e:
            state["api"] = None
            return jsonify(error=str(e)), 500
        d["signal"] = final_signal(name, d["bias"])
        state["cache"][name] = (time.time(), d)
        return jsonify(d)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes"><title>Options Desk</title>
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
</style></head><body>
<div class="tabs" id="tabs"></div><div id="st">Loading...</div><div id="out"></div>
<script>
let cur="NIFTY";const N=["NIFTY","BANKNIFTY","SENSEX","SCAN"];
const T=document.getElementById("tabs");
N.forEach(n=>{const b=document.createElement("button");b.textContent=n;b.onclick=()=>{cur=n;load()};b.id="t"+n;T.appendChild(b)});
const f=(x,d=1)=>x==null?"-":Number(x).toLocaleString("en-IN",{maximumFractionDigits:d,minimumFractionDigits:d});
const pc=x=>x==null?"-":(x*100).toFixed(1)+"%";
const R=(a,b,c="")=>`<div class="r"><span>${a}</span><span class="${c}">${b}</span></div>`;
const C=(t,col,rows)=>`<div class="card"><h3 style="background:${col}">${t}</h3>${rows.join("")}</div>`;
function draw(d){
 const pcrc=x=>x==null?"":x>=1.2?"up":x<=0.8?"dn":"";
 const em=(c,m)=>m==null?"-":`${f(c-m,0)} to ${f(c+m,0)}`;
 let mx=Math.max(...d.gex.map(g=>Math.abs(g.g)),1e-9);
 const bars=d.gex.map(g=>`<div class="r"><span>${f(g.k,0)}</span><span style="flex:1;margin:0 8px;display:flex;align-items:center"><i style="display:block;height:8px;width:${Math.abs(g.g)/mx*100}%;background:${g.g>=0?"var(--up)":"var(--dn)"}"></i></span><span class="${g.g>=0?"up":"dn"}">${f(g.g,0)}</span></div>`).join("");
 out.innerHTML=
 C("SINGLE SIGNAL","#0b0e14",[R("Signal",d.signal.text,d.signal.text.startsWith("BUY CALL")?"up":d.signal.text.startsWith("BUY PUT")?"dn":""),R("Why",d.signal.why)])+
 C("BIAS SCORE","#334155",[R("Bias",d.bias.label+" ("+(d.bias.score>0?"+":"")+d.bias.score+")",d.bias.score>=2?"up":d.bias.score<=-2?"dn":""),R("Regime",d.bias.regime),...d.bias.reasons.map(x=>R(x[0],x[1]>0?"+1":x[1]<0?"-1":"0",x[1]>0?"up":x[1]<0?"dn":"")),d.bias.closed?R("Warning","Market closed: stale data","dn"):R("Note","Not advice. Use with price action.")])+
 C(`${d.name} | Spot ${f(d.spot,2)}`,"#0874b0",[R("ATM",f(d.atm,0)),R("Expiry",d.expiry+" ("+d.days_left+"d)"),R("Updated",d.time)])+
 C("SPOT vs SYNTHETIC","#0ea5e9",[R("ATM call",f(d.call,2)),R("ATM put",f(d.put,2)),R("Synthetic",f(d.synthetic,1)),R("Basis",(d.basis>=0?"+":"")+f(d.basis,1),d.basis>=0?"up":"dn")])+
 C("EXPECTED MOVE","#7c5ce0",[R("ATM IV",pc(d.atm_iv)),R("Straddle",f(d.straddle,2)),R("IV 1σ",em(d.spot,d.em_iv)),R("Straddle x1.25",em(d.spot,d.em_straddle))])+
 C("PCR","#0f9f8f",[R("OI whole (loaded)",f(d.pcr_oi_all,2),pcrc(d.pcr_oi_all)),R("OI ATM ±3",f(d.pcr_oi_near,2),pcrc(d.pcr_oi_near)),R("Volume",f(d.pcr_vol_all,2),pcrc(d.pcr_vol_all))])+
 C("GAMMA & MAX PAIN","#ea6a12",[R("Net GEX ₹cr/1%",f(d.net_gex_cr,0),d.net_gex_cr>=0?"up":"dn"),R("Mode",d.net_gex_cr>=0?"Pinning":"Trending"),R("Gamma flip",f(d.flip,0)),R("Call wall",f(d.call_wall,0),"dn"),R("Put wall",f(d.put_wall,0),"up"),R("Max pain",f(d.max_pain,0)),R("OI support | resist",f(d.oi_support,0)+" | "+f(d.oi_resist,0))])+
 C("NET GEX BY STRIKE","#16a34a",[bars]);
}
function drawScan(s){
 const rows=s.rows||[];const calls=rows.filter(r=>r.sig&&r.sig.dir=="CALL"),puts=rows.filter(r=>r.sig&&r.sig.dir=="PUT");
 const line=r=>R(`${r.name}  ${f(r.ltp,2)} (${r.chg>=0?"+":""}${f(r.chg,2)}%)`,`${r.sig.kind} @${f(r.sig.level,0)} (${r.sig.strength}%)`,r.sig.dir=="CALL"?"up":"dn");
 const mk=(t,col,a)=>C(t+" ("+a.length+")",col,a.length?a.map(line):[R("None right now","-")]);
 const idx=rows.filter(r=>r.idx).map(r=>R(`${r.name} ${f(r.ltp,2)}`,r.sig?r.sig.dir+" "+r.sig.kind:"WAIT",r.sig?(r.sig.dir=="CALL"?"up":"dn"):""));
 out.innerHTML=C("SCANNER STATUS","#334155",[R("Levels ready",(s.ready||0)+"/"+(s.total||0)),R("Updated",s.time||"loading..."),s.closed?R("Warning","Market closed: stale","dn"):R("Note","Daily key levels + live price"),s.err?R("Error",s.err,"dn"):""])+
 C("INDICES","#0874b0",idx.length?idx:[R("Loading levels, wait 2-3 min","-")])+mk("BUY CALL SIGNALS","#12915f",calls)+mk("BUY PUT SIGNALS","#e03131",puts);
}
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
