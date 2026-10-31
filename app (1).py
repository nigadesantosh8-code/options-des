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
state = {"api": None, "scrip": None, "scrip_day": None, "cache": {}}


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
        data = requests.get(SCRIP_URL, timeout=60).json()
        state["scrip"] = [x for x in data if x.get("instrumenttype") == "OPTIDX"]
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
    }


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
let cur="NIFTY";const N=["NIFTY","BANKNIFTY","SENSEX"];
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
 C(`${d.name} | Spot ${f(d.spot,2)}`,"#0874b0",[R("ATM",f(d.atm,0)),R("Expiry",d.expiry+" ("+d.days_left+"d)"),R("Updated",d.time)])+
 C("SPOT vs SYNTHETIC","#0ea5e9",[R("ATM call",f(d.call,2)),R("ATM put",f(d.put,2)),R("Synthetic",f(d.synthetic,1)),R("Basis",(d.basis>=0?"+":"")+f(d.basis,1),d.basis>=0?"up":"dn")])+
 C("EXPECTED MOVE","#7c5ce0",[R("ATM IV",pc(d.atm_iv)),R("Straddle",f(d.straddle,2)),R("IV 1σ",em(d.spot,d.em_iv)),R("Straddle x1.25",em(d.spot,d.em_straddle))])+
 C("PCR","#0f9f8f",[R("OI whole (loaded)",f(d.pcr_oi_all,2),pcrc(d.pcr_oi_all)),R("OI ATM ±3",f(d.pcr_oi_near,2),pcrc(d.pcr_oi_near)),R("Volume",f(d.pcr_vol_all,2),pcrc(d.pcr_vol_all))])+
 C("GAMMA & MAX PAIN","#ea6a12",[R("Net GEX ₹cr/1%",f(d.net_gex_cr,0),d.net_gex_cr>=0?"up":"dn"),R("Mode",d.net_gex_cr>=0?"Pinning":"Trending"),R("Gamma flip",f(d.flip,0)),R("Call wall",f(d.call_wall,0),"dn"),R("Put wall",f(d.put_wall,0),"up"),R("Max pain",f(d.max_pain,0)),R("OI support | resist",f(d.oi_support,0)+" | "+f(d.oi_resist,0))])+
 C("NET GEX BY STRIKE","#16a34a",[bars]);
}
async function load(){
 N.forEach(n=>document.getElementById("t"+n).className=n==cur?"on":"");
 try{const r=await fetch("/api/"+cur);const d=await r.json();
  if(d.error){st.textContent="Error: "+d.error}else{st.textContent="Live, refresh 5s";draw(d)}}
 catch(e){st.textContent="Network error"}
}
load();setInterval(load,5000);
</script></body></html>"""


@app.route("/")
def home():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
