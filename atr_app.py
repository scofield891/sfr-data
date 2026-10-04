#!/usr/bin/env python3
"""ATR Stop - telefon icin web uygulamasi (tek dosya, ek paket gerekmez).

Hisse kodu + giris fiyati + yon -> 2 x ATR(14) stop ve 2R kar al.
ATR, hissenin ABD borsasindaki (NASDAQ/NYSE) gunluk barlarindan hesaplanir; veri her istekte canli cekilir.

Calistirma:   python3 atr_app.py --port 8105
Telefonda:    http://SUNUCU_IP:8105  ->  tarayici menusu -> "Ana ekrana ekle"
Veri kaynagi sirasi: Yahoo (anahtarsiz) -> Tiingo (TIINGO_API_KEY ortam degiskeni varsa) -> Stooq.
"""
import argparse, base64, csv, io, json, os, re, sys, threading, time
import urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TTL = 600            # ayni hisse 10 dakika onbellekten
ATR_N = 14
CACHE = {}
LOCK = threading.Lock()
AYLAR_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def http_get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def wilder_atr(bars, n=ATR_N):
    """bars: [(tarih, o, h, l, c), ...] eskiden yeniye. TradingView ta.atr ile ayni (RMA, ilk deger SMA)."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i, (_, o, h, l, c) in enumerate(bars):
        if i == 0:
            trs.append(h - l)
        else:
            pc = bars[i - 1][4]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def from_yahoo(t):
    last = None
    for host in ("query1", "query2"):
        try:
            raw = http_get("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1y&interval=1d&includePrePost=false" % (host, quote(t)))
            j = json.loads(raw)
            res = (j.get("chart") or {}).get("result")
            if not res:
                err = (j.get("chart") or {}).get("error") or {}
                raise LookupError(err.get("description") or "bulunamadi")
            res = res[0]; meta = res.get("meta") or {}
            ts = res.get("timestamp") or []
            q = (res.get("indicators") or {}).get("quote") or [{}]
            q = q[0]
            gmt = int(meta.get("gmtoffset") or -14400)
            bars = []
            col = lambda k, i: (q.get(k) or [])[i] if i < len(q.get(k) or []) else None
            for i, s in enumerate(ts):
                o, h, l, c = col("open", i), col("high", i), col("low", i), col("close", i)
                if None in (o, h, l, c) or min(o, h, l, c) <= 0:
                    continue
                d = datetime.fromtimestamp(s + gmt, tz=timezone.utc).strftime("%Y-%m-%d")
                bars.append((d, float(o), float(h), float(l), float(c), int(s)))
            # seans aciksa son bar yarimdir: at
            reg = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
            now = time.time()
            if bars and reg.get("start") and reg.get("end") and bars[-1][5] >= int(reg["start"]) and now < int(reg["end"]):
                bars = bars[:-1]
            bars = [b[:5] for b in bars]
            return {"bars": bars, "name": meta.get("shortName") or meta.get("longName") or "",
                    "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
                    "price": float(meta.get("regularMarketPrice") or 0) or None,
                    "type": meta.get("instrumentType") or "", "currency": meta.get("currency") or "", "source": "Yahoo"}
        except LookupError:
            raise
        except Exception as e:      # ag hatasi, 429 vb. -> diger sunucuyu dene
            last = e
    raise RuntimeError("Yahoo: %s" % last)


def from_tiingo(t, key):
    start = (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%d")
    raw = http_get("https://api.tiingo.com/tiingo/daily/%s/prices?startDate=%s&token=%s" % (quote(t), start, quote(key)))
    j = json.loads(raw)
    if not isinstance(j, list) or not j:
        raise LookupError("bulunamadi")
    bars = []
    for x in j:
        o, h, l, c = x.get("adjOpen"), x.get("adjHigh"), x.get("adjLow"), x.get("adjClose")
        if None in (o, h, l, c):
            continue
        bars.append((str(x["date"])[:10], float(o), float(h), float(l), float(c)))
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if bars and bars[-1][0] == today and datetime.utcnow().hour < 21:
        bars = bars[:-1]
    return {"bars": bars, "name": "", "exchange": "ABD", "price": None, "type": "", "currency": "USD", "source": "Tiingo"}


def from_stooq(t):
    raw = http_get("https://stooq.com/q/d/l/?s=%s.us&i=d" % quote(t.lower()))
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows or "Close" not in rows[0]:
        raise LookupError("bulunamadi")
    bars = []
    for x in rows[-260:]:
        try:
            bars.append((x["Date"], float(x["Open"]), float(x["High"]), float(x["Low"]), float(x["Close"])))
        except Exception:
            pass
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if bars and bars[-1][0] == today and datetime.utcnow().hour < 21:
        bars = bars[:-1]
    return {"bars": bars, "name": "", "exchange": "ABD", "price": None, "type": "", "currency": "USD", "source": "Stooq"}


def get_atr(t):
    now = time.time()
    with LOCK:
        hit = CACHE.get(t)
        if hit and now - hit[0] < TTL:
            return hit[1]
    errors = []; d = None; missing = False
    sources = [("Yahoo", lambda: from_yahoo(t))]
    key = os.environ.get("TIINGO_API_KEY") or os.environ.get("TIINGO_TOKEN")
    if key:
        sources.append(("Tiingo", lambda: from_tiingo(t, key)))
    sources.append(("Stooq", lambda: from_stooq(t)))
    for name, fn in sources:
        try:
            d = fn()
            if d and len(d["bars"]) >= ATR_N + 1:
                break
            errors.append("%s: yetersiz veri" % name); d = None
        except LookupError as e:
            missing = True; errors.append("%s: %s" % (name, e))
        except Exception as e:
            errors.append("%s: %s" % (name, str(e)[:120]))
    if not d:
        if missing:
            out = {"ok": False, "error": "“%s” kodlu bir ABD hissesi bulunamadı. Kodu kontrol et." % t}
        else:
            out = {"ok": False, "error": "Veri alınamadı (%s). Biraz sonra yeniden dene." % "; ".join(errors)[:300]}
        return out
    bars = d["bars"]; atr = wilder_atr(bars)
    y, m, dd = bars[-1][0].split("-")
    out = {"ok": True, "ticker": t, "name": d["name"], "exchange": d["exchange"], "atr": round(atr, 4),
           "close": bars[-1][4], "price": d["price"] or bars[-1][4], "bar_date": bars[-1][0],
           "bar_date_tr": "%d %s %s" % (int(dd), AYLAR_TR[int(m) - 1], y), "n_bars": len(bars), "source": d["source"]}
    if d.get("currency") and d["currency"] != "USD":
        out = {"ok": False, "error": "“%s” dolar cinsinden işlem görmüyor; yalnız ABD hisseleri desteklenir." % t}
    with LOCK:
        CACHE[t] = (now, out)
    return out


PAGE = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ATR Stop</title>
<link rel="manifest" href="manifest.json">
<link rel="icon" href="icon.png"><link rel="apple-touch-icon" href="icon.png">
<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="ATR Stop">
<meta name="theme-color" content="#0e1412" media="(prefers-color-scheme: dark)"><meta name="theme-color" content="#eef1ef" media="(prefers-color-scheme: light)">
<style>
/* Yerleşim: emir fişi. Üstte üç girdi, altında iki büyük seviye, ölçekli fiyat şeridi, tasfiye kontrolü. */
:root{
  --bg:#eef1ef; --surface:#fbfcfb; --line:#d3dbd6; --fg:#13201b; --muted:#5a6962;
  --accent:#0d6b5a; --accent-fg:#ffffff;
  --gain:#147a43; --gain-bg:#dff1e6; --loss:#b8322b; --loss-bg:#f8e1df; --warn:#8f5d00; --warn-bg:#f6ead0;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono","Roboto Mono",Menlo,Consolas,monospace;
  --r:12px; color-scheme:light;
}
@media (prefers-color-scheme: dark){ :root{
  --bg:#0e1412; --surface:#161e1b; --line:#2b3632; --fg:#e5ede8; --muted:#93a39b;
  --accent:#55c9ac; --accent-fg:#06211b;
  --gain:#54c48b; --gain-bg:#143224; --loss:#f0746a; --loss-bg:#3a1c1a; --warn:#e2ab4d; --warn-bg:#3a2d12;
  color-scheme:dark } }
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);font-size:15px;line-height:1.45;
  padding:calc(18px + env(safe-area-inset-top,0px)) 16px calc(36px + env(safe-area-inset-bottom,0px))}
[hidden]{display:none!important}
.wrap{max-width:520px;margin-inline:auto;display:flex;flex-direction:column;gap:16px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
h1{font-size:22px;line-height:1.15;margin:0;font-weight:750;letter-spacing:-.01em}
.eyebrow{font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:650}
.form{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.field{display:flex;flex-direction:column;gap:5px;min-width:0}
.field.full{grid-column:1 / -1}
label,.lab{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:650}
input,select{font:inherit;font-size:17px;color:var(--fg);background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:13px 12px;width:100%;min-width:0}
input.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
#ticker{text-transform:uppercase;font-weight:650;letter-spacing:.03em}
input:focus-visible,select:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.seg{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);border-radius:var(--r);overflow:hidden;background:var(--surface)}
.seg button{font:inherit;font-size:16px;font-weight:650;padding:13px 8px;border:0;background:transparent;color:var(--muted);cursor:pointer}
#dirLong[aria-pressed="true"]{background:var(--gain);color:var(--surface)}
#dirShort[aria-pressed="true"]{background:var(--loss);color:var(--surface)}
.info{font-size:13.5px;color:var(--muted);display:flex;flex-direction:column;gap:6px;min-height:20px}
.info.bad{color:var(--loss)}
.chip{align-self:flex-start;font:inherit;font-size:13.5px;font-weight:600;color:var(--accent);background:transparent;border:1px solid var(--accent);border-radius:999px;padding:6px 12px;cursor:pointer}
.levels{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.level{border-radius:var(--r);padding:14px 14px 12px;display:flex;flex-direction:column;gap:2px;min-width:0}
.level.stop{background:var(--loss-bg)} .level.tp{background:var(--gain-bg)}
.level .lab{color:var(--fg);opacity:.75}
.level .px{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:clamp(25px,8vw,34px);font-weight:650;line-height:1.1;overflow-wrap:anywhere}
.level.stop .px{color:var(--loss)} .level.tp .px{color:var(--gain)}
.level .sub{font-family:var(--mono);font-size:12.5px;opacity:.85}
.ladder{position:relative;height:96px}
.ladder .track{position:absolute;left:0;right:0;top:46px;height:6px;border-radius:3px;background:var(--line)}
.ladder .segm{position:absolute;top:46px;height:6px}
.ladder .segm.g{background:var(--gain)} .ladder .segm.l{background:var(--loss)}
.ladder .tick{position:absolute;top:38px;width:2px;height:22px;background:var(--fg);transform:translateX(-1px)}
.ladder .tick.liq{background:var(--warn);height:30px;top:34px}
.ladder .tl{position:absolute;display:flex;flex-direction:column;line-height:1.15;white-space:nowrap;transform:translateX(-50%)}
.ladder .tl.left{transform:none} .ladder .tl.right{transform:translateX(-100%);text-align:right}
.ladder .tl.top{top:0} .ladder .tl.bot{top:66px}
.ladder .tl b{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:650}
.ladder .tl span{font-family:var(--mono);font-size:12.5px;font-variant-numeric:tabular-nums}
.ladder .tl.liq b,.ladder .tl.liq span{color:var(--warn)}
.liqbox{display:flex;flex-direction:column;gap:10px;border-top:1px solid var(--line);padding-top:14px}
.liqrow{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.status{border-radius:var(--r);padding:12px 14px;display:flex;flex-direction:column;gap:3px;border-left:5px solid var(--gain);background:var(--gain-bg)}
.status[data-s="warn"]{border-left-color:var(--warn);background:var(--warn-bg)}
.status[data-s="crit"]{border-left-color:var(--loss);background:var(--loss-bg)}
.status strong{font-size:15.5px;font-weight:700}
.status span{font-size:13.5px}
dl{margin:0;border-top:1px solid var(--line)}
.row{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid var(--line);align-items:baseline}
.row dt{color:var(--muted);font-size:13.5px;min-width:0}
.row dd{margin:0;font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13.5px;text-align:right;min-width:0;overflow-wrap:anywhere}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button.copy{font:inherit;font-weight:650;font-size:16px;padding:13px 18px;border-radius:var(--r);border:0;background:var(--accent);color:var(--accent-fg);cursor:pointer}
.copied{font-size:13px;color:var(--muted)}
.note{font-size:12.5px;color:var(--muted);border-top:1px solid var(--line);padding-top:12px}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
</style></head>
<body>
<div class="wrap">
  <header><h1>ATR Stop</h1><span class="eyebrow">2×ATR stop · 2R hedef</span></header>

  <div class="form">
    <div class="field">
      <label for="ticker">Hisse</label>
      <input id="ticker" autocomplete="off" autocapitalize="characters" autocorrect="off" spellcheck="false" placeholder="QCOM" enterkeyhint="next">
    </div>
    <div class="field">
      <label for="entry">Giriş fiyatı ($)</label>
      <input id="entry" class="num" inputmode="decimal" placeholder="0,00" enterkeyhint="done">
    </div>
    <div class="field full">
      <span class="lab" id="dirlab">Yön</span>
      <div class="seg" role="group" aria-labelledby="dirlab">
        <button type="button" id="dirLong" aria-pressed="true">Long</button>
        <button type="button" id="dirShort" aria-pressed="false">Short</button>
      </div>
    </div>
    <div class="field full"><div class="info" id="info">Hisse kodunu yaz; ATR'yi borsadaki günlük grafikten ben çekerim.</div></div>
  </div>

  <div id="result" hidden>
   <div class="wrap">
    <div class="levels">
      <div class="level stop"><span class="lab">Stop · 2×ATR</span><span class="px" id="stopPx"></span><span class="sub" id="stopSub"></span></div>
      <div class="level tp"><span class="lab">Kâr al · 2R</span><span class="px" id="tpPx"></span><span class="sub" id="tpSub"></span></div>
    </div>
    <div class="ladder" id="ladder" aria-hidden="true"></div>
    <dl id="rows"></dl>
    <div class="actions"><button type="button" class="copy" id="copy">Seviyeleri kopyala</button><span class="copied" id="copied" role="status"></span></div>

    <div class="liqbox">
      <span class="lab">Tasfiye kontrolü</span>
      <div class="liqrow">
        <div class="field"><label for="lev">Kaldıraç</label>
          <select id="lev"><option value="1">1 kat</option><option value="2">2 kat</option><option value="3">3 kat</option><option value="5" selected>5 kat</option><option value="7">7 kat</option><option value="10">10 kat</option></select></div>
        <div class="field"><label for="venue">Borsa</label>
          <select id="venue"><option value="bybit" selected>Bybit</option><option value="binance">Binance</option></select></div>
      </div>
      <div class="status" id="status" data-s="ok"><strong id="stTitle"></strong><span id="stText"></span></div>
    </div>
   </div>
  </div>

  <div class="note">Stop = giriş ∓ 2 × ATR(14). Kâr al = giriş ± 4 × ATR(14). ATR, hissenin ABD borsasındaki son tamamlanmış günlük barına kadar hesaplanır. Komisyon ve fonlama dahil değil.</div>
</div>

<script>
const MMR = {bybit:0.02, binance:0.025}, LEVS = [10,7,5,3,2,1];
const $ = id => document.getElementById(id);
const el = {ticker:$('ticker'), entry:$('entry'), lev:$('lev'), venue:$('venue')};
let dir = 1, rec = null, recFor = '', timer = null, seq = 0;

const num = s => { if(s==null) return NaN; let t = String(s).trim().replace(/\s/g,''); if(t.includes(',') && t.includes('.')) t = t.replace(/\./g,''); const v = parseFloat(t.replace(',', '.')); return isFinite(v) ? v : NaN; };
const f = (n,d=2) => n.toLocaleString('tr-TR',{minimumFractionDigits:d,maximumFractionDigits:d});
const pct = (x,d=1) => '%' + f(x*100,d);
const normT = s => { let t = String(s||'').toUpperCase().trim().replace(/\s+/g,''); if(t==='BRKB'||t==='BRK.B'||t==='BRK/B') t='BRK-B'; return t.replace(/\./g,'-'); };
const liqPrice = (entry,L,mmr,d) => L<=1 ? null : (d===1 ? entry*(1-1/L)/(1-mmr) : entry*(1+1/L)/(1+mmr));

function calc(entry, atr, d, L, venue){
  if(!(entry>0) || !(atr>0)) return null;
  const R = 2*atr, stop = entry - d*R, tp = entry + d*2*R;
  if(stop<=0 || tp<=0) return {bad:true};
  const stopDist = R/entry, mmr = MMR[venue], liq = liqPrice(entry,L,mmr,d);
  const liqDist = liq==null ? null : Math.abs(liq-entry)/entry;
  let state = 'ok'; if(liqDist!=null){ if(stopDist>=liqDist) state='crit'; else if(stopDist>=0.8*liqDist) state='warn'; }
  let maxLev = 1; for(const k of LEVS){ if(k===1) break; const lp = liqPrice(entry,k,mmr,d); if(stopDist < 0.8*Math.abs(lp-entry)/entry){ maxLev=k; break; } }
  return {R, stop, tp, stopDist, liq, liqDist, state, maxLev};
}

function setInfo(html, bad){ const i=$('info'); i.className='info'+(bad?' bad':''); i.textContent=''; if(typeof html==='string') i.textContent=html; else i.append(...html); }

async function load(t){
  const my = ++seq; rec = null; recFor = t; render();
  const sp=document.createElement('span'); const s=document.createElement('span'); s.className='spin'; sp.append(s, t+' için günlük veriler çekiliyor…'); setInfo([sp]);
  try{
    const r = await fetch('api/atr?ticker='+encodeURIComponent(t), {cache:'no-store'});
    const j = await r.json(); if(my!==seq) return;
    if(!j.ok){ setInfo(j.error || 'Veri alınamadı.', true); return; }
    rec = j; showRec(); render();
  }catch(e){ if(my!==seq) return; setInfo('Sunucuya ulaşılamadı. Bağlantını kontrol edip yeniden dene.', true); }
}

function showRec(){
  const a=document.createElement('span');
  a.textContent = rec.ticker+(rec.name?' · '+rec.name:'')+' · '+(rec.exchange||'ABD')+'. ATR(14) '+f(rec.atr)+' $ ('+pct(rec.atr/rec.close)+'), '+rec.bar_date_tr+' kapanışına kadar.';
  const parts=[a];
  if(rec.price>0){ const b=document.createElement('button'); b.type='button'; b.className='chip'; b.textContent='Son fiyat '+f(rec.price)+' $ · giriş olarak kullan'; b.addEventListener('click',()=>{ el.entry.value=f(rec.price); render(); }); parts.push(b); }
  setInfo(parts);
}

function ladder(entry, r){
  const L = $('ladder'); L.textContent='';
  const showLiq = r.liq!=null && r.liqDist <= 3*r.stopDist;
  const pts = [r.stop, r.tp, entry]; if(showLiq) pts.push(r.liq);
  let lo = Math.min(...pts), hi = Math.max(...pts); const pad=(hi-lo)*0.07; lo-=pad; hi+=pad;
  const P = x => (x-lo)/(hi-lo)*100;
  const add = (cls, st) => { const d=document.createElement('div'); d.className=cls; Object.assign(d.style, st); L.appendChild(d); return d; };
  add('track',{});
  add('segm l',{left:Math.min(P(r.stop),P(entry))+'%', width:Math.abs(P(r.stop)-P(entry))+'%'});
  add('segm g',{left:Math.min(P(r.tp),P(entry))+'%', width:Math.abs(P(r.tp)-P(entry))+'%'});
  const mark = (x,name,row,extra) => { const p=P(x); add('tick'+(extra?' '+extra:''),{left:p+'%'});
    const t=add('tl '+row+(extra?' '+extra:'')+(p<16?' left':p>84?' right':''),{left:p+'%'}); const b=document.createElement('b'); b.textContent=name; const s=document.createElement('span'); s.textContent=f(x); t.append(b,s); };
  mark(r.stop,'Stop','top'); mark(entry,'Giriş','top'); mark(r.tp,'Kâr al','top'); if(showLiq) mark(r.liq,'Tasfiye','bot','liq');
}

function render(){
  const entry = num(el.entry.value), L = parseInt(el.lev.value,10), venue = el.venue.value;
  const r = rec ? calc(entry, rec.atr, dir, L, venue) : null;
  $('result').hidden = !r || r.bad;
  if(rec && r && r.bad){ setInfo('ATR giriş fiyatına göre çok büyük; fiyatı kontrol et.', true); return; }
  if(!r) return;
  if(rec.price>0 && Math.abs(entry/rec.price-1)>0.25){ setInfo('Giriş fiyatı son fiyattan ('+f(rec.price)+' $) '+pct(Math.abs(entry/rec.price-1),0)+' farklı; hisse kodunu ve fiyatı kontrol et.', true); } else if($('info').classList.contains('bad')) showRec();
  const sg = dir===1 ? '−' : '+', sg2 = dir===1 ? '+' : '−';
  $('stopPx').textContent=f(r.stop); $('stopSub').textContent=sg+f(r.R)+' $ · '+sg+pct(r.stopDist);
  $('tpPx').textContent=f(r.tp); $('tpSub').textContent=sg2+f(2*r.R)+' $ · '+sg2+pct(2*r.stopDist);
  ladder(entry, r);
  const rows=[['ATR(14), günlük', f(rec.atr)+' $'], ['Risk (1R = 2×ATR)', f(r.R)+' $ · '+pct(r.stopDist)], ['Hedef (2R = 4×ATR)', f(2*r.R)+' $ · '+pct(2*r.stopDist)], ['Veri', (rec.exchange||'ABD')+' · '+rec.bar_date_tr]];
  const dl=$('rows'); dl.textContent=''; for(const [k,v] of rows){ const d=document.createElement('div'); d.className='row'; const dt=document.createElement('dt'); dt.textContent=k; const dd=document.createElement('dd'); dd.textContent=v; d.append(dt,dd); dl.appendChild(d); }
  const st=$('status'); st.dataset.s=r.state; const vn = venue==='bybit'?'Bybit':'Binance';
  if(r.liq==null){ $('stTitle').textContent='Kaldıraçsız: tasfiye yok'; $('stText').textContent='Stop girişin '+pct(r.stopDist)+' uzağında.'; }
  else if(r.state==='crit'){ $('stTitle').textContent='Bu kaldıraçta tasfiye stoptan önce gelir'; $('stText').textContent='Tasfiye '+pct(r.liqDist)+' uzakta ('+f(r.liq)+' $), stop '+pct(r.stopDist)+' uzakta. '+(r.maxLev>1?'En fazla '+r.maxLev+' kat kullan.':'Bu stop mesafesiyle kaldıraçsız aç.'); }
  else if(r.state==='warn'){ $('stTitle').textContent='Stop tasfiyeye çok yakın'; $('stText').textContent='Tasfiye '+pct(r.liqDist)+' uzakta ('+f(r.liq)+' $), stop '+pct(r.stopDist)+' uzakta. '+(r.maxLev>1?'Güvenli sınır '+r.maxLev+' kat.':'Güvenli olan kaldıraçsız işlem.'); }
  else { $('stTitle').textContent='Stop tasfiyeden önce çalışır'; $('stText').textContent='Tasfiye '+pct(r.liqDist)+' uzakta ('+f(r.liq)+' $), stop '+pct(r.stopDist)+' uzakta. Stopta teminatın '+pct(Math.min(1,r.stopDist*L),0)+' kadarı gider. '+vn+' bakım teminatı '+pct(MMR[venue])+' varsayıldı.'; }
  try{ localStorage.setItem('atrstop', JSON.stringify({l:el.lev.value, v:el.venue.value})); }catch(e){}
}

function setDir(d){ dir=d; $('dirLong').setAttribute('aria-pressed', d===1); $('dirShort').setAttribute('aria-pressed', d===-1); render(); }
function onTicker(now){ const t=normT(el.ticker.value); clearTimeout(timer); if(!t){ rec=null; recFor=''; setInfo("Hisse kodunu yaz; ATR'yi borsadaki günlük grafikten ben çekerim."); render(); return; } if(t===recFor && rec) return; if(!/^[A-Z][A-Z0-9-]{0,6}$/.test(t)){ setInfo('Hisse kodu harflerden oluşmalı (örnek: QCOM, NVDA, BRK-B).', true); return; } timer=setTimeout(()=>load(t), now?0:700); }

el.ticker.addEventListener('input', ()=>onTicker(false));
el.ticker.addEventListener('change', ()=>onTicker(true));
el.ticker.addEventListener('keydown', e=>{ if(e.key==='Enter'){ onTicker(true); el.entry.focus(); } });
el.entry.addEventListener('input', render);
el.lev.addEventListener('change', render); el.venue.addEventListener('change', render);
$('dirLong').addEventListener('click', ()=>setDir(1)); $('dirShort').addEventListener('click', ()=>setDir(-1));
$('copy').addEventListener('click', ()=>{ const entry=num(el.entry.value); const r=rec?calc(entry,rec.atr,dir,parseInt(el.lev.value,10),el.venue.value):null; if(!r||r.bad) return;
  const t=rec.ticker+' '+(dir===1?'long':'short')+' | giriş '+f(entry)+' | stop '+f(r.stop)+' | kâr al '+f(r.tp)+' | ATR '+f(rec.atr);
  const done=()=>{ $('copied').textContent='Kopyalandı'; setTimeout(()=>{$('copied').textContent='';},2500); };
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(t).then(done).catch(()=>{ $('copied').textContent=t; }); } else { $('copied').textContent=t; } });
try{ const s=JSON.parse(localStorage.getItem('atrstop')||'null'); if(s){ if(s.l) el.lev.value=s.l; if(s.v) el.venue.value=s.v; } }catch(e){}
</script>
</body></html>
"""
ICON = base64.b64decode("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")
MANIFEST = json.dumps({"name": "ATR Stop", "short_name": "ATR Stop", "start_url": ".", "display": "standalone",
                       "background_color": "#0e1412", "theme_color": "#0d6b5a",
                       "icons": [{"src": "icon.png", "sizes": "192x192", "type": "image/png", "purpose": "any"}]})


class H(BaseHTTPRequestHandler):
    server_version = "atrstop/1"

    def _send(self, code, body, ctype, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); p = u.path.rstrip("/") or "/"
        if p == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if p == "/icon.png":
            return self._send(200, ICON, "image/png", "max-age=86400")
        if p == "/manifest.json":
            return self._send(200, MANIFEST, "application/manifest+json", "max-age=3600")
        if p == "/api/atr":
            t = (parse_qs(u.query).get("ticker") or [""])[0].upper().strip().replace(".", "-")
            if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,6}", t):
                return self._send(400, json.dumps({"ok": False, "error": "Geçersiz hisse kodu."}), "application/json; charset=utf-8")
            try:
                out = get_atr(t)
            except Exception as e:
                out = {"ok": False, "error": "Sunucu hatası: %s" % str(e)[:160]}
            return self._send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
        self._send(404, "bulunamadi", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8105)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--test", metavar="HISSE", help="sunucuyu baslatmadan tek hisse icin sonucu yaz")
    a = ap.parse_args()
    if a.test:
        print(json.dumps(get_atr(a.test.upper()), ensure_ascii=False, indent=1)); return
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print("ATR Stop calisiyor: http://%s:%d" % (a.host, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
