#!/usr/bin/env python3
"""ATR Stop - telefon icin web uygulamasi (tek dosya, ek paket gerekmez).  Surum 10.

Sekmeler: ABD taramasi | BIST 50 taramasi | Hesaplayici | Portfoy (uyarilar) | Pyramid (kagit defteri).

Hisse kodu + giris fiyati + yon -> 2 x ATR(14) stop ve 2R kar al.
ATR, hissenin ABD borsasindaki (NASDAQ/NYSE) gunluk barlarindan hesaplanir; veri her istekte canli cekilir.

Calistirma:   python3 atr_stop.py --port 8105
Telefonda:    http://SUNUCU_IP:8105  ->  tarayici menusu -> "Ana ekrana ekle"
Veri kaynagi sirasi: Tiingo (anahtar ~/.atr_env icinde TIINGO_API_KEY=... ise) -> Nasdaq -> Yahoo -> Stooq.
Anahtari otomatik bulmak icin:  python3 atr_stop.py --find-key /home/ubuntu/tce-superapp
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


def http_get(url, timeout=12, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json,text/csv,*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def ny_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=4)


def drop_partial(bars):
    """Bugunun bari seans kapanmadan gelmisse at (yarim bar)."""
    n = ny_now()
    if bars and bars[-1][0] == n.strftime("%Y-%m-%d") and (n.hour, n.minute) < (16, 5):
        return bars[:-1]
    return bars


def load_env_file():
    """~/.atr_env icindeki ANAHTAR=deger satirlarini ortam degiskeni yap (varsa)."""
    p = os.path.expanduser("~/.atr_env")
    try:
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def tiingo_key():
    return os.environ.get("TIINGO_API_KEY") or os.environ.get("TIINGO_TOKEN") or os.environ.get("TIINGO_KEY")


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


META = {}


def from_tiingo(t, key):
    start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
    hd = {"Authorization": "Token " + key}
    try:
        raw = http_get("https://api.tiingo.com/tiingo/daily/%s/prices?startDate=%s" % (quote(t), start), headers=hd)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError("bulunamadi")
        raise
    j = json.loads(raw)
    if not isinstance(j, list) or not j:
        raise LookupError("bulunamadi")
    # bolunmeye gore duzelt (temettuye gore degil): TradingView grafigiyle ayni fiyatlar
    bars = []; f = 1.0
    for x in reversed(j):
        o, h, l, c = x.get("open"), x.get("high"), x.get("low"), x.get("close")
        if None not in (o, h, l, c) and min(o, h, l, c) > 0:
            bars.append((str(x["date"])[:10], o / f, h / f, l / f, c / f))
        sf = x.get("splitFactor") or 1.0
        if sf and sf != 1.0:
            f *= float(sf)
    bars.reverse()
    bars = drop_partial(bars)
    name = ""; exch = "ABD"; price = None
    m = META.get(t)
    if not m or time.time() - m[0] > 86400:
        try:
            mj = json.loads(http_get("https://api.tiingo.com/tiingo/daily/%s" % quote(t), headers=hd))
            m = (time.time(), mj.get("name") or "", mj.get("exchangeCode") or "ABD"); META[t] = m
        except Exception:
            m = None
    if m:
        name, exch = m[1], m[2]
    try:
        ij = json.loads(http_get("https://api.tiingo.com/iex/?tickers=%s" % quote(t), headers=hd))
        if isinstance(ij, list) and ij:
            price = ij[0].get("tngoLast") or ij[0].get("last") or ij[0].get("prevClose")
    except Exception:
        pass
    return {"bars": bars, "name": name, "exchange": exch, "price": float(price) if price else None, "type": "", "currency": "USD", "source": "Tiingo"}


NASDAQ_HD = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def nasdaq_hist(t, cls="stocks", timeout=10):
    """Nasdaq'tan gunluk barlar (eskiden yeniye). Veri yoksa LookupError."""
    end = datetime.now(timezone.utc); start = end - timedelta(days=400)
    raw = http_get("https://api.nasdaq.com/api/quote/%s/historical?assetclass=%s&fromdate=%s&todate=%s&limit=9999" % (quote(t.replace("-", ".")), cls, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")), timeout=timeout, headers=NASDAQ_HD)
    rows = (((json.loads(raw).get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    bars = []
    for x in rows:
        try:
            mm, dd, yy = x["date"].split("/")
            v = [float(str(x[k]).replace("$", "").replace(",", "")) for k in ("open", "high", "low", "close")]
            if min(v) > 0:
                bars.append(("%s-%s-%s" % (yy, mm, dd), v[0], v[1], v[2], v[3]))
        except Exception:
            pass
    if not bars:
        raise LookupError("bulunamadi")
    bars.sort()
    return drop_partial(bars)


def _num(x):
    try:
        v = float(str(x).replace("$", "").replace(",", "").strip())
        return v if v > 0 else None
    except Exception:
        return None


def _asof_date(txt):
    txt = str(txt or "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    m = re.search(r"([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})", txt)
    if m:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime("%s %s %s" % (m.group(1), m.group(2), m.group(3)), fmt).strftime("%Y-%m-%d")
            except Exception:
                pass
    return None


def nasdaq_today(t, cls="stocks"):
    """Suren ya da son NORMAL seansin (09:30-16:00 New York) bari, Nasdaq'in dakikalik grafik verisinden.
    Seans oncesi ve sonrasi islemler sayilmaz. Alinamazsa None."""
    j = json.loads(http_get("https://api.nasdaq.com/api/quote/%s/chart?assetclass=%s" % (quote(t.replace("-", ".")), cls), timeout=8, headers=NASDAQ_HD))
    d = j.get("data") or {}
    reg = []
    for p in d.get("chart") or []:
        y = _num(p.get("y"))
        if y is None:
            y = _num((p.get("z") or {}).get("value"))
        x = p.get("x")
        if y is None or not isinstance(x, (int, float)):
            continue
        dt = datetime.fromtimestamp(x / 1000.0, tz=timezone.utc)     # Nasdaq'in x degeri: New York duvar saati, UTC gibi yazilmis
        mins = dt.hour * 60 + dt.minute
        if 570 <= mins <= 960:
            reg.append((dt.strftime("%Y-%m-%d"), mins, y))
    if not reg:
        return None
    date = reg[-1][0]
    reg = [r for r in reg if r[0] == date]
    ys = [r[2] for r in reg]
    return {"date": date, "o": ys[0], "h": max(ys), "l": min(ys), "c": ys[-1], "time": "%02d:%02d" % divmod(reg[-1][1], 60), "n": len(ys)}


def session_open():
    n = ny_now()
    return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)


def from_nasdaq(t):
    last = None
    for cls in ("stocks", "etf"):
        try:
            bars = nasdaq_hist(t, cls)
        except Exception as e:
            last = e
            continue
        name = ""; exch = "ABD borsası"; price = None
        try:    # sirket adi, borsa ve son fiyat (olmazsa sessizce gec)
            info = (json.loads(http_get("https://api.nasdaq.com/api/quote/%s/info?assetclass=%s" % (quote(t.replace("-", ".")), cls), timeout=8, headers=NASDAQ_HD)).get("data") or {})
            name = re.sub(r"\s+(Common Stock|Class [A-Z] Common Stock|Ordinary Shares|American Depositary Shares).*$", "", info.get("companyName") or "").strip()
            ex = (info.get("exchange") or "").upper()
            exch = "NASDAQ" if ex.startswith("NASDAQ") else ("NYSE" if ex.startswith("NYSE") else (ex or exch))
            lp = str(((info.get("primaryData") or {}).get("lastSalePrice")) or "").replace("$", "").replace(",", "")
            price = float(lp) if lp and float(lp) > 0 else None
            rng = [_num(v) for v in re.split(r"\s*-\s*", str(((info.get("keyStats") or {}).get("dayrange") or {}).get("value") or ""))]
            rng = sorted(v for v in rng if v) if len(rng) == 2 and all(rng) else None
        except Exception:
            rng = None
        partial = None; ptime = ""
        try:    # bugunun (suren) bari: gecmiste henuz yoksa ATR'ye eklenir
            td = nasdaq_today(t, cls)
            if td and td["date"] > bars[-1][0]:
                h, l, c = td["h"], td["l"], td["c"]
                if session_open() and price and l * 0.9 <= price <= h * 1.1:
                    c = price                                   # seans icindeyken en guncel fiyat
                if rng and rng[0] >= l * 0.9 and rng[1] <= h * 1.1:
                    l = min(l, rng[0]); h = max(h, rng[1])       # Nasdaq'in gun ici en dusuk/en yuksek degeri (dakikalik veriden daha kesin)
                h = max(h, c); l = min(l, c)
                partial = (td["date"], td["o"], h, l, c); ptime = td["time"]
                price = c
            elif td and price is None:
                price = td["c"]
        except Exception:
            pass
        return {"bars": bars, "partial": partial, "partial_time": ptime, "name": name, "exchange": exch, "price": price, "live": price is not None, "type": "", "currency": "USD", "source": "Nasdaq"}
    if isinstance(last, LookupError):
        raise last
    raise RuntimeError(str(last))


def from_stooq(t):
    raw = http_get("https://stooq.com/q/d/l/?s=%s.us&i=d" % quote(t.lower()), timeout=8)
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows or "Close" not in rows[0]:
        raise LookupError("bulunamadi")
    bars = []
    for x in rows[-260:]:
        try:
            bars.append((x["Date"], float(x["Open"]), float(x["High"]), float(x["Low"]), float(x["Close"])))
        except Exception:
            pass
    return {"bars": drop_partial(bars), "name": "", "exchange": "ABD", "price": None, "type": "", "currency": "USD", "source": "Stooq"}


def source_list(t):
    out = []
    key = tiingo_key()
    if key:
        out.append(("Tiingo", lambda: from_tiingo(t, key)))
    out += [("Nasdaq", lambda: from_nasdaq(t)), ("Yahoo", lambda: from_yahoo(t)), ("Stooq", lambda: from_stooq(t))]
    return out


def find_key(root):
    """Sunucudaki dosyalarda ve calisan servislerde Tiingo anahtarini arar, dener, ~/.atr_env'e yazar. Anahtari ekrana basmaz."""
    cands = []
    def add(x):
        if x and x not in cands:
            cands.append(x)
    rx = re.compile(r"\b[a-f0-9]{40}\b")
    for k in ("TIINGO_API_KEY", "TIINGO_TOKEN", "TIINGO_KEY"):
        add(os.environ.get(k))
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if pid.isdigit():
            try:
                env = open("/proc/%s/environ" % pid, "rb").read().decode("utf-8", "replace").split("\0")
                for e in env:
                    if "tiingo" in e.lower():
                        for m in rx.findall(e): add(m)
            except Exception:
                pass
    exts = (".py", ".env", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt", ".service", ".sh", ".conf")
    for base in [root, "/etc/systemd/system"]:
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in (".git", "node_modules", "__pycache__", "venv", ".venv", "btvenv", "site-packages")]
            for f in fn:
                if not (f.endswith(exts) or f.startswith(".env")):
                    continue
                p = os.path.join(dp, f)
                try:
                    if os.path.getsize(p) > 2_000_000:
                        continue
                    txt = open(p, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                if "tiingo" not in txt.lower():
                    continue
                lines = txt.splitlines()
                for i, line in enumerate(lines):
                    if "tiingo" in line.lower():
                        for m in rx.findall(" ".join(lines[max(0, i - 1):i + 3])): add(m)
                for m in rx.findall(txt): add(m)
    print("aday anahtar sayisi:", len(cands))
    for c in cands:
        try:
            r = json.loads(http_get("https://api.tiingo.com/api/test", headers={"Authorization": "Token " + c}))
            if "success" in str(r.get("message", "")).lower():
                p = os.path.expanduser("~/.atr_env")
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("TIINGO_API_KEY=%s\n" % c)
                os.chmod(p, 0o600)
                print("Tiingo anahtari bulundu (%s...%s), calisiyor ve %s dosyasina kaydedildi." % (c[:3], c[-2:], p))
                return True
        except Exception as e:
            print("  aday calismadi:", str(e)[:80])
    print("Calisan bir Tiingo anahtari bulunamadi.")
    return False


def get_atr(t):
    now = time.time()
    with LOCK:
        hit = CACHE.get(t)
        if hit and now - hit[0] < (60 if hit[1].get("atr_live") else TTL):
            return hit[1]
    errors = []; d = None; missing = False
    sources = source_list(t)
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
    bars = d["bars"]; atr_closed = wilder_atr(bars); atr = atr_closed
    part = d.get("partial")
    if part:
        atr = wilder_atr(bars + [part])      # bugunun bari dahil: TradingView'de o an gorunen ATR
    y, m, dd = bars[-1][0].split("-")
    out = {"ok": True, "ticker": t, "name": d["name"], "exchange": d["exchange"], "atr": round(atr, 4),
           "atr_closed": round(atr_closed, 4), "atr_live": bool(part), "session_open": bool(part) and session_open(),
           "live_time": d.get("partial_time") or "", "live_date_tr": tr_date(part[0]) if part else "",
           "today": {"o": part[1], "h": part[2], "l": part[3], "c": part[4]} if part else None,
           "close": bars[-1][4], "price": d["price"] or bars[-1][4], "bar_date": bars[-1][0],
           "bar_date_tr": "%d %s %s" % (int(dd), AYLAR_TR[int(m) - 1], y), "n_bars": len(bars), "source": d["source"],
           "price_live": bool(d.get("live") or (d["source"] in ("Yahoo", "Tiingo") and d["price"]))}
    if d.get("currency") and d["currency"] != "USD":
        out = {"ok": False, "error": "“%s” dolar cinsinden işlem görmüyor; yalnız ABD hisseleri desteklenir." % t}
    with LOCK:
        CACHE[t] = (now, out)
    return out



# ============================ TARAMA ============================
# Sinyal (testteki kuralla ayni): gunluk kapanis, onceki 55 gunun en yuksegini yukari keser. Yalniz long.
# Evren: Bybit ve Binance'teki ABD hisse kontratlarindan gunluk hacmi yeterli olanlar; her gun yenilenir.
N_BREAK = 55
MIN_VOL = 50000.0          # kontratin 30 gunluk medyan gunluk hacmi (USDT)
MAX_N = 150                # taranacak hisse sayisi: hacme gore en likit ilk 150 gecerli ABD hissesi
SKIP_DAYS = 7              # ABD hissesi olmadigi anlasilan kontratlar (ETF, yabanci hisse) bu kadar gun yeniden denenmez
LOOKBACK_SIGNALS = 3       # son 3 tamamlanmis barda verilen sinyaller listelenir
SCAN_FILE = os.path.expanduser("~/.atr_scan.json")
UNI_FILE = os.path.expanduser("~/.atr_universe.json")
SCAN = {"status": "bekliyor", "progress": [0, 0], "result": None, "started": 0.0, "error": ""}
SCAN_LOCK = threading.Lock()


def tr_date(d):
    y, m, dd = d.split("-")
    return "%d %s %s" % (int(dd), AYLAR_TR[int(m) - 1], y)


def median(xs):
    xs = sorted(xs); n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def norm_base(b):
    b = str(b).upper()
    if b.endswith("STOCK") and len(b) > 5:
        b = b[:-5]
    return {"BRKB": "BRK-B", "BRKA": "BRK-A", "BFB": "BF-B"}.get(b, b)


def bybit_universe():
    rows = []; cursor = ""
    while True:
        j = json.loads(http_get("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000" + ("&cursor=" + quote(cursor) if cursor else "")))
        if j.get("retCode") != 0:
            raise RuntimeError("Bybit: %s" % j.get("retMsg"))
        rows += j["result"]["list"]; cursor = j["result"].get("nextPageCursor") or ""
        if not cursor:
            break
    out = {}
    for s in rows:
        if str(s.get("symbolType")).lower() != "stock" or s.get("status") != "Trading" or s.get("quoteCoin") != "USDT":
            continue
        try:
            k = json.loads(http_get("https://api.bybit.com/v5/market/kline?category=linear&symbol=%s&interval=D&limit=31" % s["symbol"]))
            lst = k["result"]["list"]            # yeniden eskiye: [baslangic, o, h, l, c, hacim, ciro]
            vols = [float(x[6]) for x in lst[1:]]
            if vols:
                out[norm_base(s["baseCoin"])] = {"sym": s["symbol"], "vol": median(vols), "last": float(lst[0][4])}
        except Exception:
            pass
        time.sleep(0.05)
    return out


def binance_universe():
    info = json.loads(http_get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20))
    out = {}
    for s in info.get("symbols", []):
        if s.get("underlyingType") != "EQUITY" or s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT" or "PERPETUAL" not in str(s.get("contractType")):
            continue
        try:
            k = json.loads(http_get("https://fapi.binance.com/fapi/v1/klines?symbol=%s&interval=1d&limit=31" % s["symbol"]))
            vols = [float(x[7]) for x in k[:-1]]  # eskiden yeniye: [..., c, hacim, kapanis zamani, ciro, ...]
            if vols:
                out[norm_base(s["baseAsset"])] = {"sym": s["symbol"], "vol": median(vols), "last": float(k[-1][4])}
        except Exception:
            pass
        time.sleep(0.05)
    return out


def build_universe(force=False):
    """{hisse: {"bybit": {...}, "binance": {...}}}; gunde bir yenilenir, dosyada saklanir."""
    try:
        saved = json.load(open(UNI_FILE, encoding="utf-8"))
        if not force and time.time() - saved.get("ts", 0) < 20 * 3600 and saved.get("tickers"):
            return saved
    except Exception:
        saved = None
    uni = {}; notes = []
    for name, fn in (("bybit", bybit_universe), ("binance", binance_universe)):
        try:
            d = fn()
            notes.append("%s: %d kontrat" % (name, len(d)))
            for t, v in d.items():
                uni.setdefault(t, {})[name] = v
        except Exception as e:
            notes.append("%s: HATA %s" % (name, str(e)[:80]))
    uni = {t: v for t, v in uni.items() if max(x["vol"] for x in v.values()) >= MIN_VOL}
    if not uni and saved and saved.get("tickers"):
        saved["notes"] = notes + ["eski liste kullanildi"]
        return saved
    out = {"ts": time.time(), "tickers": uni, "notes": notes, "skip": (saved or {}).get("skip") or {}}
    try:
        json.dump(out, open(UNI_FILE, "w", encoding="utf-8"))
    except Exception:
        pass
    return out


def find_signal(bars):
    """Son LOOKBACK_SIGNALS bar icindeki en yeni long kirilim. Yoksa None."""
    n = len(bars)
    if n < N_BREAK + LOOKBACK_SIGNALS + 2:
        return None
    hi = [b[2] for b in bars]; cl = [b[4] for b in bars]
    for age in range(LOOKBACK_SIGNALS):
        i = n - 1 - age
        up_now = max(hi[i - N_BREAK:i]); up_prev = max(hi[i - 1 - N_BREAK:i - 1])
        if cl[i] > up_now and cl[i - 1] <= up_prev:
            atr = wilder_atr(bars[:i + 1])
            return {"age": age, "date": bars[i][0], "close": cl[i], "atr": round(atr, 4), "hi55": up_now, "last_close": cl[-1]}
    return None


def scan_one(t, venues):
    bars = nasdaq_hist(t, "stocks", timeout=12)
    close = bars[-1][4]
    ok = {k: v for k, v in venues.items() if v.get("last") and abs(v["last"] / close - 1) <= 0.25}
    if not ok:
        return ("uyusmuyor", bars[-1][0], None)      # kontrat bu hisse degil
    sig = find_signal(bars)
    if sig:
        sig.update({"ticker": t, "date_tr": tr_date(sig["date"]),
                    "venues": {k: {"sym": v["sym"], "vol": round(v["vol"])} for k, v in ok.items()}})
    return ("tamam", bars[-1][0], sig)


def run_scan(limit=None, verbose=False):
    from concurrent.futures import ThreadPoolExecutor
    with SCAN_LOCK:
        if SCAN["status"] == "taraniyor":
            return SCAN["result"]
        SCAN.update(status="taraniyor", progress=[0, 0], started=time.time(), error="")
    try:
        uni = build_universe()
        vol = {t: max(x["vol"] for x in v.values()) for t, v in uni["tickers"].items()}
        skip = {t for t, ts in (uni.get("skip") or {}).items() if time.time() - ts < SKIP_DAYS * 86400}
        tickers = sorted((t for t in uni["tickers"] if t not in skip), key=lambda t: -vol[t])
        if limit:
            tickers = tickers[:limit]
        SCAN["progress"] = [0, len(tickers)]
        signals = []; dates = {}; failed = 0; mismatch = 0; nodata = 0; valid = []; newskip = {}

        def work(t):
            for attempt in range(2):
                try:
                    return t, scan_one(t, uni["tickers"][t])
                except LookupError:
                    return t, ("veri yok", None, None)
                except Exception as e:
                    if attempt == 1:
                        return t, ("hata", None, str(e)[:80])
                    time.sleep(1.5)

        with ThreadPoolExecutor(max_workers=2) as ex:
            for t, (st, d, sig) in ex.map(work, tickers):
                SCAN["progress"][0] += 1
                if st == "tamam":
                    dates[d] = dates.get(d, 0) + 1; valid.append(t)
                    if sig:
                        signals.append(sig)
                elif st == "uyusmuyor":
                    mismatch += 1; newskip[t] = time.time()
                elif st == "veri yok":
                    nodata += 1; newskip[t] = time.time()
                else:
                    failed += 1
                if verbose and SCAN["progress"][0] % 20 == 0:
                    print("  tarandi: %d/%d" % tuple(SCAN["progress"]), flush=True)
        bar_date = max(dates, key=dates.get) if dates else ""
        top = set(sorted(valid, key=lambda t: -vol[t])[:MAX_N])       # en likit ilk MAX_N gecerli hisse
        signals = [x for x in signals if x["ticker"] in top]
        if newskip and not limit and len(valid) >= 30:      # tarama genel olarak calistiysa (gecici veri kesintisinde listeyi bozma)
            try:
                u2 = json.load(open(UNI_FILE, encoding="utf-8")); sk = u2.get("skip") or {}; sk.update(newskip); u2["skip"] = sk
                json.dump(u2, open(UNI_FILE, "w", encoding="utf-8"))
            except Exception:
                pass
        signals = [x for x in signals if (datetime.strptime(bar_date, "%Y-%m-%d") - datetime.strptime(x["date"], "%Y-%m-%d")).days <= 7] if bar_date else []
        signals.sort(key=lambda x: (x["age"], -max(v["vol"] for v in x["venues"].values())))
        res = {"bar_date": bar_date, "bar_date_tr": tr_date(bar_date) if bar_date else "", "updated": int(time.time()),
               "universe_n": len(tickers), "scanned_n": len(top), "failed_n": failed, "nodata_n": nodata, "mismatch_n": mismatch,
               "signals": signals, "notes": uni.get("notes", [])}
        if not limit and res["scanned_n"]:
            SCAN["result"] = res
            try:
                json.dump(res, open(SCAN_FILE, "w", encoding="utf-8"))
            except Exception:
                pass
        SCAN["status"] = "hazir"
        return res
    except Exception as e:
        SCAN.update(status="hata", error=str(e)[:160])
        return None


def scan_loop():
    """Arka planda: Nasdaq'ta yeni gunun bari cikinca taramayi calistirir. 20 dakikada bir bakar."""
    time.sleep(3)
    while True:
        try:
            latest = nasdaq_hist("AAPL", "stocks")[-1][0]
            res = SCAN["result"]
            uni_old = True
            try:
                uni_old = time.time() - json.load(open(UNI_FILE, encoding="utf-8")).get("ts", 0) > 20 * 3600
            except Exception:
                pass
            if not res or res.get("bar_date", "") < latest:
                run_scan()
            elif uni_old and time.time() - res.get("updated", 0) > 20 * 3600:
                run_scan()
        except Exception as e:
            sys.stderr.write("tarama dongusu: %s\n" % str(e)[:120])
        time.sleep(1200)


# ====================================================================================================
# Donchian 55/20 Pyramid - kagit takibi (uc piyasa, ayri defter). Kural (dondurulmus):
#   Ilk giris : gun ici fiyat onceki gunun 55 gunluk tepesini astigi anda; dolum = max(tepe, acilis).
#   Parca     : defter ozkaynaginin %0,5'i nominal; gunde en cok bir parca; en cok 4 parca (%2).
#   Ekleme    : pozisyon acikken yeni 55 gunluk tepe yapilan her gun bir parca (duraklama sarti yok).
#   Stop      : ilk giris - 2 x ATR14 (bir onceki gunun ATR'si); sonradan degismez; tum pozisyon icin.
#   Cikis     : gun ici fiyat onceki gunun 20 gunluk dibine degdigi anda tum pozisyon; stop daha yukaridaysa stoptan.
#   Gunluk barla gun ici sira bilinemez -> kotumser: ayni gun once ekleme, sonra cikis; giris gunu stop tetiklenirse stop.
# ====================================================================================================
PYR_STATE = os.path.expanduser("~/.pyr_state.json")
PYR_OUT = os.path.expanduser("~/.pyr_sonuc.json")
PYR_LOG = os.path.expanduser("~/.pyr_olay.csv")
PYR_BIST_FILE = os.path.expanduser("~/.pyr_bist.csv")        # istege bagli elle veri (ticker,date,open,high,low,close,volume)
PYR_FAIZ_FILE = os.path.expanduser("~/.pyr_faiz.csv")        # istege bagli: tarih,yuzde satirlari (TCMB degisirse)
PYR_PARCA = 0.005; PYR_MAXP = 4
PYR_GERI = 500                      # kayit baslangicindan once kac takvim gunu veri okunur (sabit capa: hesap her gun ayni bardan baslar)
PYR_KRIPTO_MIN = 50
PYR_COST = {"us": 0.0005, "bist": 0.0010, "kripto": 0.0013}
PYR_AD = {"bist": "BIST 38", "us": "ABD likit 50", "kripto": "Kripto ilk 20"}
PYR_BIST = ("THYAO GARAN AKBNK ISCTR YKBNK VAKBN HALKB SISE EREGL KCHOL SAHOL TUPRS BIMAS ASELS FROTO TOASO PGSUS TCELL TTKOM PETKM "
            "ARCLK SASA ENKAI TAVHL MGROS DOHOL VESTL EKGYO OYAKC ALARK KRDMD TKFEN CIMSA AKSEN ULKER SOKM AEFES BRSAN").split()
PYR_US50 = ("MU NVDA AAPL TSLA AMD MSFT META INTC AMZN GOOGL AVGO GOOG PLTR MRVL ORCL STX LITE AMAT WDC DELL LLY LRCX NFLX WMT CRM JPM V XOM PANW HOOD "
            "BRK-B CAT CSCO APP NOW COST QCOM KLAC GS BAC CRWD UNH JNJ TXN COHR MA CVX GLW IBM C").split()          # 2026-10-01 ceyregi (dondurulmus uyelik tablosu)
PYR_US245 = ("AAPL ABBV ABNB ABT ACN ADBE ADI ADP ADSK AEP AJG AKAM ALL AMAT AMD AMGN AMT AMZN ANET AON APD APH APO APP AVGO AXON AXP AZO BA BAC BKNG BKR BLK BMY BNY BRK-B BSX BX C CAH CAT CB "
             "CCL CDNS CHTR CI CIEN CL CMCSA CME CMG CMI COF COHR COIN COP COR COST CRH CRM CRWD CSCO CSX CTAS CTSH CVNA CVS CVX DAL DASH DDOG DE DELL DHR DIS DLR DLTR DUK DVN ECHO ELV EOG EQIX "
             "EQT ETN EW EXC F FAST FCX FDX FIX FLEX FTNT GD GE GILD GIS GLW GM GOOG GOOGL GS GWW HAL HBAN HCA HD HLT HON HOOD HPE HUM HWM IBKR IBM ICE INTC INTU ISRG JBL JCI JNJ JPM KDP KEYS KLAC "
             "KMB KO KR LIN LITE LLY LMT LOW LRCX MA MAR MCD MCHP MCK MCO MDLZ MDT META MMM MNST MO MPC MPWR MRK MRNA MRSH MRVL MS MSCI MSFT MU NDAQ NEE NEM NFLX NKE NOC NOW NTAP NVDA NXPI ON ORCL "
             "ORLY OXY PANW PCAR PEP PFE PG PGR PH PLD PLTR PM PNC PSX PWR PYPL QCOM RCL REGN ROK ROST RTX SBUX SCHW SHW SLB SMCI SNPS SO SPG SPGI STT STX SYK T TDG TECH TEL TER TFC TGT TJX TMO "
             "TMUS TRV TSLA TT TTWO TXN UAL UBER UNH UNP UPS URI USB V VLO VRT VRTX VST VZ WBD WDAY WDC WELL WFC WM WMB WMT XEL XOM XYZ YUM ZTS").split()
PYR_KRIPTO_ESKI = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "HYPEUSDT", "DOGEUSDT", "ZECUSDT", "1000PEPEUSDT", "ADAUSDT", "SUIUSDT", "NEARUSDT", "TAOUSDT", "XAUTUSDT",
                   "FARTCOINUSDT", "ENAUSDT", "LABUSDT", "WLDUSDT", "LINKUSDT", "ONDOUSDT", "AVAXUSDT"]                  # 2026-07-01 ceyregi (yedek)
PYR_STABLE = {"USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "PYUSD", "USDP"}
PYR_FAIZ = [("2016-01-01", 8.0), ("2017-01-16", 10.0), ("2017-03-17", 11.5), ("2017-04-27", 12.0), ("2017-12-15", 12.75), ("2018-04-26", 13.5), ("2018-05-24", 16.5), ("2018-06-08", 17.75),
            ("2018-09-14", 24.0), ("2019-07-26", 19.75), ("2019-09-13", 16.5), ("2019-10-25", 14.0), ("2019-12-13", 12.0), ("2020-01-17", 11.25), ("2020-02-20", 10.75), ("2020-03-18", 9.75),
            ("2020-04-23", 8.75), ("2020-05-22", 8.25), ("2020-08-10", 10.0), ("2020-09-25", 11.5), ("2020-10-23", 13.5), ("2020-11-20", 15.0), ("2020-12-25", 17.0), ("2021-03-19", 19.0),
            ("2021-09-24", 18.0), ("2021-10-22", 16.0), ("2021-11-19", 15.0), ("2021-12-17", 14.0), ("2022-08-19", 13.0), ("2022-09-23", 12.0), ("2022-10-21", 10.5), ("2022-11-25", 9.0),
            ("2023-02-24", 8.5), ("2023-06-23", 15.0), ("2023-07-21", 17.5), ("2023-08-25", 25.0), ("2023-09-22", 30.0), ("2023-10-27", 35.0), ("2023-11-24", 40.0), ("2023-12-22", 42.5),
            ("2024-01-26", 45.0), ("2024-03-22", 50.0), ("2024-12-27", 47.5), ("2025-01-24", 45.0), ("2025-03-07", 42.5), ("2025-03-21", 46.0), ("2025-07-25", 43.0), ("2025-09-12", 40.5),
            ("2025-10-24", 39.5), ("2025-12-12", 38.0), ("2026-01-23", 37.0)]
PYR = {"durum": {}, "sonuc": None, "kilit": threading.Lock()}


def pyr_utc():
    return datetime.now(timezone.utc)


def pyr_ceyrek(d):
    """'YYYY-MM-DD' -> o ceyregin ilk gunu."""
    y, m = int(d[:4]), int(d[5:7]); return "%04d-%02d-01" % (y, 3 * ((m - 1) // 3) + 1)


def pyr_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def pyr_yaz(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------- veri
def pyr_nasdaq(t, days=640, bas=None):
    """ABD: Nasdaq gunluk barlar (tarih, a, y, d, k, hacim), eskiden yeniye; yarim bar atilir. bas verilirse o tarihten baslar."""
    end = pyr_utc(); start = datetime.strptime(bas, "%Y-%m-%d") if bas else end - timedelta(days=days)
    raw = http_get("https://api.nasdaq.com/api/quote/%s/historical?assetclass=stocks&fromdate=%s&todate=%s&limit=9999" % (
        quote(t.replace("-", ".")), start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")), timeout=12, headers=NASDAQ_HD)
    rows = (((json.loads(raw).get("data") or {}).get("tradesTable") or {}).get("rows")) or []; bars = []
    for x in rows:
        try:
            mm, dd, yy = x["date"].split("/")
            v = [float(str(x[k]).replace("$", "").replace(",", "")) for k in ("open", "high", "low", "close")]
            vol = float(str(x.get("volume", "0")).replace(",", "") or 0)
            if min(v) > 0: bars.append(("%s-%s-%s" % (yy, mm, dd), v[0], v[1], v[2], v[3], vol))
        except Exception:
            pass
    if not bars: raise LookupError("Nasdaq: veri yok")
    bars.sort(); return drop_partial(bars)


def pyr_bybit(sym, limit=1000, bas=None):
    """Kripto: Bybit gunluk barlar (UTC gunu); bugunun acik bari atilir. Son sutun ciro (USDT). bas verilirse o tarihe kadar geriye sayfalanir."""
    bugun = pyr_utc().strftime("%Y-%m-%d"); bars = {}; end = None
    for _ in range(8):
        j = json.loads(http_get("https://api.bybit.com/v5/market/kline?category=linear&symbol=%s&interval=D&limit=%d" % (quote(sym), limit) + ("&end=%d" % end if end else ""), timeout=12))
        if j.get("retCode") != 0: raise RuntimeError("Bybit: %s" % j.get("retMsg"))
        lst = j["result"]["list"]
        for x in lst:
            d = datetime.fromtimestamp(int(x[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if d < bugun and float(x[5]) > 0: bars[d] = (d, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[6]))
        if bas is None or len(lst) < limit: break
        ilk = min(int(x[0]) for x in lst)
        if datetime.fromtimestamp(ilk / 1000, tz=timezone.utc).strftime("%Y-%m-%d") <= bas: break
        end = ilk - 1
    out = sorted(v for d, v in bars.items() if bas is None or d >= bas)
    if not out: raise LookupError("Bybit: veri yok")
    return out


def pyr_fonlama(sym, bas_tarih):
    """Gun -> o gunun fonlama oranlari toplami (UTC). bas_tarih'ten bugune."""
    out = {}; end = None; bas_ms = int(datetime.strptime(bas_tarih, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    for _ in range(80):
        u = "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=%s&limit=200" % quote(sym) + ("&endTime=%d" % end if end else "")
        lst = (json.loads(http_get(u, timeout=12)).get("result") or {}).get("list") or []
        if not lst: break
        for x in lst:
            ts = int(x["fundingRateTimestamp"])
            if ts >= bas_ms:
                d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"); out[d] = out.get(d, 0.0) + float(x["fundingRate"])
        son = min(int(x["fundingRateTimestamp"]) for x in lst)
        if son <= bas_ms or len(lst) < 200: break
        end = son - 1
    return out


def pyr_yahoo(t, bas=None):
    """BIST: Yahoo gunluk barlar, bolunme+temettu duzeltilmis (geriye donuk testteki veriyle ayni tanim). Istanbul 18:30'dan once bugunun bari atilir."""
    hata = None
    aralik = ("period1=%d&period2=%d" % (int(datetime.strptime(bas, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()), int(pyr_utc().timestamp()) + 86400)) if bas else "range=3y"
    for host in ("query2", "query1"):
        try:
            j = json.loads(http_get("https://%s.finance.yahoo.com/v8/finance/chart/%s.IS?%s&interval=1d&events=div%%2Csplit&includeAdjustedClose=true" % (host, quote(t), aralik), timeout=12, headers=YAHOO_HD))
            r = j["chart"]["result"][0]; q = r["indicators"]["quote"][0]; adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose")
            ist = pyr_utc() + timedelta(hours=3); bugun = ist.strftime("%Y-%m-%d"); bars = []
            for i, ts in enumerate(r["timestamp"]):
                o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
                if None in (o, h, l, c) or c <= 0: continue
                d = (datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
                if d == bugun and (ist.hour, ist.minute) < (18, 30): continue
                f = (adj[i] / c) if (adj and adj[i]) else 1.0; v = v or 0
                if v <= 0 and h == l: continue
                bars.append((d, o * f, h * f, l * f, c * f, float(v)))
            if bars:
                bars.sort(); return bars
        except Exception as e:
            hata = e
    raise LookupError("Yahoo: %s" % str(hata)[:80])


def pyr_bist_dosya():
    out = {}
    try:
        for i, r in enumerate(csv.reader(open(PYR_BIST_FILE, encoding="utf-8"))):
            if i == 0 or len(r) < 7: continue
            o, h, l, c, v = (float(x) for x in r[2:7])
            if v <= 0 and h == l: continue
            out.setdefault(r[0], []).append((r[1][:10], o, h, l, c, v))
    except Exception:
        return {}
    for k in out: out[k].sort()
    return out


def pyr_deflator():
    tab = list(PYR_FAIZ)
    try:
        for r in csv.reader(open(PYR_FAIZ_FILE, encoding="utf-8")):
            if len(r) >= 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[0].strip()): tab.append((r[0].strip(), float(r[1])))
    except Exception:
        pass
    tab = sorted(dict(tab).items()); D = {}; M = 1.0; i = 0; d = datetime(2016, 1, 1); son = pyr_utc().replace(tzinfo=None) + timedelta(days=2)
    while d <= son:
        s = d.strftime("%Y-%m-%d")
        while i + 1 < len(tab) and tab[i + 1][0] <= s: i += 1
        M *= 1 + tab[i][1] / 100.0 / 365.0; D[s] = 1.0 / M; d += timedelta(days=1)
    return D, tab[-1][1]


# ---------------------------------------------------------------------------------------- evren
def pyr_evren_us(ceyrek):
    """Ceyrek basindan onceki 63 islem gununun medyan dolar hacmine gore 245 hisseden en likit 50."""
    sk = []
    for t in PYR_US245:
        try:
            b = [x for x in pyr_nasdaq(t, days=260) if x[0] < ceyrek][-63:]
            if len(b) >= 60: sk.append((-median([x[4] * x[5] for x in b]), t))
        except Exception:
            pass
        time.sleep(0.12)
    if len(sk) < 200: raise RuntimeError("yeterli hisse verisi alinamadi (%d)" % len(sk))
    sk.sort(); return [t for _, t in sk[:50]]


def pyr_evren_kripto(ceyrek):
    """Ceyrek basinda en az 180 gundur listeli USDT perp'ler; onceki 90 gunun medyan gunluk cirosu; ilk 20."""
    rows = []; cursor = ""
    while True:
        j = json.loads(http_get("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000" + ("&cursor=" + quote(cursor) if cursor else ""), timeout=15))
        if j.get("retCode") != 0: raise RuntimeError("Bybit: %s" % j.get("retMsg"))
        rows += j["result"]["list"]; cursor = j["result"].get("nextPageCursor") or ""
        if not cursor: break
    q = datetime.strptime(ceyrek, "%Y-%m-%d"); g180 = (q - timedelta(days=180)).strftime("%Y-%m-%d"); g90 = (q - timedelta(days=90)).strftime("%Y-%m-%d"); sk = []
    for s in rows:
        if s.get("quoteCoin") != "USDT" or s.get("contractType") != "LinearPerpetual" or s.get("status") != "Trading" or s.get("baseCoin") in PYR_STABLE: continue
        try:
            b = pyr_bybit(s["symbol"], 400)
            if b[0][0] > g180 or b[-1][0] < (q - timedelta(days=2)).strftime("%Y-%m-%d"): continue
            c = {x[0]: x[5] for x in b if g90 <= x[0] < ceyrek}
            v = sorted(c.get((q - timedelta(days=k)).strftime("%Y-%m-%d"), 0.0) for k in range(1, 91))
            sk.append((-(v[44] + v[45]) / 2.0, s["symbol"]))
        except Exception:
            pass
        time.sleep(0.05)
    if len(sk) < PYR_KRIPTO_MIN: raise RuntimeError("yeterli sembol verisi alinamadi (%d)" % len(sk))
    sk.sort(); return [t for _, t in sk[:20]]


def pyr_evren(m, st, bugun, uyari):
    if m == "bist": return list(PYR_BIST)
    c = pyr_ceyrek(bugun); e = (st.get("evren") or {}).get(m) or {}
    if e.get("ceyrek") == c and e.get("liste"): return e["liste"]
    yedek = e.get("liste") or (list(PYR_US50) if m == "us" else list(PYR_KRIPTO_ESKI))
    if m == "us" and c == "2026-10-01": liste = list(PYR_US50)
    else:
        dn = (st.get("evren_deneme") or {}).get(m) or {}; simdi = pyr_utc().timestamp()
        if dn.get("ceyrek") == c and simdi - dn.get("zaman", 0) < 6 * 3600:                 # ayni ceyrek icin basarisiz deneme: 6 saatte bir yinele
            uyari.append("%s listesi %s çeyreği için henüz yenilenemedi; önceki liste kullanılıyor" % (PYR_AD[m], c)); return yedek
        st.setdefault("evren_deneme", {})[m] = {"ceyrek": c, "zaman": simdi}
        try:
            liste = pyr_evren_us(c) if m == "us" else pyr_evren_kripto(c)
        except Exception as ex:
            uyari.append("%s listesi %s çeyreği için yenilenemedi (%s); önceki liste kullanıldı" % (PYR_AD[m], c, str(ex)[:60])); pyr_yaz(PYR_STATE, st)
            return yedek
    st.setdefault("evren", {})[m] = {"ceyrek": c, "liste": liste}
    st.setdefault("evren_gecmis", {}).setdefault(m, {})[c] = liste          # ceyrek -> liste; kayit boyunca saklanir
    return liste


def pyr_uygun(m, st, s):
    """Tarih -> o gun sembol evrende mi. Bilinen ilk ceyrekten onceki gunler icin ilk liste gecerli sayilir. None: hep uygun."""
    g = (st.get("evren_gecmis") or {}).get(m) or {}
    if m == "bist" or not g: return None
    ks = sorted(g); uye = [s in g[k] for k in ks]
    if all(uye): return None
    def f(d):
        c = pyr_ceyrek(d); r = uye[0]
        for k, u in zip(ks, uye):
            if k <= c: r = u
        return r
    return f


# ---------------------------------------------------------------------------------------- motor
def pyr_gosterge(bars):
    n = len(bars); h = [b[2] for b in bars]; l = [b[3] for b in bars]; c = [b[4] for b in bars]
    U = [None] * n; L = [None] * n; A = [None] * n
    for i in range(n):
        if i >= 54: U[i] = max(h[i - 54:i + 1])
        if i >= 19: L[i] = min(l[i - 19:i + 1])
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    if n >= 14:
        A[13] = sum(tr[:14]) / 14.0
        for i in range(14, n): A[i] = (13 * A[i - 1] + tr[i]) / 14.0
    return U, L, A


def pyr_oynat(bars, uygun=None):
    """Barlari bastan oynatir. Donus: kampanya listesi; her biri {parca:[(i, fiyat)], stop, x, cikis, neden}. Acik kampanyada x None.
    uygun(tarih) False ise o gun yeni kampanya baslamaz (sembol o ceyrek evrende degil); acik kampanya kendi cikisina kadar surer."""
    n = len(bars); U, L, A = pyr_gosterge(bars); K = []; j = 56
    while j <= n - 1:
        o, h = bars[j][1], bars[j][2]
        if not (U[j - 1] is not None and A[j - 1] is not None and h > U[j - 1]) or (uygun is not None and not uygun(bars[j][0])): j += 1; continue
        f0 = max(U[j - 1], o); stop = f0 - 2 * A[j - 1]; tr = [(j, f0)]; x = None; px = None; why = None
        for k in range(j, n):
            ok, hk, lk = bars[k][1], bars[k][2], bars[k][3]
            if k > j and len(tr) < PYR_MAXP and hk > U[k - 1]: tr.append((k, max(U[k - 1], ok)))
            lvl = max(stop, L[k - 1]) if L[k - 1] is not None else stop
            if lk <= lvl:
                x = k; px = lvl if k == j else min(lvl, ok); why = "stop" if lvl == stop else "dip"; break
        K.append({"parca": tr, "stop": stop, "x": x, "cikis": px, "neden": why})
        if x is None: break
        j = x + 1
    return K, (U, L, A)


def pyr_defter(m, veri, kamp, t0, D, fon):
    """Resmi defter: ilk girisi t0'dan SONRA olan kampanyalar. Parca = onceki kapanis ozkaynaginin %0,5'i. Donus: (gun listesi, ozkaynak listesi, acik nominal orani)."""
    cost = PYR_COST[m]; gunler = sorted({b[0] for s in veri for b in veri[s] if b[0] > t0}); gi = {d: i for i, d in enumerate(gunler)}; olay = {}
    for s, K in kamp.items():
        bars = veri[s]; f = fon.get(s, {})
        for k in K:
            if bars[k["parca"][0][0]][0] <= t0: continue
            xe = k["x"] if k["x"] is not None else len(bars) - 1
            for (je, fiy) in k["parca"]:
                dj = D.get(bars[je][0], 1.0) if D else 1.0; yol = []; onceki = fiy * dj
                for i in range(je, xe + 1):
                    di = D.get(bars[i][0], 1.0) if D else 1.0
                    son = (k["cikis"] if (k["x"] is not None and i == xe) else bars[i][4]) * di
                    yol.append((bars[i][0], son - onceki - f.get(bars[i][0], 0.0) * bars[i][4] * di, bars[i][4] * di)); onceki = bars[i][4] * di if not (k["x"] is not None and i == xe) else son
                cx = (k["cikis"] * (D.get(bars[xe][0], 1.0) if D else 1.0)) if k["x"] is not None else None
                olay.setdefault(bars[je][0], []).append([fiy * dj, yol, cx])
    E = 1.0; eq = []; brut = []; akt = []
    for d in gunler:
        pnl = 0.0; G = sum(a[0] * a[3] for a in akt); yeni = []
        for a in akt:
            q, yol, p = a[0], a[1], a[2]
            if p < len(yol) and yol[p][0] == d:
                pnl += q * yol[p][1]; a[3] = yol[p][2]; a[2] += 1
                if a[2] >= len(yol) and a[4] is not None:                      # kampanya kapandi (acik olan listede kalir)
                    pnl -= cost * q * a[4]; continue
            yeni.append(a)
        akt = yeni
        for ent, yol, cx in olay.get(d, []):
            q = PYR_PARCA * E / ent
            if G + q * ent > E: continue
            G += q * ent; pnl += q * yol[0][1] - cost * q * ent
            if len(yol) == 1:
                if cx is not None: pnl -= cost * q * cx
            else: akt.append([q, yol, 1, yol[0][2], cx])
        E = max(E + pnl, 1e-9); eq.append(E); brut.append(sum(a[0] * a[3] for a in akt) / E)
    return gunler, eq, brut


def pyr_sayi(x, n=6):
    return None if x is None else float(("%%.%dg" % n) % x)


def pyr_guncelle(m, sozlu=False):
    """Bir piyasayi bastan hesaplar ve sonucu kaydeder."""
    st = pyr_json(PYR_STATE, {}); uyari = []; bugun = pyr_utc().strftime("%Y-%m-%d")
    liste = pyr_evren(m, st, bugun, uyari)
    eski = {s for l in ((st.get("evren_gecmis") or {}).get(m) or {}).values() for s in l} - set(liste)      # kayit boyunca evrende bulunmus, simdi listede olmayanlar
    izlenen = list(liste) + sorted(eski); veri = {}; hatali = []; dosya = pyr_bist_dosya() if m == "bist" else {}
    bas = (st.get("bas") or {}).get(m) or (pyr_utc() - timedelta(days=PYR_GERI)).strftime("%Y-%m-%d")
    PYR["durum"][m] = {"durum": "guncelleniyor", "ilerleme": [0, len(izlenen)]}
    bkay = st.get("bist_kaynak") if m == "bist" else None
    if m == "bist" and not dosya and not bkay:                             # kaynak ilk basarili hesapta secilir ve degismez (kayit gunden gune tutarli kalsin)
        try:
            pyr_isy("THYAO", (pyr_utc() - timedelta(days=20)).strftime("%Y-%m-%d")); bkay = "isy"
        except Exception:
            bkay = "yahoo"
        st["bist_kaynak"] = bkay
    if bkay == "isy" and not dosya: uyari.append("BIST verisi İş Yatırım'dan; kaynakta açılış fiyatı yok, boşluklu açılışlarda dolum fiyatı yaklaşık")
    for i, s in enumerate(izlenen):
        for deneme in range(2):
            try:
                b = pyr_nasdaq(s, bas=bas) if m == "us" else pyr_bybit(s, bas=bas) if m == "kripto" else (dosya.get(s) or (pyr_yahoo(s, bas) if bkay == "yahoo" else pyr_isy(s, bas)))
                if len(b) >= 75: veri[s] = b
                else: hatali.append(s)
                break
            except Exception as e:
                if deneme == 1: hatali.append(s)
                else: time.sleep(1.5)
        PYR["durum"][m]["ilerleme"][0] = i + 1; time.sleep(0.25 if m == "bist" else 0.12)
        if sozlu and (i + 1) % 10 == 0: print("   %s %d/%d" % (m, i + 1, len(izlenen)), flush=True)
    if len(veri) < 0.7 * len(izlenen):
        PYR["durum"][m] = {"durum": "hata", "hata": "veri alınamadı (%d/%d sembol)" % (len(veri), len(izlenen))}; return None
    kayitli = pyr_kayitli(m); eksik = [s for s in hatali if s in kayitli]
    if eksik:                                                              # defterde islemi olan sembol eksikse defter yanlis cikar: bu turu atla, eski sonuc kalsin
        PYR["durum"][m] = {"durum": "hata", "hata": "kayıtlı işlemi olan sembolün verisi alınamadı, defter güncellenmedi: " + ", ".join(eksik[:6])}; return None
    if hatali: uyari.append("verisi alınamayan: " + ", ".join(hatali))
    son = max(b[-1][0] for b in veri.values()); geride = [s for s, b in veri.items() if b[-1][0] < son]
    if geride: uyari.append("son günü eksik: " + ", ".join(geride[:8]))
    t0 = (st.get("t0") or {}).get(m)
    if not t0:
        t0 = son; st.setdefault("t0", {})[m] = t0; st.setdefault("bas", {})[m] = bas     # kayit, bu gunden SONRAKI ilk tam kapanisla baslar
    D, faiz = pyr_deflator() if m == "bist" else (None, None); kamp = {}; gos = {}
    for s, b in veri.items(): kamp[s], gos[s] = pyr_oynat(b, pyr_uygun(m, st, s))
    fon = {}
    if m == "kripto":
        for s, K in kamp.items():
            ilk = [veri[s][k["parca"][0][0]][0] for k in K if veri[s][k["parca"][0][0]][0] > t0]
            if ilk:
                try: fon[s] = pyr_fonlama(s, min(ilk))
                except Exception: uyari.append("fonlama alınamadı: " + s)
    gunler, eq, brut = pyr_defter(m, veri, kamp, t0, D, fon); satir = []; kapanan = []; olaylar = []
    for s in izlenen:
        if s not in veri: continue
        b = veri[s]; U, L, A = gos[s]; K = kamp[s]; n = len(b); c = b[-1][4]; ac = K[-1] if (K and K[-1]["x"] is None) else None
        r = {"s": s, "t": b[-1][0], "k": pyr_sayi(c), "ust": pyr_sayi(U[-1]), "alt": pyr_sayi(L[-1]), "atr": pyr_sayi(A[-1]), "uzak": round((U[-1] / c - 1) * 100, 2), "kademe": 0, "evrende": s in liste}
        if ac:
            tr = ac["parca"]; ilk = b[tr[0][0]][0]; cik = max(ac["stop"], L[-1])
            r.update(kademe=len(tr), ilk_t=ilk, ilk_f=pyr_sayi(tr[0][1]), stop=pyr_sayi(ac["stop"]), cikis=pyr_sayi(cik), sonraki=pyr_sayi(U[-1]) if len(tr) < PYR_MAXP else None,
                     kz=round(sum(c / f - 1 for _, f in tr) / len(tr) * 100, 2), resmi=ilk > t0, parcalar=[[b[i][0], pyr_sayi(f)] for i, f in tr])
        else:
            r["girilirse_stop"] = pyr_sayi(U[-1] - 2 * A[-1])
        if ac or s in liste: satir.append(r)
        for k in K:
            ilk = b[k["parca"][0][0]][0]
            if ilk <= t0: continue
            for no, (i, f) in enumerate(k["parca"], 1): olaylar.append((m, b[i][0], s, "giris" if no == 1 else "ekleme", no, f))
            if k["x"] is not None:
                xe = k["x"]; g = sum(k["cikis"] / f - 1 for _, f in k["parca"]) / len(k["parca"]) - 2 * PYR_COST[m]
                olaylar.append((m, b[xe][0], s, "cikis-" + k["neden"], len(k["parca"]), k["cikis"]))
                kapanan.append({"s": s, "ilk_t": ilk, "cik_t": b[xe][0], "kademe": len(k["parca"]), "neden": k["neden"], "getiri": round(g * 100, 2)})
    uyari += pyr_olay_yaz(olaylar, m, t0, min(b[0][0] for b in veri.values()))
    kapanan.sort(key=lambda x: x["cik_t"], reverse=True)
    out = {"ad": PYR_AD[m], "son": son, "t0": t0, "guncelleme": int(time.time()), "evren": len(liste), "satir": satir, "kapanan": kapanan[:200], "kapanan_n": len(kapanan),
           "kazanan_n": sum(1 for x in kapanan if x["getiri"] > 0), "ozkaynak": round(eq[-1], 6) if eq else 1.0, "gun": len(gunler), "nominal": round(brut[-1] * 100, 1) if brut else 0.0,
           "egri": [[d, round(e, 5)] for d, e in list(zip(gunler, eq))[-400:]], "uyari": uyari, "faiz": faiz, "maliyet": PYR_COST[m],
           "bugun": [{"s": o[2], "tur": o[3], "no": o[4], "f": pyr_sayi(o[5])} for o in olaylar if o[1] == son]}
    with PYR["kilit"]:
        tum = PYR["sonuc"] or pyr_json(PYR_OUT, {}); tum[m] = out; PYR["sonuc"] = tum; pyr_yaz(PYR_OUT, tum); pyr_yaz(PYR_STATE, st)
    PYR["durum"][m] = {"durum": "hazir"}
    return out


def pyr_kayitli(m):
    """Olay kaydinda satiri bulunan semboller."""
    out = set()
    try:
        for r in csv.reader(open(PYR_LOG, encoding="utf-8")):
            if len(r) >= 6 and r[0] == m: out.add(r[2])
    except Exception:
        pass
    return out


def pyr_olay_yaz(olaylar, m, t0, pencere_basi):
    """Resmi olaylari kayda ekler (satir degistirilmez). Daha once kaydedilmis bir olay bugunku hesapta yoksa uyari dondurur."""
    var = {}; uy = []
    try:
        for r in csv.reader(open(PYR_LOG, encoding="utf-8")):
            if len(r) >= 6 and r[0] == m: var[(r[1], r[2], r[3], r[4])] = float(r[5])
    except Exception:
        pass
    yeni = []; simdi = set()
    for (mm, d, s, tur, no, f) in olaylar:
        k = (d, s, tur, str(no)); simdi.add(k)
        if k not in var: yeni.append([mm, d, s, tur, no, "%.8g" % f, time.strftime("%Y-%m-%d %H:%M")])
        elif abs(var[k] / f - 1) > 0.02: uy.append("geçmiş fiyat düzeltilmiş (bölünme/temettü olabilir): %s %s %s" % (s, d, tur))
    for k in var:
        if k not in simdi and k[0] > max(t0, pencere_basi): uy.append("kayıtlı olay artık hesapta yok: %s %s %s" % (k[1], k[0], k[2]))
    if yeni:
        ilk = not os.path.exists(PYR_LOG)
        with open(PYR_LOG, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if ilk: w.writerow(["piyasa", "tarih", "sembol", "olay", "parca_no", "fiyat", "kayit_zamani"])
            w.writerows(yeni)
    return uy[:12]


def pyr_son_bar(m):
    if m == "bist":
        d = pyr_bist_dosya().get("THYAO"); bas = (pyr_utc() - timedelta(days=20)).strftime("%Y-%m-%d")
        if d: return d[-1][0]
        try: return pyr_isy("THYAO", bas)[-1][0]
        except Exception: return pyr_yahoo("THYAO", bas)[-1][0]
    return (pyr_nasdaq("AAPL", 12) if m == "us" else pyr_bybit("BTCUSDT", 5))[-1][0]


def pyr_dongu():
    """Arka planda: bir piyasada yeni tam gunluk bar cikinca o piyasayi yeniden hesaplar. 20 dakikada bir bakar."""
    time.sleep(8)
    PYR["sonuc"] = pyr_json(PYR_OUT, {}) or None
    while True:
        for m in ("us", "bist"):
            try:
                eski = ((PYR["sonuc"] or {}).get(m) or {})
                if not eski or pyr_son_bar(m) > eski.get("son", ""): pyr_guncelle(m)
            except Exception as e:
                PYR["durum"][m] = {"durum": "hata", "hata": str(e)[:140]}; sys.stderr.write("pyramid %s: %s\n" % (m, str(e)[:140]))
        time.sleep(1200)


def pyr_api():
    tum = PYR["sonuc"] or pyr_json(PYR_OUT, {})
    return {"ok": True, "piyasa": tum, "durum": PYR["durum"], "kural": {"parca": PYR_PARCA, "maxp": PYR_MAXP}}


def pyr_probe():
    """Sunucudan hangi veri kaynaklarina ulasilabildigini yazar."""
    def dene(ad, fn):
        t = time.time()
        try:
            r = fn(); print("  %-34s TAMAM  %s  (%.1f sn)" % (ad, r, time.time() - t))
        except Exception as e:
            print("  %-34s HATA   %s  (%.1f sn)" % (ad, str(e)[:110], time.time() - t))
    def ozet(b): return "%d bar, %s -> %s, son kapanis %.6g" % (len(b), b[0][0], b[-1][0], b[-1][4])
    dene("ABD   Nasdaq AAPL", lambda: ozet(pyr_nasdaq("AAPL")))
    dene("ABD   Nasdaq BRK-B", lambda: ozet(pyr_nasdaq("BRK-B")))
    dene("Kripto Bybit BTCUSDT gunluk", lambda: ozet(pyr_bybit("BTCUSDT")))
    dene("Kripto Bybit fonlama", lambda: "%d gun" % len(pyr_fonlama("BTCUSDT", (pyr_utc() - timedelta(days=20)).strftime("%Y-%m-%d"))))
    dene("BIST  Yahoo THYAO", lambda: ozet(pyr_yahoo("THYAO")))
    dene("BIST  Yahoo SOKM", lambda: ozet(pyr_yahoo("SOKM")))
    def isy():
        d2 = pyr_utc(); d1 = d2 - timedelta(days=20)
        raw = http_get("https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/Data.aspx/HisseTekil?hisse=THYAO&startdate=%s&enddate=%s" % (d1.strftime("%d-%m-%Y"), d2.strftime("%d-%m-%Y")), timeout=12)
        v = (json.loads(raw).get("value") or []); return "%d satir; alanlar: %s" % (len(v), sorted(v[-1].keys())[:40] if v else None)
    dene("BIST  Is Yatirim (yedek aday)", isy)
    print("  elle BIST dosyasi (%s): %s" % (PYR_BIST_FILE, "var, %d hisse" % len(pyr_bist_dosya()) if os.path.exists(PYR_BIST_FILE) else "yok"))


# ====================================================================================================
# BIST 50 taramasi + Portfoy takibi ve uyarilar
#   Portfoye eklenen hisse icin (yalniz long):
#     ekleme uyarisi : fiyat onceki 55 gunun tepesine degerse / gecerse. Hisse basina en cok 3, gunde en cok 1.
#                      Yalniz eklendigi gunden SONRAKI gunlerde.
#     cikis uyarisi  : fiyat onceki 20 gunun dibine degerse.
#     stop uyarisi   : fiyat giris - 2 x ATR14 seviyesine gelirse (stop eklenirken sabitlenir).
#   Cikis ya da stop uyarisindan sonra o hisse icin baska uyari gelmez.
#   Bildirim: ntfy uygulamasi (telefon). ~/.atr_env icine TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID yazilirsa Telegram'a da gider.
# ====================================================================================================
import hashlib, hmac, secrets

BIST50_FILE = os.path.expanduser("~/.atr_bist50.txt")          # istege bagli: endeks degisince kodlari buraya yaz (boslukla ya da satir satir)
BIST50_AUTO = os.path.expanduser("~/.atr_bist50_auto.json")   # TradingView'den otomatik cekilen BIST 50 listesi (gunde bir yenilenir)
BIST_SCAN_FILE = os.path.expanduser("~/.atr_bist_scan.json")
ISY = "https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/"
YAHOO_HD = {"User-Agent": "Mozilla/5.0"}                      # sunucudan Yahoo yalniz bu sade kimlikle ve query2 uzerinden yanit veriyor
PF_FILE = os.path.expanduser("~/.atr_portfoy.json")
BIST50 = ("AEFES AKBNK AKSEN ALARK ASELS ASTOR BIMAS BRSAN BTCIM CANTE CCOLA CIMSA CVKMD CWENE DOAS ECILC EKGYO ENERY ENKAI EREGL FROTO GARAN GLRMK GUBRF HALKB "
          "HEKTS ISCTR KCHOL KRDMD MAVI MGROS OYAKC PETKM PGSUS SAHOL SASA SISE TAVHL TCELL THYAO TOASO TRALT TRMET TSKB TTKOM TUPRS TURSG ULKER VAKBN YKBNK").split()
PF_MAX_EK = 3
PF_MAX_POZ = 60
BSCAN = {"status": "bekliyor", "progress": [0, 0], "result": None, "started": 0.0, "error": ""}
BSCAN_LOCK = threading.Lock()
PF = {"kilit": threading.RLock(), "veri": None, "onbellek": {}, "yanlis": [], "hata": {}}
PF_AD = {"us": "ABD", "bist": "BIST"}


def ist_now():
    return pyr_utc() + timedelta(hours=3)


def bist_seans():
    n = ist_now()
    return n.weekday() < 5 and (9, 55) <= (n.hour, n.minute) < (18, 45)      # kapanis 18:10; gecikmeli veri icin 18:45'e kadar sik denetim


def bist50_tv():
    """BIST 50 uyeleri, TradingView tarayicisindan (hisselerin 'indexes' sutununda BIST:XU050 gecenler)."""
    j = json.loads(http_post_json("https://scanner.tradingview.com/turkey/scan", {"filter": [], "options": {"lang": "tr"}, "markets": ["turkey"],
                                  "symbols": {"query": {"types": []}, "tickers": []}, "columns": ["indexes"], "range": [0, 2000]}, timeout=20,
                                  headers={"Content-Type": "application/json", "Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}))
    out = set()
    for x in j.get("data") or []:
        s = str(x.get("s") or ""); ix = (x.get("d") or [None])[0] or []
        if s.startswith("BIST:") and any((i or {}).get("proname") == "BIST:XU050" for i in ix) and re.fullmatch(r"[A-Z0-9]{3,6}", s[5:]): out.add(s[5:])
    if not 45 <= len(out) <= 55: raise LookupError("beklenmeyen uye sayisi: %d" % len(out))
    return sorted(out)


def bist_listesi(bilgi=None):
    """Oncelik: elle dosya > TradingView'den otomatik liste (gunde bir yenilenir) > yerlesik liste. bilgi sozlugune kaynak yazilir."""
    try:
        kod = [x for x in re.split(r"[\s,;]+", open(BIST50_FILE, encoding="utf-8").read().upper()) if re.fullmatch(r"[A-Z0-9]{3,6}", x)]
        if len(kod) >= 10:
            if bilgi is not None: bilgi["liste"] = "elle dosya"
            return sorted(set(kod))
    except Exception:
        pass
    oto = pyr_json(BIST50_AUTO, {}); now = time.time()
    if now - oto.get("deneme", 0) > 20 * 3600:
        oto["deneme"] = now
        try:
            oto["liste"] = bist50_tv(); oto["ts"] = now; oto.pop("hata", None)
        except Exception as e:
            oto["hata"] = str(e)[:100]
        try: pyr_yaz(BIST50_AUTO, oto)
        except Exception: pass
    if oto.get("liste") and now - oto.get("ts", 0) < 14 * 86400:
        if bilgi is not None: bilgi["liste"] = "TradingView, BIST 50"
        return list(oto["liste"])
    if bilgi is not None: bilgi["liste"] = "yerleşik liste (7 Ekim 2026)"
    return list(BIST50)


def http_post_json(url, obj, timeout=10, headers=None):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8"); h = {"User-Agent": UA, "Content-Type": "application/json; charset=utf-8"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------------------- BIST verisi
def bist_yahoo(t):
    """BIST gunluk barlar (bolunmeye gore duzeltilmis, temettuye gore DEGIL: grafikte gorunen fiyat) + bugunun suren bari (15 dk gecikmeli)."""
    hata = None
    for host in ("query2", "query1"):
        try:
            j = json.loads(http_get("https://%s.finance.yahoo.com/v8/finance/chart/%s.IS?range=1y&interval=1d" % (host, quote(t)), timeout=12, headers=YAHOO_HD))
            r = j["chart"]["result"][0]; q = r["indicators"]["quote"][0]; meta = r.get("meta") or {}
            ist = ist_now(); bugun = ist.strftime("%Y-%m-%d"); gun = {}; vol = q.get("volume") or []
            for i, ts in enumerate(r.get("timestamp") or []):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, h, l, c) or min(o, h, l, c) <= 0: continue
                d = (datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
                v = (vol[i] if i < len(vol) else 0) or 0
                if v <= 0 and h == l and d != bugun: continue                      # tatil dolgusu
                gun[d] = (d, float(o), float(h), float(l), float(c))
            bars = [gun[k] for k in sorted(gun)]; part = None; saat = ""
            if bars and bars[-1][0] == bugun and (ist.hour, ist.minute) < (18, 30):
                d, o, h, l, c = bars.pop()
                p = meta.get("regularMarketPrice"); hi = meta.get("regularMarketDayHigh"); lo = meta.get("regularMarketDayLow")
                if p and l * 0.8 <= p <= h * 1.2: c = float(p)
                if hi and h <= hi <= h * 1.2: h = float(hi)
                if lo and l * 0.8 <= lo <= l: l = float(lo)
                part = (d, o, max(h, c), min(l, c), c); tm = meta.get("regularMarketTime")
                saat = (datetime.fromtimestamp(tm, tz=timezone.utc) + timedelta(hours=3)).strftime("%H:%M") if tm else ""
            if len(bars) < 20: raise LookupError("yetersiz veri")
            return {"bars": bars, "partial": part, "partial_time": saat, "name": meta.get("shortName") or meta.get("longName") or "", "price": part[4] if part else bars[-1][4],
                    "source": "Yahoo (15 dk gecikmeli)", "currency": "TRY"}
        except Exception as e:
            hata = e
    raise LookupError("Yahoo: %s" % str(hata)[:80])


def isy_gecmis(t, bas=None, gun=420):
    """Is Yatirim gunluk barlar, bolunme ve temettuye gore DUZELTILMIS (HGDG_ alanlari): (tarih, acilis~, yuksek, dusuk, kapanis, hacim).
    Kaynakta acilis yok; acilis = onceki kapanisin o gunun [dusuk, yuksek] araligina kirpilmis hali (yalniz Pyramid defterinde kullanilir)."""
    d2 = ist_now(); d1 = datetime.strptime(bas, "%Y-%m-%d") if bas else d2 - timedelta(days=gun)
    raw = http_get(ISY + "Data.aspx/HisseTekil?hisse=%s&startdate=%s&enddate=%s" % (quote(t), d1.strftime("%d-%m-%Y"), d2.strftime("%d-%m-%Y")), timeout=15)
    gunler = {}
    for x in (json.loads(raw).get("value") or []):
        try:
            d = datetime.strptime(str(x["HGDG_TARIH"])[:10], "%d-%m-%Y").strftime("%Y-%m-%d")
            c = float(x["HGDG_KAPANIS"]); h = float(x["HGDG_MAX"]); l = float(x["HGDG_MIN"]); v = float(x.get("HGDG_HACIM") or 0)
            if min(c, h, l) <= 0 or (v <= 0 and h == l): continue
            gunler[d] = (d, max(h, c), min(l, c), c, v)
        except Exception:
            pass
    out = []; onceki = None
    for d in sorted(gunler):
        _, h, l, c, v = gunler[d]; o = c if onceki is None else min(max(onceki, l), h)
        out.append((d, o, h, l, c, v)); onceki = c
    if not out: raise LookupError("Is Yatirim: veri yok")
    return out


def isy_canli(t):
    """Is Yatirim anlik fiyat: son seansin tarihi/saati ve acilis, yuksek, dusuk, son. Alinamazsa None."""
    try:
        x = json.loads(http_get(ISY + "Data.aspx/OneEndeks?endeks=%s" % quote(t), timeout=10))[0]; ud = str(x["updateDate"])
        o, h, l, c = (float(x.get(k) or 0) for k in ("open", "high", "low", "last"))
        if min(h, l, c) <= 0 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}.*", ud): return None
        if o <= 0: o = c
        return {"gun": ud[:10], "saat": ud[11:16], "o": o, "h": max(h, c, o), "l": min(l, c, o), "c": c}
    except Exception:
        return None


def pyr_isy(s, bas):
    """Pyramid defteri icin Is Yatirim barlari (acilis yaklasik); seans bitmeden bugunun satiri alinmaz."""
    b = isy_gecmis(s, bas); ist = ist_now()
    if b and b[-1][0] == ist.strftime("%Y-%m-%d") and (ist.hour, ist.minute) < (18, 30): b = b[:-1]
    if not b: raise LookupError("Is Yatirim: veri yok")
    return b


def bist_isy(t):
    """BIST: duzeltilmis gunluk gecmis + bugunun suren bari (Is Yatirim). Seans bitmis ama gecmise yazilmamis gun, tam bar olarak eklenir."""
    bars = [b[:5] for b in isy_gecmis(t)]; ist = ist_now(); bugun = ist.strftime("%Y-%m-%d"); hm = (ist.hour, ist.minute)
    if bars and bars[-1][0] == bugun and hm < (18, 15): bars.pop()                  # seans surerken gecmisteki bugun satirina guvenme
    if len(bars) < 20: raise LookupError("Is Yatirim: yetersiz veri")
    q = isy_canli(t); part = None; saat = ""
    if q and q["gun"] > bars[-1][0]:
        b = (q["gun"], q["o"], q["h"], q["l"], q["c"])
        if q["gun"] < bugun or q["saat"] >= "18:05" or hm >= (18, 40): bars.append(b)   # gun kapandi
        else:
            part = b; saat = q["saat"]
    return {"bars": bars, "partial": part, "partial_time": saat, "name": "", "price": q["c"] if q else bars[-1][4], "source": "İş Yatırım", "currency": "TRY"}


def pf_veri(m, t, taze=90):
    """Bir hissenin tamamlanmis gunluk barlari + bugunun suren bari. Kisa sureli onbellek."""
    k = (m, t); now = time.time(); hit = PF["onbellek"].get(k)
    if hit and now - hit[0] < taze: return hit[1]
    if m == "us": d = from_nasdaq(t)
    else:
        try: d = bist_isy(t)
        except Exception as e1:
            try: d = bist_yahoo(t)
            except Exception as e2: raise LookupError("%s; %s" % (str(e1)[:70], str(e2)[:70]))
    PF["onbellek"][k] = (now, d)
    if len(PF["onbellek"]) > 400: PF["onbellek"] = dict(sorted(PF["onbellek"].items(), key=lambda kv: -kv[1][0])[:200])
    return d


# ---------------------------------------------------------------------------------------- BIST 50 taramasi
def bist_scan_run(verbose=False):
    with BSCAN_LOCK:
        if BSCAN["status"] == "taraniyor": return BSCAN["result"]
        BSCAN.update(status="taraniyor", progress=[0, 0], started=time.time(), error="")
    try:
        bilgi = {}; liste = bist_listesi(bilgi); BSCAN["progress"] = [0, len(liste)]; sinyal = []; gunler = {}; hata = 0; kaynak = {}
        for t in liste:
            d = None
            for deneme in range(2):
                try:
                    d = pf_veri("bist", t, taze=300); break
                except Exception:
                    time.sleep(1.5)
            BSCAN["progress"][0] += 1
            if verbose and BSCAN["progress"][0] % 10 == 0: print("  tarandi: %d/%d" % tuple(BSCAN["progress"]), flush=True)
            if not d:
                hata += 1; continue
            bars = d["bars"]; gunler[bars[-1][0]] = gunler.get(bars[-1][0], 0) + 1; kaynak[d["source"]] = kaynak.get(d["source"], 0) + 1
            s = find_signal(bars)
            if s:
                s.update({"ticker": t, "date_tr": tr_date(s["date"])}); sinyal.append(s)
            time.sleep(0.25)
        if len(gunler) == 0:
            BSCAN.update(status="hata", error="BIST verisi alınamadı (%d hisse)" % hata); return None
        bar = max(gunler, key=gunler.get)
        sinyal = [x for x in sinyal if (datetime.strptime(bar, "%Y-%m-%d") - datetime.strptime(x["date"], "%Y-%m-%d")).days <= 7]
        sinyal.sort(key=lambda x: (x["age"], x["ticker"]))
        res = {"bar_date": bar, "bar_date_tr": tr_date(bar), "updated": int(time.time()), "universe_n": len(liste), "scanned_n": len(liste) - hata, "failed_n": hata,
               "signals": sinyal, "source": max(kaynak, key=kaynak.get) if kaynak else "", "liste": bilgi.get("liste", "")}
        BSCAN["result"] = res; BSCAN["status"] = "hazir"
        try: pyr_yaz(BIST_SCAN_FILE, res)
        except Exception: pass
        return res
    except Exception as e:
        BSCAN.update(status="hata", error=str(e)[:160]); return None


def bist_scan_loop():
    """Arka planda: BIST'te yeni gunun bari cikinca taramayi calistirir. 20 dakikada bir bakar."""
    time.sleep(6)
    while True:
        try:
            son = pf_veri("bist", "THYAO", taze=600)["bars"][-1][0]; res = BSCAN["result"]
            if not res or res.get("bar_date", "") < son: bist_scan_run()
        except Exception as e:
            sys.stderr.write("bist tarama dongusu: %s\n" % str(e)[:120])
        time.sleep(1200)


# ---------------------------------------------------------------------------------------- portfoy kaydi
def pf_yukle():
    if PF["veri"] is None:
        v = pyr_json(PF_FILE, {})
        v.setdefault("poz", []); v.setdefault("uyari", []); v.setdefault("sayac", 0)
        PF["veri"] = v
    return PF["veri"]


def pf_kaydet():
    pyr_yaz(PF_FILE, PF["veri"])
    try: os.chmod(PF_FILE, 0o600)
    except Exception: pass


def pf_pin_ozet(tuz, pin):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), tuz.encode("ascii"), 60000).hex()


def pf_pin_koy(pin):
    with PF["kilit"]:
        v = pf_yukle()
        if v.get("pin"): return False
        tuz = secrets.token_hex(8); v["pin"] = {"tuz": tuz, "ozet": pf_pin_ozet(tuz, pin)}
        v.setdefault("ntfy", "atr-" + secrets.token_hex(8)); pf_kaydet(); return True


def pf_pin_dogru(pin):
    """True / False; cok fazla yanlis denemede None (gecici kilit)."""
    v = pf_yukle(); p = v.get("pin"); now = time.time()
    PF["yanlis"] = [x for x in PF["yanlis"] if now - x < 600]
    if len(PF["yanlis"]) >= 8: return None
    if p and pin and hmac.compare_digest(pf_pin_ozet(p["tuz"], str(pin)[:40]), p["ozet"]): return True
    if pin: PF["yanlis"].append(now)
    return False


def pf_seviye(bars):
    """Tamamlanmis barlardan (55 gunluk tepe, 20 gunluk dip); yeterli bar yoksa None."""
    return (max(b[2] for b in bars[-55:]) if len(bars) >= 55 else None), (min(b[3] for b in bars[-20:]) if len(bars) >= 20 else None)


def pf_onizle(m, s, giris=None):
    d = pf_veri(m, s); bars = d["bars"]; part = d.get("partial")
    if part and part[0] <= bars[-1][0]: part = None
    atr = wilder_atr(bars + [part]) if part else wilder_atr(bars)
    if not atr: raise LookupError("yeterli günlük veri yok")
    tepe, dip = pf_seviye(bars); fiyat = part[4] if part else (d.get("price") or bars[-1][4])
    out = {"ok": True, "m": m, "s": s, "ad": d.get("name") or "", "atr": atr, "fiyat": fiyat, "tepe55": tepe, "dip20": dip, "son_bar": bars[-1][0], "canli": bool(part),
           "saat": d.get("partial_time") or "", "kaynak": d.get("source") or ""}
    if giris: out["stop"] = giris - 2 * atr
    return out, bars, part


def pf_ekle(m, s, giris):
    o, bars, part = pf_onizle(m, s, giris)
    if o["stop"] <= 0: return {"ok": False, "error": "ATR giriş fiyatına göre çok büyük; fiyatı kontrol et."}
    if abs(giris / o["fiyat"] - 1) > 0.5: return {"ok": False, "error": "Giriş fiyatı son fiyattan (%.2f) çok farklı; hisse kodunu ve fiyatı kontrol et." % o["fiyat"]}
    with PF["kilit"]:
        v = pf_yukle()
        if any(p["m"] == m and p["s"] == s and p["durum"] == "acik" for p in v["poz"]): return {"ok": False, "error": "%s zaten portföyde." % s}
        if len(v["poz"]) >= PF_MAX_POZ: return {"ok": False, "error": "Portföy dolu (%d kayıt). Önce kapananları kaldır." % PF_MAX_POZ}
        v["sayac"] += 1
        p = {"id": v["sayac"], "m": m, "s": s, "ad": o["ad"], "giris": giris, "atr": o["atr"], "stop": o["stop"], "eklendi": int(time.time()), "son_gun": bars[-1][0],
             "giris_gun": part[0] if part else None, "dusuk0": part[3] if part else None, "ekleme": 0, "ek_gun": None, "durum": "acik", "bitis": None,
             "canli": {"fiyat": o["fiyat"], "tepe55": o["tepe55"], "dip20": o["dip20"], "saat": o["saat"], "gun": part[0] if part else bars[-1][0], "zaman": int(time.time())}}
        v["poz"].append(p); pf_kaydet()
    return {"ok": True, "id": p["id"], "stop": p["stop"], "atr": p["atr"]}


def pf_sil(pid):
    with PF["kilit"]:
        v = pf_yukle(); n = len(v["poz"]); v["poz"] = [p for p in v["poz"] if p["id"] != pid]
        if len(v["poz"]) != n: pf_kaydet()
        return len(v["poz"]) != n


# ---------------------------------------------------------------------------------------- uyari motoru
def pf_denetle(p, bars, part):
    """Pozisyonu, son denetlenen gunden sonraki tamamlanmis barlarda ve bugunun suren barinda denetler; p'yi gunceller.
    Donus: [(tur, gun, seviye, fiyat, no)]; tur: ekleme | cikis | stop."""
    olay = []

    def bak(d, h, l, c, once):
        tepe, dip = pf_seviye(once); giris_gunu = (d == p.get("giris_gun"))
        if not giris_gunu and tepe is not None and p["ekleme"] < PF_MAX_EK and p.get("ek_gun") != d and h >= tepe:
            p["ekleme"] += 1; p["ek_gun"] = d; olay.append(("ekleme", d, tepe, c, p["ekleme"]))
        lvl = max(p["stop"], dip) if dip is not None else p["stop"]
        dustu = l <= lvl
        if giris_gunu and p.get("dusuk0") is not None:                     # giris gununde yalniz eklendikten SONRAKI hareket sayilir
            dustu = (l < p["dusuk0"] and l <= lvl) or c <= lvl
        if dustu:
            tur = "stop" if lvl == p["stop"] else "cikis"
            p["durum"] = tur; p["bitis"] = {"gun": d, "seviye": lvl}; olay.append((tur, d, lvl, c, 0))
        return dustu

    for i, b in enumerate(bars):
        if b[0] <= p["son_gun"]: continue
        if p["durum"] != "acik": break
        bak(b[0], b[2], b[3], b[4], bars[:i]); p["son_gun"] = b[0]
    if p["durum"] == "acik" and part and part[0] > bars[-1][0]:
        bak(part[0], part[2], part[3], part[4], bars)
    return olay


def pf_fiyat(m, x):
    return ("%.2f" % x).replace(".", ",") + (" $" if m == "us" else " TL")


def pf_metin(p, olay, bugun):
    tur, d, lvl, c, no = olay; s = p["s"]; gun = "" if d == bugun else " (%s)" % tr_date(d)
    if tur == "ekleme":
        return "%s · ekleme %d/%d" % (s, no, PF_MAX_EK), "55 günlük tepeye değdi: %s. Son fiyat %s.%s" % (pf_fiyat(p["m"], lvl), pf_fiyat(p["m"], c), gun), 4
    if tur == "cikis":
        return "%s · ÇIKIŞ" % s, "20 günlük dibe değdi: %s. Son fiyat %s.%s" % (pf_fiyat(p["m"], lvl), pf_fiyat(p["m"], c), gun), 5
    return "%s · STOP" % s, "Stop seviyesine geldi: %s (giriş %s). Son fiyat %s.%s" % (pf_fiyat(p["m"], lvl), pf_fiyat(p["m"], p["giris"]), pf_fiyat(p["m"], c), gun), 5


def pf_gonder(baslik, metin, oncelik=4):
    """Telefona (ntfy) ve ayarliysa Telegram'a gonderir. Donus: (ulasan kanallar, hatalar)."""
    v = pf_yukle(); kanal = []; hata = []; konu = v.get("ntfy")
    if konu and os.environ.get("ATR_NTFY", "1") != "0":
        try:
            http_post_json(os.environ.get("NTFY_URL") or "https://ntfy.sh", {"topic": konu, "title": baslik, "message": metin, "priority": oncelik,
                                                                            "tags": ["rotating_light"] if oncelik >= 5 else ["chart_with_upwards_trend"]}); kanal.append("telefon")
        except Exception as e:
            hata.append("ntfy: %s" % str(e)[:80])
    tok = os.environ.get("TELEGRAM_BOT_TOKEN"); chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            http_post_json("https://api.telegram.org/bot%s/sendMessage" % tok, {"chat_id": chat, "text": "%s\n%s" % (baslik, metin)}); kanal.append("telegram")
        except Exception as e:
            hata.append("telegram: %s" % type(e).__name__)                  # anahtar sizmasin diye hata metni yazilmaz
    return kanal, hata


def pf_tur(m):
    """Bir piyasadaki acik pozisyonlari denetler; uyarilari kaydeder ve gonderir. Donus: gonderilen uyari sayisi."""
    with PF["kilit"]:
        acik = [p for p in pf_yukle()["poz"] if p["m"] == m and p["durum"] == "acik"]
    bugun = (ny_now() if m == "us" else ist_now()).strftime("%Y-%m-%d"); n = 0
    for p in acik:
        try:
            d = pf_veri(m, p["s"], taze=50); PF["hata"].pop((m, p["s"]), None)
        except Exception as e:
            PF["hata"][(m, p["s"])] = str(e)[:120]; continue
        bars = d["bars"]; part = d.get("partial")
        if part and part[0] <= bars[-1][0]: part = None
        gonder = []
        with PF["kilit"]:
            if p["durum"] != "acik" or p not in pf_yukle()["poz"]: continue
            onceki = p["son_gun"]; olay = pf_denetle(p, bars, part); tepe, dip = pf_seviye(bars)
            p["canli"] = {"fiyat": part[4] if part else bars[-1][4], "tepe55": tepe, "dip20": dip, "saat": d.get("partial_time") or "", "gun": part[0] if part else bars[-1][0], "zaman": int(time.time())}
            for o in olay:
                baslik, metin, onc = pf_metin(p, o, bugun)
                kayit = {"zaman": int(time.time()), "m": m, "s": p["s"], "pid": p["id"], "tur": o[0], "gun": o[1], "seviye": o[2], "fiyat": o[3], "no": o[4], "baslik": baslik, "metin": metin, "kanal": []}
                pf_yukle()["uyari"].append(kayit); gonder.append((kayit, baslik, metin, onc))
            pf_yukle()["uyari"] = pf_yukle()["uyari"][-300:]
            if olay or p["son_gun"] != onceki: pf_kaydet()
        for kayit, baslik, metin, onc in gonder:
            kanal, hata = pf_gonder(baslik, metin, onc); n += 1
            with PF["kilit"]:
                kayit["kanal"] = kanal
                if hata: kayit["hata"] = "; ".join(hata)
                pf_kaydet()
    return n


def pf_dongu():
    """Arka planda: seans acikken 2-3 dakikada bir, kapaliyken yarim saatte bir acik pozisyonlari denetler."""
    time.sleep(5); son = {"us": 0.0, "bist": 0.0}
    while True:
        for m in ("us", "bist"):
            try:
                with PF["kilit"]:
                    var = any(p["m"] == m and p["durum"] == "acik" for p in pf_yukle()["poz"])
                if not var: continue
                ara = (120 if session_open() else 1800) if m == "us" else (180 if bist_seans() else 1800)
                if time.time() - son[m] >= ara:
                    son[m] = time.time(); pf_tur(m)
            except Exception as e:
                sys.stderr.write("portfoy dongusu %s: %s\n" % (m, str(e)[:140]))
        time.sleep(20)


def pf_api(pin):
    v = pf_yukle()
    if not v.get("pin"): return {"ok": True, "pin_var": False}
    r = pf_pin_dogru(pin)
    if r is None: return {"ok": False, "pin_var": True, "yetki": False, "error": "Çok fazla yanlış deneme. 10 dakika sonra yeniden dene."}
    if not r: return {"ok": False, "pin_var": True, "yetki": False, "error": "PIN yanlış." if pin else ""}
    with PF["kilit"]:
        poz = [dict(p, hata=PF["hata"].get((p["m"], p["s"]), "")) for p in v["poz"]]
        return {"ok": True, "pin_var": True, "yetki": True, "poz": poz, "uyari": v["uyari"][-60:][::-1], "ntfy": v.get("ntfy") or "", "ntfy_url": os.environ.get("NTFY_URL") or "https://ntfy.sh",
                "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")), "seans": {"us": session_open(), "bist": bist_seans()}, "max_ek": PF_MAX_EK}


def pf_istek(yol, govde, pin):
    """POST /api/pf/<yol>"""
    v = pf_yukle()
    if yol == "pin":
        if v.get("pin"): return {"ok": False, "error": "PIN zaten belirlenmiş."}
        yeni = str(govde.get("pin") or "")
        if not re.fullmatch(r"[A-Za-z0-9]{4,20}", yeni): return {"ok": False, "error": "PIN 4–20 harf ya da rakam olmalı."}
        pf_pin_koy(yeni); return {"ok": True}
    r = pf_pin_dogru(pin)
    if not r: return {"ok": False, "yetki": False, "error": "Çok fazla yanlış deneme. 10 dakika sonra yeniden dene." if r is None else "PIN yanlış."}
    if yol == "ekle":
        m = govde.get("m"); s = str(govde.get("s") or "").upper().strip().replace(".", "-")
        try: giris = float(govde.get("giris"))
        except Exception: return {"ok": False, "error": "Giriş fiyatı sayı olmalı."}
        if m not in ("us", "bist") or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,6}" if m == "us" else r"[A-Z0-9]{3,6}", s) or not giris > 0: return {"ok": False, "error": "Hisse kodu ya da fiyat geçersiz."}
        try: return pf_ekle(m, s, giris)
        except LookupError as e: return {"ok": False, "error": "%s için veri alınamadı (%s)." % (s, str(e)[:120])}
        except Exception as e: return {"ok": False, "error": "Eklenemedi: %s" % str(e)[:120]}
    if yol == "sil":
        try: return {"ok": pf_sil(int(govde.get("id")))}
        except Exception: return {"ok": False, "error": "Geçersiz kayıt."}
    if yol == "deneme":
        kanal, hata = pf_gonder("ATR Stop · deneme", "Bildirim çalışıyor. Uyarılar buraya düşecek.", 3)
        return {"ok": bool(kanal), "kanal": kanal, "error": "; ".join(hata) if hata else ("" if kanal else "Bildirim kanalı ayarlı değil.")}
    return {"ok": False, "error": "bilinmeyen istek"}


def pf_probe():
    def dene(ad, fn):
        t = time.time()
        try:
            r = fn(); print("  %-34s TAMAM  %s  (%.1f sn)" % (ad, r, time.time() - t))
        except Exception as e:
            print("  %-34s HATA   %s  (%.1f sn)" % (ad, str(e)[:110], time.time() - t))
    def oz(d): return "%d bar, son tam bar %s, süren bar %s, kaynak %s" % (len(d["bars"]), d["bars"][-1], d.get("partial"), d["source"])
    print("Portföy ve BIST taraması için:")
    dene("BIST  İş Yatırım THYAO (canlı dahil)", lambda: oz(bist_isy("THYAO")))
    dene("BIST  İş Yatırım anlık THYAO", lambda: isy_canli("THYAO"))
    dene("BIST  Yahoo THYAO (yedek)", lambda: oz(bist_yahoo("THYAO")))
    def liste():
        l = bist50_tv(); return "%d hisse | yerleşik listede olmayan: %s | yerleşikte olup burada olmayan: %s" % (len(l), sorted(set(l) - set(BIST50)), sorted(set(BIST50) - set(l)))
    dene("BIST 50 listesi (TradingView)", liste)
    dene("ABD   Nasdaq AAPL (canlı dahil)", lambda: oz(from_nasdaq("AAPL")))
    dene("Bildirim ntfy.sh erişimi", lambda: http_get((os.environ.get("NTFY_URL") or "https://ntfy.sh") + "/v1/health", timeout=8)[:60])
    print("  Telegram: %s" % ("ayarlı" if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID") else "ayarlı değil (şimdilik gerekmiyor)"))
    print("  İstanbul saati %s | BIST seansı açık: %s | New York saati %s | ABD seansı açık: %s" % (ist_now().strftime("%Y-%m-%d %H:%M"), bist_seans(), ny_now().strftime("%Y-%m-%d %H:%M"), session_open()))


PYR_PAGE = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Pyramid Takip</title>
<link rel="icon" href="icon.png"><link rel="apple-touch-icon" href="icon.png">
<meta name="theme-color" content="#0e1412" media="(prefers-color-scheme: dark)"><meta name="theme-color" content="#eef1ef" media="(prefers-color-scheme: light)">
<style>
:root{--bg:#eef1ef;--surface:#fbfcfb;--line:#d3dbd6;--fg:#13201b;--muted:#5a6962;--accent:#0d6b5a;--accent-fg:#fff;
 --gain:#147a43;--gain-bg:#dff1e6;--loss:#b8322b;--loss-bg:#f8e1df;--warn:#8f5d00;--warn-bg:#f6ead0;
 --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,"SF Mono","Roboto Mono",Menlo,Consolas,monospace;--r:12px;color-scheme:light}
@media (prefers-color-scheme: dark){:root{--bg:#0e1412;--surface:#161e1b;--line:#2b3632;--fg:#e5ede8;--muted:#93a39b;--accent:#55c9ac;--accent-fg:#06211b;
 --gain:#54c48b;--gain-bg:#143224;--loss:#f0746a;--loss-bg:#3a1c1a;--warn:#e2ab4d;--warn-bg:#3a2d12;color-scheme:dark}}
*{box-sizing:border-box} html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);font-size:15px;line-height:1.45;padding:calc(18px + env(safe-area-inset-top,0px)) 16px calc(40px + env(safe-area-inset-bottom,0px))}
[hidden]{display:none!important}
.wrap{max-width:560px;margin-inline:auto;display:flex;flex-direction:column;gap:18px}
header{display:flex;justify-content:space-between;align-items:flex-end;gap:12px}
h1{font-size:22px;line-height:1.15;margin:0;font-weight:750;letter-spacing:-.01em}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:650;margin:0 0 8px}
.eyebrow{font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:650}
a.back{font-size:14px;color:var(--accent);text-decoration:none;font-weight:600;white-space:nowrap}
.seg{display:grid;grid-template-columns:repeat(2,1fr);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;background:var(--surface)}
.seg button{font:inherit;font-size:15px;font-weight:650;padding:12px 6px;border:0;background:transparent;color:var(--muted);cursor:pointer}
.seg button[aria-pressed="true"]{background:var(--accent);color:var(--accent-fg)}
button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.meta{font-size:13px;color:var(--muted)}
.book{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:12px 16px}
.stat{display:flex;flex-direction:column;gap:1px;min-width:0}
.stat b{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:20px;font-weight:650;line-height:1.2}
.stat span{font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:650}
.pos{color:var(--gain)} .neg{color:var(--loss)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px;display:flex;flex-direction:column;gap:8px}
.list{display:flex;flex-direction:column;gap:10px}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.sym{font-weight:750;font-size:17px;letter-spacing:.02em}
.tag{font-size:11px;font-weight:650;letter-spacing:.05em;text-transform:uppercase;border-radius:999px;padding:2px 8px;background:var(--warn-bg);color:var(--warn)}
.dots{display:inline-flex;gap:4px;vertical-align:middle;margin-left:8px} .dots i{width:9px;height:9px;border-radius:50%;border:1.5px solid var(--accent);display:block} .dots i.on{background:var(--accent)}
.kv{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px}
.kv div{display:flex;flex-direction:column;min-width:0} .kv span{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:650}
.kv b{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:15px;font-weight:600;overflow-wrap:anywhere}
.row{display:grid;grid-template-columns:minmax(64px,1.1fr) 1fr 1fr 1fr;gap:8px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--line);font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13.5px}
.row:last-child{border-bottom:0} .row.h{font-family:var(--sans);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:650;padding-top:0}
.row > :not(:first-child){text-align:right} .row .s{font-family:var(--sans);font-weight:700;font-size:14.5px}
.box{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px}
.empty{color:var(--muted);font-size:14px;padding:4px 0}
.field{display:flex;flex-direction:column;gap:5px} label{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:650}
input{font:inherit;font-size:17px;font-family:var(--mono);color:var(--fg);background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:12px;width:100%}
details{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:0 14px} summary{cursor:pointer;padding:13px 0;font-weight:650}
.scroll{overflow-x:auto;margin:0 -14px;padding:0 14px 12px} .scroll .row{min-width:430px;grid-template-columns:minmax(70px,1.1fr) repeat(5,1fr)}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:var(--r);padding:10px 12px;font-size:13.5px}
.note{font-size:13px;color:var(--muted)}
.ev{display:flex;justify-content:space-between;gap:10px;font-size:14px;padding:6px 0;border-bottom:1px solid var(--line)} .ev:last-child{border-bottom:0} .ev b{font-family:var(--mono);font-weight:600}
</style></head>
<body>
<div class="wrap">
  <header><div><div class="eyebrow">Kâğıt takibi</div><h1>Donchian 55/20 Pyramid</h1></div><a class="back" href="./">‹ Geri</a></header>
  <div class="seg" role="group" aria-label="Piyasa">
    <button type="button" data-m="bist" aria-pressed="true">BIST</button><button type="button" data-m="us" aria-pressed="false">ABD</button>
  </div>
  <div class="meta" id="meta">Yükleniyor…</div>
  <div id="warn" class="warn" hidden></div>
  <section id="main" hidden>
   <div class="wrap">
    <div class="book" id="book"></div>
    <div><h2>Son kapanışta olanlar</h2><div class="box" id="today"></div></div>
    <div><h2>Açık pozisyonlar</h2><div class="list" id="open"></div></div>
    <div class="field"><label for="cap">Defter sermayesi (parça tutarını hesaplamak için)</label><input id="cap" inputmode="decimal" placeholder="örn. 100000"></div>
    <div><h2>Kırılıma en yakın</h2><div class="box" id="near"></div></div>
    <details><summary>Tüm semboller</summary><div class="scroll" id="all"></div></details>
    <details><summary>Kapanan kampanyalar</summary><div id="closed" style="padding-bottom:10px"></div></details>
    <div class="note" id="note"></div>
   </div>
  </section>
</div>
<script>
const $ = id => document.getElementById(id);
let data = null, mk = 'bist';
try{ const m = localStorage.getItem('pyrm'); if(m==='us' || m==='bist') mk = m; }catch(e){}
const nf = (x, d) => x==null ? '–' : Number(x).toLocaleString('tr-TR', {minimumFractionDigits:d, maximumFractionDigits:d});
const px = x => { if(x==null) return '–'; const a = Math.abs(x); return nf(x, a>=1000?1: a>=100?2: a>=1?3: a>=0.01?5:7); };
const pct = x => x==null ? '–' : (x>0?'+':'') + nf(x, 2) + '%';
const trd = d => { if(!d) return '–'; const p = d.split('-'); return p[2]+'.'+p[1]+'.'+p[0]; };
const el = (t, c, txt) => { const e = document.createElement(t); if(c) e.className = c; if(txt!=null) e.textContent = txt; return e; };
const num = s => { let t = String(s||'').trim().replace(/\s/g,''); if(t.includes(',') && t.includes('.')) t = t.replace(/\./g,''); const v = parseFloat(t.replace(',', '.')); return isFinite(v) ? v : NaN; };
function kv(parent, label, value, cls){ const d = el('div'); d.append(el('span', null, label), el('b', cls||null, value)); parent.append(d); }
function row(parent, cells, head){ const r = el('div', 'row'+(head?' h':'')); cells.forEach((c,i)=>{ const s = el('span', (!head && i===0)?'s':null, c); r.append(s); }); parent.append(r); }
function render(){
  document.querySelectorAll('.seg button').forEach(b=>b.setAttribute('aria-pressed', b.dataset.m===mk));
  const d = data && data.piyasa ? data.piyasa[mk] : null, st = data && data.durum ? data.durum[mk] : null;
  $('warn').hidden = true;
  if(!d){ $('main').hidden = true; $('meta').textContent = st && st.durum==='guncelleniyor' ? 'İlk hesap yapılıyor… ' + (st.ilerleme ? st.ilerleme[0]+'/'+st.ilerleme[1] : '') : st && st.durum==='hata' ? 'Veri alınamadı: ' + (st.hata||'') : 'Bu piyasa için henüz hesap yok.'; return; }
  $('main').hidden = false;
  $('meta').textContent = d.ad + ' · son tam kapanış ' + trd(d.son) + ' · ' + d.evren + ' sembol' + (st && st.durum==='guncelleniyor' ? ' · güncelleniyor…' : '');
  const uy = (d.uyari || []).slice(); if(st && st.durum==='hata') uy.unshift('Son güncelleme yapılamadı: ' + (st.hata||''));
  if(uy.length){ $('warn').hidden = false; $('warn').textContent = uy.join(' · '); }
  const book = $('book'); book.textContent = '';
  const g = (d.ozkaynak - 1) * 100;
  const add = (lab, val, cls) => { const s = el('div', 'stat'); s.append(el('b', cls||null, val), el('span', null, lab)); book.append(s); };
  add(mk==='bist' ? 'Defter getirisi (faiz üstü)' : 'Defter getirisi', pct(g), g>0?'pos':g<0?'neg':null);
  add('Pozisyondaki pay', nf(d.nominal, 1) + '%');
  add('Kapanan kampanya', d.kapanan_n + (d.kapanan_n ? ' · ' + d.kazanan_n + ' kârlı' : ''));
  add('Kayıt başlangıcı', trd(d.t0) + ' sonrası');
  const td = $('today'); td.textContent = '';
  if(!d.bugun.length) td.append(el('div', 'empty', 'Yeni giriş, ekleme ya da çıkış yok.'));
  d.bugun.forEach(o=>{ const r = el('div', 'ev'); const ad = o.tur==='giris' ? 'İlk giriş' : o.tur==='ekleme' ? o.no + '. parça eklendi' : o.tur==='cikis-stop' ? 'Stoptan çıkış' : '20 günlük dipten çıkış'; r.append(el('span', null, o.s + ' · ' + ad), el('b', null, px(o.f))); td.append(r); });
  const cap = num($('cap').value), parca = isFinite(cap) ? cap * data.kural.parca : NaN;
  const op = $('open'); op.textContent = '';
  const acik = d.satir.filter(r=>r.kademe>0).sort((a,b)=> (b.resmi-a.resmi) || a.s.localeCompare(b.s));
  if(!acik.length) op.append(el('div', 'empty', 'Açık pozisyon yok.'));
  acik.forEach(r=>{ const c = el('div', 'card'); const top = el('div', 'top'); const left = el('div'); left.append(el('span', 'sym', r.s));
    const dots = el('span', 'dots'); dots.setAttribute('aria-label', r.kademe + ' / ' + data.kural.maxp + ' parça'); for(let i=0;i<data.kural.maxp;i++) dots.append(el('i', i<r.kademe?'on':null)); left.append(dots);
    top.append(left); if(!r.resmi) top.append(el('span', 'tag', 'kayıt öncesi')); else top.append(el('b', r.kz>0?'pos':r.kz<0?'neg':null, pct(r.kz))); c.append(top);
    const k = el('div', 'kv'); kv(k, 'İlk giriş', px(r.ilk_f) + ' · ' + trd(r.ilk_t)); kv(k, 'Sabit stop', px(r.stop));
    kv(k, 'Çıkış seviyesi', px(r.cikis)); kv(k, 'Sonraki parça', r.sonraki==null ? 'dolu (4/4)' : px(r.sonraki) + (isFinite(parca) ? ' · ' + nf(parca / r.sonraki, parca / r.sonraki < 10 ? 3 : 0) + ' adet' : ''));
    kv(k, 'Kapanış', px(r.k)); kv(k, '20 günlük dip', px(r.alt)); c.append(k); op.append(c); });
  const nr = $('near'); nr.textContent = ''; row(nr, ['Sembol', 'Alış tetiği', 'Uzaklık', isFinite(parca) ? 'Parça adedi' : 'Girilirse stop'], true);
  d.satir.filter(r=>!r.kademe && r.evrende).sort((a,b)=>a.uzak-b.uzak).slice(0, 12).forEach(r=> row(nr, [r.s, px(r.ust), pct(r.uzak), isFinite(parca) ? nf(parca / r.ust, parca / r.ust < 10 ? 3 : 0) : px(r.girilirse_stop)]));
  const al = $('all'); al.textContent = ''; row(al, ['Sembol', 'Kapanış', '55 üst', '20 alt', 'ATR14', 'Parça'], true);
  d.satir.slice().sort((a,b)=>a.s.localeCompare(b.s)).forEach(r=> row(al, [r.s, px(r.k), px(r.ust), px(r.alt), px(r.atr), r.kademe ? r.kademe + '/' + data.kural.maxp : '–']));
  const cl = $('closed'); cl.textContent = '';
  if(!d.kapanan.length) cl.append(el('div', 'empty', 'Kayıt başlangıcından beri kapanan kampanya yok.'));
  d.kapanan.slice(0, 40).forEach(x=>{ const r = el('div', 'ev'); r.append(el('span', null, x.s + ' · ' + trd(x.ilk_t) + ' → ' + trd(x.cik_t) + ' · ' + x.kademe + ' parça · ' + (x.neden==='stop'?'stop':'20 günlük dip')), el('b', x.getiri>0?'pos':'neg', pct(x.getiri))); cl.append(r); });
  $('note').textContent = 'Kural: gün içi 55 günlük tepe kırılımında ilk parça (defterin %0,5\'i); pozisyon açıkken her yeni tepe gününde bir parça, en çok 4. Stop ilk girişin 2 × ATR14 altında sabit. 20 günlük dibe değince hepsi kapanır. Alış tetiği ve çıkış seviyesi bir sonraki işlem günü için geçerlidir. Kâğıt kaydı; gerçek emir yok. Maliyet yön başına ' + nf(d.maliyet * 100, 2) + '%' + (mk==='bist' ? '; getiri faiz üstü (faiz ' + nf(d.faiz, 1) + '%).' : mk==='kripto' ? '; fonlama dahil.' : '.');
}
async function load(){ try{ const r = await fetch('api/pyr', {cache:'no-store'}); data = await r.json(); }catch(e){ $('meta').textContent = 'Sunucuya ulaşılamadı.'; return; } render(); }
document.querySelectorAll('.seg button').forEach(b=>b.addEventListener('click', ()=>{ mk = b.dataset.m; try{ localStorage.setItem('pyrm', mk); }catch(e){} render(); }));
try{ const c = localStorage.getItem('pyrcap'); if(c) $('cap').value = c; }catch(e){}
$('cap').addEventListener('input', ()=>{ try{ localStorage.setItem('pyrcap', $('cap').value); }catch(e){} render(); });
load(); setInterval(()=>{ if(document.visibilityState==='visible') load(); }, 120000);
</script>
</body></html>
"""


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

.scan{display:flex;flex-direction:column;gap:8px}
.scanhead{display:flex;justify-content:space-between;align-items:center;gap:10px}
.scanmeta{font-size:12.5px;color:var(--muted)}
button.mini{font:inherit;font-size:13px;font-weight:650;color:var(--accent);background:transparent;border:1px solid var(--line);border-radius:999px;padding:6px 12px;cursor:pointer;white-space:nowrap}
.siglist{display:flex;flex-direction:column;border-top:1px solid var(--line)}
.sig{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px;padding:11px 2px;border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--fg);font:inherit;text-align:left;cursor:pointer;width:100%}
.sig .tk{font-weight:750;font-size:16px;letter-spacing:.02em;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sig .age{font-size:11px;font-weight:650;letter-spacing:.04em;padding:2px 7px;border-radius:999px;background:var(--gain-bg);color:var(--gain)}
.sig .age.old{background:var(--warn-bg);color:var(--warn)}
.sig .px2{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:15px;text-align:right}
.sig .sub2{font-size:12.5px;color:var(--muted);min-width:0}
.sig .sub2.r{text-align:right;font-family:var(--mono)}
.empty{font-size:13.5px;color:var(--muted);padding:10px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.tabp{gap:16px}
nav.tabs{position:fixed;left:0;right:0;bottom:0;display:grid;grid-template-columns:repeat(5,1fr);background:var(--surface);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom,0px);z-index:5}
nav.tabs button{font:inherit;font-size:11px;font-weight:650;letter-spacing:.01em;display:flex;flex-direction:column;align-items:center;gap:3px;padding:9px 2px 8px;position:relative;min-width:0;border:0;background:transparent;color:var(--muted);cursor:pointer}
nav.tabs button[aria-pressed="true"]{color:var(--accent)}
body{padding-bottom:calc(84px + env(safe-area-inset-bottom,0px))}
nav.tabs .badge{position:absolute;top:4px;left:calc(50% + 7px);min-width:16px;height:16px;border-radius:8px;background:var(--loss);color:var(--surface);font-size:10.5px;line-height:16px;padding:0 4px;font-weight:700}
.seg.plain button[aria-pressed="true"]{background:var(--accent);color:var(--accent-fg)}
#pfT{text-transform:uppercase;font-weight:650;letter-spacing:.03em}
button.copy:disabled{opacity:.45;cursor:default}
button.wide{width:100%}
.plist{display:flex;flex-direction:column;gap:10px;margin-top:8px}
.pcard{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px;display:flex;flex-direction:column;gap:9px}
.pcard[data-s="cikis"],.pcard[data-s="stop"]{border-color:var(--loss)}
.ptop{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.ptop b{font-family:var(--mono);font-variant-numeric:tabular-nums}
.psym{font-weight:750;font-size:17px;letter-spacing:.02em}
.pdots{display:inline-flex;gap:4px;vertical-align:middle;margin-left:8px} .pdots i{width:9px;height:9px;border-radius:50%;border:1.5px solid var(--accent);display:block} .pdots i.on{background:var(--accent)}
.pkv{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px}
.pkv div{display:flex;flex-direction:column;min-width:0} .pkv span{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:650}
.pkv b{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:15px;font-weight:600;overflow-wrap:anywhere}
.pflag{border-radius:8px;padding:8px 10px;font-size:13.5px;font-weight:700;background:var(--loss-bg);color:var(--loss)}
.pos{color:var(--gain)} .neg{color:var(--loss)}
.pev{display:flex;flex-direction:column;gap:1px;padding:9px 0;border-bottom:1px solid var(--line)} .pev:first-child{border-top:1px solid var(--line)}
.pev b{font-size:14.5px} .pev span{font-size:13px;color:var(--muted)} .pev.t-cikis b,.pev.t-stop b{color:var(--loss)} .pev.t-ekleme.new b{color:var(--accent)}
.pev .yeni{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--accent-fg);background:var(--accent);border-radius:999px;padding:1px 7px;margin-left:8px;vertical-align:1px}
.pfsec{display:flex;flex-direction:column;gap:6px}
details.box{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:0 14px} details.box summary{cursor:pointer;padding:13px 0;font-weight:650}
details.box .in{display:flex;flex-direction:column;gap:10px;padding-bottom:14px;font-size:14px;align-items:flex-start}
code.topic{font-family:var(--mono);font-size:15px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;display:block;overflow-wrap:anywhere;user-select:all;-webkit-user-select:all;align-self:stretch}
</style></head>
<body>
<div class="wrap">
  <header><h1>ATR Stop</h1><span class="eyebrow">2×ATR stop · 2R hedef</span></header>

  <div id="tabScan" class="wrap tabp">
  <section class="scan" aria-label="Tarama">
    <div class="scanhead"><span class="lab">ABD · 55 günlük kırılım · long</span><button type="button" class="mini" id="rescan">Yenile</button></div>
    <div class="scanmeta" id="scanmeta">Tarama sonucu yükleniyor…</div>
    <div id="siglist"></div>
  </section>
  <div class="note">Sinyal: günlük kapanış, önceki 55 günün en yükseğini yukarı keser. Liste ABD kapanışından sonra kendiliğinden güncellenir; gün içi fiyat hareketleri kapanışa kadar listeye girmez. Bir hisseye dokununca hesaplayıcıda açılır.</div>
  </div>

  <div id="tabBist" class="wrap tabp" hidden>
  <section class="scan" aria-label="BIST taraması">
    <div class="scanhead"><span class="lab">BIST 50 · 55 günlük kırılım · long</span><button type="button" class="mini" id="rescanB">Yenile</button></div>
    <div class="scanmeta" id="bscanmeta">Tarama sonucu yükleniyor…</div>
    <div id="bsiglist"></div>
  </section>
  <div class="note">Sinyal: günlük kapanış, önceki 55 günün en yükseğini yukarı keser. Liste BIST kapanışından sonra kendiliğinden güncellenir. Bir hisseye dokununca Portföy sekmesinde ekleme formu açılır.</div>
  </div>

  <div id="tabPf" class="wrap tabp" hidden>
    <div id="pfPin" class="wrap" hidden>
      <div class="field"><label for="pfPinIn" id="pfPinLab">PIN</label><input id="pfPinIn" type="password" inputmode="numeric" autocomplete="off" placeholder="en az 4 hane" enterkeyhint="done"></div>
      <div class="info" id="pfPinInfo"></div>
      <button type="button" class="copy wide" id="pfPinBtn">Giriş</button>
    </div>
    <div id="pfMain" class="wrap" hidden>
      <div class="seg plain" role="group" aria-label="Piyasa"><button type="button" id="pfUs" aria-pressed="true">ABD</button><button type="button" id="pfBist" aria-pressed="false">BIST</button></div>
      <div class="form">
        <div class="field"><label for="pfT">Hisse</label><input id="pfT" autocomplete="off" autocapitalize="characters" autocorrect="off" spellcheck="false" placeholder="QCOM" enterkeyhint="next"></div>
        <div class="field"><label for="pfE" id="pfElab">Giriş fiyatı ($)</label><input id="pfE" class="num" inputmode="decimal" placeholder="0,00" enterkeyhint="done"></div>
        <div class="field full"><div class="info" id="pfInfo">Hisse kodunu ve giriş fiyatını yaz; stopu ben hesaplarım.</div></div>
        <div class="field full"><button type="button" class="copy wide" id="pfAdd" disabled>Portföye ekle</button></div>
      </div>
      <div class="pfsec"><span class="lab">Pozisyonlar</span><div class="plist" id="pfList"></div></div>
      <div class="pfsec"><span class="lab">Uyarılar</span><div id="pfAlerts"></div></div>
      <details class="box" id="pfSetup"><summary>Telefon bildirimi kurulumu</summary><div class="in">
        <span>1. Telefona ücretsiz <b>ntfy</b> uygulamasını kur (Play Store ya da App Store).</span>
        <span>2. Uygulamada “+” ile yeni konuya abone ol ve şu konu adını yaz:</span>
        <code class="topic" id="pfTopic"></code>
        <button type="button" class="mini" id="pfCopyTopic">Konu adını kopyala</button>
        <span>3. Deneme bildirimi gönder; telefona düşüyorsa kurulum tamam.</span>
        <button type="button" class="copy wide" id="pfTest">Deneme bildirimi gönder</button>
        <span class="copied" id="pfTestInfo" role="status"></span>
        <span class="scanmeta">Konu adı şifre gibidir; kimseyle paylaşma. Bildirim metni ntfy.sh sunucusundan geçer. <span id="pfTg"></span></span>
      </div></details>
      <div class="note">Ekleme uyarısı: fiyat önceki 55 günün tepesine değerse; hisse başına en çok 3, günde en çok 1, eklediğin günden sonraki günlerde. Çıkış uyarısı: fiyat önceki 20 günün dibine değerse. Stop = giriş − 2 × ATR(14), eklerken sabitlenir. Çıkış ya da stop uyarısından sonra o hisse için başka uyarı gelmez. Seans açıkken ABD 2 dakikada, BIST 3 dakikada bir denetlenir; BIST fiyatı gecikmeli gelebilir (kartta son fiyatın saati yazar). Uyarıdır, emir vermez.</div>
    </div>
  </div>

  <div id="tabCalc" class="wrap tabp" hidden>

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
    <div class="actions"><button type="button" class="copy" id="copy">Seviyeleri kopyala</button><button type="button" class="mini" id="toPf">Portföye ekle</button><span class="copied" id="copied" role="status"></span></div>

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

  <div class="note">Stop = giriş ∓ 2 × ATR(14). Kâr al = giriş ± 4 × ATR(14). ATR, hissenin ABD borsasındaki günlük barlarından hesaplanır; seans açıkken bugünün barını da içerir ve dakikada bir güncellenir. Stop ve kâr al senin yazdığın giriş fiyatına göre yerleşir. Komisyon ve fonlama dahil değil.</div>
  </div>
</div>

<nav class="tabs" aria-label="Sekmeler">
  <button type="button" id="navScan" aria-pressed="true"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>ABD</span></button>
  <button type="button" id="navBist" aria-pressed="false"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M4 6h10M4 12h16M4 18h16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>BIST</span></button>
  <button type="button" id="navCalc" aria-pressed="false"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M3 12h18M7 7v10M17 7v10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Hesaplayıcı</span></button>
  <button type="button" id="navPf" aria-pressed="false"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M4 8h16v11H4zM9 8V5h6v3M4 13h16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Portföy</span><span class="badge" id="navBadge" hidden></span></button>
  <button type="button" id="navPyr" aria-pressed="false"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M4 19h16M7 19v-4M12 19V9M17 19V5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Pyramid</span></button>
</nav>

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
  const when = rec.atr_live ? (rec.session_open ? 'canlı, bugünün barı dahil'+(rec.live_time?' (New York saati '+rec.live_time+')':'') : rec.live_date_tr+' seansı dahil') : rec.bar_date_tr+' kapanışına kadar (seans kapalı)';
  a.textContent = rec.ticker+(rec.name?' · '+rec.name:'')+' · '+(rec.exchange||'ABD')+'. ATR(14) '+f(rec.atr)+' $ ('+pct(rec.atr/rec.close)+'), '+when+'.';
  const parts=[a];
  if(rec.price>0){ const b=document.createElement('button'); b.type='button'; b.className='chip'; b.textContent=(rec.price_live?'Son fiyat ':'Son kapanış ')+f(rec.price)+' $ · giriş olarak kullan'; b.addEventListener('click',()=>{ el.entry.value=f(rec.price); render(); }); parts.push(b); }
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
  $('toPf').hidden = dir!==1;
  $('stopPx').textContent=f(r.stop); $('stopSub').textContent=sg+f(r.R)+' $ · '+sg+pct(r.stopDist);
  $('tpPx').textContent=f(r.tp); $('tpSub').textContent=sg2+f(2*r.R)+' $ · '+sg2+pct(2*r.stopDist);
  ladder(entry, r);
  const rows=[['ATR(14), günlük'+(rec.atr_live&&rec.session_open?' · canlı':''), f(rec.atr)+' $'], ['Risk (1R = 2×ATR)', f(r.R)+' $ · '+pct(r.stopDist)], ['Hedef (2R = 4×ATR)', f(2*r.R)+' $ · '+pct(2*r.stopDist)], ['Veri', (rec.exchange||'ABD')+' · '+(rec.atr_live?rec.live_date_tr+(rec.live_time?' '+rec.live_time:''):rec.bar_date_tr)]];
  if(rec.atr_live && rec.today) rows.splice(1,0,['Bugünün aralığı', f(rec.today.l)+' – '+f(rec.today.h)+' $']);
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

// ---- tarama ----
let scanTimer = null;
const AGE = ['son kapanış','1 gün önce','2 gün önce'];
function pick(x){
  clearTimeout(timer); seq++;
  el.ticker.value = x.ticker; el.entry.value = f(x.last_close);
  rec = {ok:true, ticker:x.ticker, name:'', exchange:'', atr:x.atr, close:x.close, price:x.last_close, price_live:false, bar_date_tr:x.date_tr};
  recFor = x.ticker; setDir(1); showRec(); render();
  showTab('calc');
  load2(x.ticker);
}
async function load2(t){   // güncel veriyi çek: canlı ATR, şirket adı, borsa ve son fiyat
  const my = seq;
  try{ const r = await fetch('api/atr?ticker='+encodeURIComponent(t), {cache:'no-store'}); const j = await r.json(); if(my!==seq || !j.ok || !rec || rec.ticker!==t) return;
    rec=j; showRec(); render(); }catch(e){}
}
function drawScan(j){
  const meta=$('scanmeta'), list=$('siglist'); list.textContent='';
  const res=j.result;
  if(j.status==='taraniyor'){ meta.textContent='Taranıyor: '+j.progress[0]+' / '+j.progress[1]+' hisse'+(res?' · aşağıda önceki sonuç ('+res.bar_date_tr+')':''); }
  else if(j.status==='hata' && !res){ meta.textContent='Tarama yapılamadı: '+(j.error||'bilinmeyen hata')+'. Yenile ile yeniden dene.'; return; }
  if(!res){ if(j.status!=='taraniyor') meta.textContent='Henüz tarama yapılmadı; birkaç dakika içinde ilk sonuç gelir.'; return; }
  if(j.status!=='taraniyor'){ const d=new Date(res.updated*1000); meta.textContent=res.bar_date_tr+' kapanışı · '+res.scanned_n+' hisse tarandı · '+res.signals.length+' sinyal · güncelleme '+d.toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'}); }
  if(!res.signals.length){ const e=document.createElement('div'); e.className='empty'; e.textContent='Son üç günde kırılım veren hisse yok.'; list.appendChild(e); return; }
  const wrap=document.createElement('div'); wrap.className='siglist';
  for(const x of res.signals){
    const b=document.createElement('button'); b.type='button'; b.className='sig';
    const tk=document.createElement('span'); tk.className='tk'; tk.textContent=x.ticker;
    const ag=document.createElement('span'); ag.className='age'+(x.age>0?' old':''); ag.textContent=AGE[x.age]||(x.age+' gün önce'); tk.appendChild(ag);
    const px=document.createElement('span'); px.className='px2'; px.textContent=f(x.close)+' $';
    const venue=el.venue.value, c=calc(x.close, x.atr, 1, 10, venue);
    const s1=document.createElement('span'); s1.className='sub2'; s1.textContent=Object.keys(x.venues).map(k=>k==='bybit'?'Bybit':'Binance').join(' · ')+(c&&!c.bad?' · en fazla '+c.maxLev+' kat':'');
    const s2=document.createElement('span'); s2.className='sub2 r'; s2.textContent=c&&!c.bad?'stop −'+pct(c.stopDist):'';
    b.append(tk,px,s1,s2); b.addEventListener('click',()=>pick(x)); wrap.appendChild(b);
  }
  list.appendChild(wrap);
}
async function loadScan(){
  clearTimeout(scanTimer);
  try{ const r=await fetch('api/scan',{cache:'no-store'}); const j=await r.json(); drawScan(j); if(j.status==='taraniyor' || (!j.result && j.status!=='hata')) scanTimer=setTimeout(loadScan, 5000); }
  catch(e){ $('scanmeta').textContent='Tarama sonucu alınamadı; bağlantını kontrol et.'; }
}
$('rescan').addEventListener('click', async ()=>{ $('scanmeta').textContent='Tarama isteniyor…'; try{ const r=await fetch('api/scan/run',{cache:'no-store'}); const j=await r.json(); if(!j.started) $('scanmeta').textContent='Tarama zaten sürüyor ya da az önce yapıldı; son sonuç gösteriliyor.'; }catch(e){} setTimeout(loadScan, 1500); });
el.venue.addEventListener('change', loadScan);
loadScan();

// ---- seans açıkken ATR'yi dakikada bir tazele ----
setInterval(()=>{ if(rec && rec.session_open && !$('tabCalc').hidden && document.visibilityState==='visible') load2(rec.ticker); }, 60000);

// ---- BIST taraması ----
let bTimer = null;
const mk = (t,c,txt) => { const e=document.createElement(t); if(c) e.className=c; if(txt!=null) e.textContent=txt; return e; };
function drawBist(j){
  const meta=$('bscanmeta'), list=$('bsiglist'); list.textContent=''; const res=j.result;
  if(j.status==='taraniyor'){ meta.textContent='Taranıyor: '+j.progress[0]+' / '+j.progress[1]+' hisse'+(res?' · aşağıda önceki sonuç ('+res.bar_date_tr+')':''); }
  else if(j.status==='hata' && !res){ meta.textContent='Tarama yapılamadı: '+(j.error||'bilinmeyen hata')+'. Yenile ile yeniden dene.'; return; }
  if(!res){ if(j.status!=='taraniyor') meta.textContent='Henüz tarama yapılmadı; birkaç dakika içinde ilk sonuç gelir.'; return; }
  if(j.status!=='taraniyor'){ const d=new Date(res.updated*1000); meta.textContent=res.bar_date_tr+' kapanışı · '+res.scanned_n+' hisse tarandı'+(res.failed_n?' ('+res.failed_n+' hissenin verisi alınamadı)':'')+' · '+res.signals.length+' sinyal · güncelleme '+d.toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'})+(res.liste?' · liste: '+res.liste:''); }
  if(!res.signals.length){ list.appendChild(mk('div','empty','Son üç günde kırılım veren hisse yok.')); return; }
  const wrap=mk('div','siglist');
  for(const x of res.signals){
    const b=mk('button','sig'); b.type='button';
    const tk=mk('span','tk',x.ticker); tk.appendChild(mk('span','age'+(x.age>0?' old':''), AGE[x.age]||(x.age+' gün önce')));
    b.append(tk, mk('span','px2',f(x.close)+' ₺'), mk('span','sub2','ATR(14) '+f(x.atr)+' ₺'), mk('span','sub2 r','stop −'+pct(2*x.atr/x.close)));
    b.addEventListener('click',()=>pfPrefill('bist', x.ticker, x.last_close)); wrap.appendChild(b);
  }
  list.appendChild(wrap);
}
async function loadBist(){
  clearTimeout(bTimer);
  try{ const r=await fetch('api/bist/scan',{cache:'no-store'}); const j=await r.json(); drawBist(j); if(j.status==='taraniyor' || (!j.result && j.status!=='hata')) bTimer=setTimeout(loadBist, 5000); }
  catch(e){ $('bscanmeta').textContent='Tarama sonucu alınamadı; bağlantını kontrol et.'; }
}
$('rescanB').addEventListener('click', async ()=>{ $('bscanmeta').textContent='Tarama isteniyor…'; try{ const r=await fetch('api/bist/scan/run',{cache:'no-store'}); const j=await r.json(); if(!j.started) $('bscanmeta').textContent='Tarama zaten sürüyor ya da az önce yapıldı; son sonuç gösteriliyor.'; }catch(e){} setTimeout(loadBist, 1500); });

// ---- portföy ----
let pfM='us', pfD=null, pfSeq=0, pfTm=null, pfPinMem='';
try{ if(localStorage.getItem('atrpfm')==='bist') pfM='bist'; }catch(e){}
const getPin = () => { try{ return localStorage.getItem('atrpin') || pfPinMem; }catch(e){ return pfPinMem; } };
const setPin = p => { pfPinMem=p; try{ localStorage.setItem('atrpin', p); }catch(e){} };
const getSeen = () => { try{ return parseInt(localStorage.getItem('atrseen')||'0',10)||0; }catch(e){ return 0; } };
const cur = m => m==='us' ? ' $' : ' ₺';
const trd = d => { if(!d) return ''; const p=d.split('-'); return p[2]+'.'+p[1]+'.'+p[0]; };
async function pfPost(path, body){ const r=await fetch('api/pf/'+path,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Pin':getPin()},body:JSON.stringify(body||{})}); return r.json(); }
function pfInfoSet(t, bad){ const i=$('pfInfo'); i.className='info'+(bad?' bad':''); i.textContent=t; }
function pfBadge(){
  const b=$('navBadge'); let n=0; if(pfD && pfD.ok && pfD.uyari){ const s=getSeen(); n=pfD.uyari.filter(u=>u.zaman>s).length; }
  if(curTab==='pf') n=0; b.hidden=!n; b.textContent=n>9?'9+':String(n);
}
function pfSeen(){ if(pfD && pfD.ok && pfD.uyari && pfD.uyari.length){ try{ localStorage.setItem('atrseen', String(Math.max(...pfD.uyari.map(u=>u.zaman)))); }catch(e){} } }
async function pfLoad(){
  let d; try{ const r=await fetch('api/pf',{cache:'no-store',headers:{'X-Pin':getPin()}}); d=await r.json(); }catch(e){ if(curTab==='pf' && !pfD){ $('pfPin').hidden=true; $('pfMain').hidden=false; pfInfoSet('Sunucuya ulaşılamadı. Bağlantını kontrol et.', true); } return; }
  pfD=d; pfDraw(); pfBadge();
}
function pfDraw(){
  const d=pfD; if(!d) return;
  const needPin = d.pin_var===false || d.yetki===false;
  $('pfPin').hidden=!needPin; $('pfMain').hidden=needPin;
  if(needPin){
    const ilk = d.pin_var===false;
    $('pfPinLab').textContent = ilk ? 'Portföy için bir PIN belirle' : 'PIN'; $('pfPinBtn').textContent = ilk ? 'PIN’i kaydet' : 'Giriş';
    const i=$('pfPinInfo'); i.className='info'+(d.error?' bad':''); i.textContent = d.error || (ilk ? 'Portföyünü ve uyarılarını yalnız bu PIN’i bilen görür ve değiştirir. Bu telefonda bir kez girmen yeter.' : 'Portföyü görmek için PIN’i gir.');
    return;
  }
  const seen=getSeen(), yeni = m => d.uyari.filter(u=>u.m===m && u.zaman>seen).length;
  $('pfUs').setAttribute('aria-pressed', pfM==='us'); $('pfBist').setAttribute('aria-pressed', pfM==='bist');
  $('pfUs').textContent='ABD'+(pfM!=='us'&&yeni('us')?' · '+yeni('us')+' yeni':''); $('pfBist').textContent='BIST'+(pfM!=='bist'&&yeni('bist')?' · '+yeni('bist')+' yeni':'');
  $('pfElab').textContent='Giriş fiyatı ('+(pfM==='us'?'$':'₺')+')'; $('pfT').placeholder = pfM==='us'?'QCOM':'THYAO';
  const list=$('pfList'); list.textContent='';
  const poz=d.poz.filter(p=>p.m===pfM).sort((a,b)=>((a.durum==='acik')-(b.durum==='acik')) || a.s.localeCompare(b.s));
  if(!poz.length) list.append(mk('div','empty',(pfM==='us'?'ABD':'BIST')+' portföyünde hisse yok. Yukarıdan ekle.'));
  for(const p of poz){
    const c=mk('div','pcard'); c.dataset.s=p.durum; const cv=p.canli||{}, cu=cur(p.m);
    const top=mk('div','ptop'), left=mk('div'); left.append(mk('span','psym',p.s));
    const dots=mk('span','pdots'); dots.setAttribute('role','img'); dots.setAttribute('aria-label', p.ekleme+' / '+d.max_ek+' ekleme uyarısı'); for(let i=0;i<d.max_ek;i++) dots.append(mk('i', i<p.ekleme?'on':null)); left.append(dots);
    const kz = cv.fiyat ? cv.fiyat/p.giris-1 : null;
    top.append(left, mk('b', kz==null?null:kz>0?'pos':kz<0?'neg':null, kz==null?'':(kz>=0?'+':'−')+pct(Math.abs(kz),2))); c.append(top);
    if(p.durum!=='acik' && p.bitis) c.append(mk('div','pflag',(p.durum==='stop'?'STOP uyarısı':'ÇIKIŞ uyarısı')+' · '+f(p.bitis.seviye)+cu+' · '+trd(p.bitis.gun)));
    const k=mk('div','pkv'); const kv=(a,b)=>{ const x=mk('div'); x.append(mk('span',null,a), mk('b',null,b)); k.append(x); };
    kv('Giriş', f(p.giris)+cu); kv('Stop · 2×ATR', f(p.stop)+cu);
    kv('Son fiyat'+(cv.saat?' · '+cv.saat:''), cv.fiyat?f(cv.fiyat)+cu:'–'); kv('Ekleme uyarısı', p.ekleme+' / '+d.max_ek);
    kv('55 günlük tepe', cv.tepe55?f(cv.tepe55)+cu:'–'); kv('20 günlük dip', cv.dip20?f(cv.dip20)+cu:'–'); c.append(k);
    if(p.hata) c.append(mk('span','sub2','Son denetimde veri alınamadı: '+p.hata));
    const rm=mk('button','mini', p.durum==='acik'?'Portföyden çıkar':'Kaldır'); rm.type='button';
    rm.addEventListener('click', async ()=>{ if(!confirm(p.s+' portföyden çıkarılsın mı? Bu hisse için uyarılar durur.')) return; try{ await pfPost('sil',{id:p.id}); }catch(e){} pfLoad(); });
    const act=mk('div','actions'); act.append(rm); c.append(act); list.append(c);
  }
  const al=$('pfAlerts'); al.textContent=''; const ua=d.uyari.filter(u=>u.m===pfM).slice(0,30);
  if(!ua.length) al.append(mk('div','empty','Henüz uyarı yok.'));
  for(const u of ua){ const r=mk('div','pev t-'+u.tur+(u.zaman>seen?' new':'')); const dt=new Date(u.zaman*1000); const hb=mk('b',null,u.baslik); if(u.zaman>seen) hb.append(mk('span','yeni','yeni'));
    r.append(hb, mk('span',null,u.metin), mk('span',null,dt.toLocaleString('tr-TR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+(u.kanal&&u.kanal.length?' · iletildi: '+u.kanal.join(', '):' · telefona iletilemedi'))); al.append(r); }
  $('pfTopic').textContent=d.ntfy||''; $('pfTg').textContent=d.telegram?'Telegram da ayarlı; uyarılar oraya da gidiyor.':'';
}
function pfOnInput(now){
  clearTimeout(pfTm); pfSeq++; $('pfAdd').disabled=true;
  const s=normT($('pfT').value), g=num($('pfE').value);
  if(!s){ pfInfoSet('Hisse kodunu ve giriş fiyatını yaz; stopu ben hesaplarım.'); return; }
  if(!(pfM==='us' ? /^[A-Z][A-Z0-9-]{0,6}$/ : /^[A-Z0-9]{3,6}$/).test(s)){ pfInfoSet('Hisse kodu geçersiz (örnek: '+(pfM==='us'?'QCOM, NVDA':'THYAO, GARAN')+').', true); return; }
  const m=pfM;
  pfTm=setTimeout(async ()=>{ const my=++pfSeq; pfInfoSet(s+' için veriler çekiliyor…');
    try{ const r=await fetch('api/pf/onizle?m='+m+'&s='+encodeURIComponent(s)+(g>0?'&giris='+g:''),{cache:'no-store'}); const j=await r.json(); if(my!==pfSeq) return;
      if(!j.ok){ pfInfoSet(j.error||'Veri alınamadı.', true); return; }
      const c=cur(m); let t=j.s+(j.ad && j.ad.toUpperCase()!==j.s?' · '+j.ad:'')+' · son fiyat '+f(j.fiyat)+c+' · ATR(14) '+f(j.atr)+c;
      if(g>0 && j.stop>0) t+=' · stop '+f(j.stop)+c+' (−'+pct((g-j.stop)/g)+')';
      if(j.tepe55) t+=' · 55 günlük tepe '+f(j.tepe55)+c; if(j.dip20) t+=' · 20 günlük dip '+f(j.dip20)+c;
      if(g>0 && !(j.stop>0)){ pfInfoSet('ATR giriş fiyatına göre çok büyük; fiyatı kontrol et.', true); return; }
      pfInfoSet(t); $('pfAdd').disabled=!(g>0);
    }catch(e){ if(my===pfSeq) pfInfoSet('Sunucuya ulaşılamadı.', true); } }, now?0:700);
}
function pfSetM(m){ pfM=m; try{ localStorage.setItem('atrpfm', m); }catch(e){} $('pfT').value=''; $('pfE').value=''; pfOnInput(true); pfDraw(); }
function pfPrefill(m, s, price){ pfM=m; try{ localStorage.setItem('atrpfm', m); }catch(e){} showTab('pf'); $('pfT').value=s; $('pfE').value=price>0?f(price):''; pfOnInput(true); }
$('pfUs').addEventListener('click', ()=>pfSetM('us')); $('pfBist').addEventListener('click', ()=>pfSetM('bist'));
$('pfT').addEventListener('input', ()=>pfOnInput(false)); $('pfE').addEventListener('input', ()=>pfOnInput(false));
$('pfT').addEventListener('keydown', e=>{ if(e.key==='Enter') $('pfE').focus(); });
$('pfAdd').addEventListener('click', async ()=>{ const s=normT($('pfT').value), g=num($('pfE').value), m=pfM; if(!(g>0)) return; $('pfAdd').disabled=true; pfInfoSet('Ekleniyor…');
  try{ const j=await pfPost('ekle',{m:m,s:s,giris:g}); if(!j.ok){ pfInfoSet(j.error||'Eklenemedi.', true); if(j.yetki===false) pfLoad(); return; }
    $('pfT').value=''; $('pfE').value=''; pfInfoSet(s+' eklendi. Stop '+f(j.stop)+cur(m)+'.'); pfLoad(); }
  catch(e){ pfInfoSet('Sunucuya ulaşılamadı.', true); } });
async function pfPinGo(){ const p=$('pfPinIn').value.trim(), i=$('pfPinInfo');
  if(!/^[A-Za-z0-9]{4,20}$/.test(p)){ i.className='info bad'; i.textContent='PIN 4–20 harf ya da rakam olmalı.'; return; }
  try{ if(pfD && pfD.pin_var===false){ const j=await pfPost('pin',{pin:p}); if(!j.ok){ i.className='info bad'; i.textContent=j.error||'PIN kaydedilemedi.'; return; } }
    setPin(p); $('pfPinIn').value=''; pfLoad(); }catch(e){ i.className='info bad'; i.textContent='Sunucuya ulaşılamadı.'; } }
$('pfPinBtn').addEventListener('click', pfPinGo); $('pfPinIn').addEventListener('keydown', e=>{ if(e.key==='Enter') pfPinGo(); });
$('pfCopyTopic').addEventListener('click', ()=>{ const t=$('pfTopic').textContent; if(navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(()=>{ $('pfTestInfo').textContent='Konu adı kopyalandı.'; }).catch(()=>{}); });
$('pfTest').addEventListener('click', async ()=>{ $('pfTestInfo').textContent='Gönderiliyor…'; try{ const j=await pfPost('deneme',{}); $('pfTestInfo').textContent = j.ok ? 'Gönderildi ('+j.kanal.join(', ')+'). Telefona düşmediyse konu adını kontrol et.' : 'Gönderilemedi: '+(j.error||''); }catch(e){ $('pfTestInfo').textContent='Sunucuya ulaşılamadı.'; } });
$('toPf').addEventListener('click', ()=>{ if(!rec) return; pfPrefill('us', rec.ticker, num(el.entry.value)); });
setInterval(()=>{ if(document.visibilityState==='visible' && getPin()) pfLoad(); }, 60000);
document.addEventListener('visibilitychange', ()=>{ if(document.visibilityState==='hidden' && curTab==='pf') pfSeen(); });

// ---- sekmeler ----
const TABS = {scan:'tabScan', bist:'tabBist', calc:'tabCalc', pf:'tabPf'}, NAVS = {scan:'navScan', bist:'navBist', calc:'navCalc', pf:'navPf'};
let curTab = 'scan';
function showTab(name){
  if(!TABS[name]) name='scan';
  if(curTab==='pf' && name!=='pf') pfSeen();
  curTab=name;
  for(const k in TABS){ $(TABS[k]).hidden = k!==name; $(NAVS[k]).setAttribute('aria-pressed', k===name); }
  window.scrollTo(0,0);
  try{ localStorage.setItem('atrtab', name); }catch(e){}
  if(name==='scan') loadScan(); else if(name==='bist') loadBist(); else if(name==='pf') pfLoad();
  pfBadge();
}
$('navScan').addEventListener('click', ()=>showTab('scan'));
$('navBist').addEventListener('click', ()=>showTab('bist'));
$('navCalc').addEventListener('click', ()=>showTab('calc'));
$('navPf').addEventListener('click', ()=>showTab('pf'));
$('navPyr').addEventListener('click', ()=>{ location.href = 'pyramid'; });
let ilkTab='scan'; try{ ilkTab = localStorage.getItem('atrtab') || 'scan'; }catch(e){}
if(ilkTab!=='scan') showTab(ilkTab); else if(getPin()) pfLoad();
</script>
</body></html>
"""
ICON = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAKOElEQVR42u3de3BU5RnH8d/uJiEJSUiEQLjHhHvuEBIKBKlaSiijIpdCoDrO9I+2dIYBrTJTi2CdKWKVcQrtjLZlOhQcQK4qiEhlYLiHAEFuEYLBcFFEAoQQEpLtH4wiCtk3ySE5J+f7+Y/wZndz8vzO87xnL/HIJiKn5vkF17i6cKnHDo/DQ8HDzYHwUPRwcxg8FD3cHAYPhQ83B8FD4cPNQfBS/HASq2vMQ+HDzd3AS/HDzd3AS/HDzSHwUPhw80jkpfjh5m7gpfjh5hB4KX64OQReDhfczGt1ogAndQEvxQ83h8BL8cPNIWAPAPYAnP3h1i7gpfjh5hAwAoERiLM/3NoF6ACgA3D2h1u7AB0AdADA9QFg/IEbxyA6AOgAgKsDwPgDt45BdADQAQACALiQh/kfdACAAAAEACAAAAEAXCCoJfwQOT376oNpL9bre/x+v1JmT9fpixeM1q+e+oIe6Ztqy5//F2++om2fHf3R1xf/epoeT89q1G3X1NaquqZGFVU3dKmiXOfKLqnk4gUdPvuF9pWcVP7nJ1RdU0MAmlNe9rB6f4/H49Hk7Bz9Zf0qToN18Hm98nm9Cg0O1gOtI5QYG6ehPft+9//llZV6vzBfb2/bpL2nTjACNbXwkFZ6IqNhZ7lJWTnyeDxUeSNEhIZqYtZQbX52jpb/5jl1io4hAE3p8YwstW4V2qDvjW/XXoMTe1PFFhmZnKGPn52jXh06EYCmMrkB409jxyfcW5eYtto4fZY6tokhAE1xsHO+N482xJiMbIWFhFC5FmobEam5435FAO772X/QsEbP8BGhoXosLYuqtdiYjGxldEsgAPfTpKwcy4IE6z2WPtD2j9Gxl0EHJfRSQmwHS25rWK9+6hLTVqWXLt77jLbw1Qbddn2uxf98/svaefJ4sxzPu913WHCI2oSHq1eHTnq0X5qeGfKw2oSFm99mUrrmrFtGB7hf44+JiqobgQ+Cx2NZN2lJrldX6fzlMm0tOqJZa97RwFf+oBNfnTP+fqtOUATgB8KCQzQmI9to7cyVi43W5Q0iAIGcv1ymGcsWGa8PD2ll+wsMjgzA6LRMRRm04oKSYv1nx5Y6R5tvJcbGKTuhF1UewNaiIyqruGa8PjqsNQGwfPwxvHa/PH+7/H6/Vu7badYFsukCgdT6/UYnlG9dvXGdAFipU3SMhvdJDriuprZWK/ftuhWEvduNbnts/0EKDQ6mygMwvfRcUXVD5ZWVBMBKE7Ny5DX4BWwtOqwvr5RJkg6dOa2j50oDfk9UWLhGp2ZS4XXweb3q9kA7o7UFJcVsgq1mOqYsz99xx79X/ODf9xyveE6gTj/tk6LI0DCjtZuOHCQAVsqM72H0QqvK6mqtO7C3zkDU9Qt2yutYmmP8fH3C00Zrb9ys1pJdWwlAc5z9N3xaoKuVd26+Tl+8oN3FRYEPiMejiVlDqXZJrYKC1SEqWjk9++rlJyZpzx/n6cF2Ztf2/7pxrb66etn2P2OQk34Z4wb8xGz8ucemd0X+DqNLnXnZOZq/6T1XFfvG6bMsu63NRwv1xkfrHPFzO6YDjErpr+jwwNeUyyquadPhu8+eqwp262Zt4Lfv9Y7rrAHdE2kBDfB+Yb7y3p7vmLdJOiYAppvTNfv3qKrm5l3/7+vyK/rk2KeWjlu4fWxnLFukvLfm63pVlWMetyMC0D6yjR7um2I2/uTXfc1/meFzAuMzB6tVEM8JBFJYWqIX3l2s1NnT9c9tHzvu8TsiAL/MGqIgry/gujNl32j7iWN1t+iD+UZnqOjw1hqV0p8KDyDY55NfflXfdOYnQzgiAKZvW3w3f4f8/ro/67ei6oY+KMxnDLJI345dNG/cU9ry/J8d8epPxwUgrWu8kjp1NRt/DMcb0+cEHumXqg5R0VS5gaROXbVpxmz1aN+RAFhpiuHm99j5Mzp05rTR2s1HC/XNtfKA64K8Pk0YOJjqNhQbGaUVv31OEaGhjnnMtn4eINjn07gBZgXYJ66zrixYcl/Gr79tXt/ii/f77wjzeb2KCY9QcueuGtN/kKYMekjBPp/R7STGxmneuKf0u/++RQdorJHJGWobEdnsrT2ta7yrzuQ1tbX6uvyKthw/rGnv/EvD572os2WX6tG1H3LM5y3ZOgB2+cyeKS5/gdyhM6f15N/n1uulzXPH8rEojdI2IlIjktJt8VjGDRhsPAK0VEfOlupPa5Yar0/v9qAjPhXCtgGYkDnENkXXNiJSI5MzXL/J/ff2/+nA6VPG62fmPmn7z161bQDs9iZ1PkLx1kfKv1SPjzlJ7txNo1MHEIAGbTy72GvjOSIpXe0iolwfgk+OHdKeU58Zr38+dwwBqC87visr2OfT+EyeE5CkVzesNl6b1iVeuTZ+SYntnge49eTTEKO1u4qLNOKNOY2+z/0vva7E2DijYP5jy4euD8CmIwdVUFKs/t3NPvtzZu4YbThUQAcw8Wi/VLWPbGO0dlXBLkvuc3XBbqN1qV26K7lzN1qApHkfmneBjG4JtrmiZ/sAmG42/X6/1u7fY8l9rtm/23jtZDbDkqT1hwpUWFpivH6mTfcCtgpAdHhr5aaYXW7cWVykc5cvWXK/haUlOnnhvNHaCQPNXppNF7hTZnwPW/6RQVsFoD5vQrFq/LndBcy6SWxklH6WlEb1S3rvYL6OnC01Xm/HLmCrAJiOF7V+v9Yd2GNtAArMxyDeJ3B7DH2tHl0gO6GXhvdOIgB30zuus/FVhR0njun85TJL7/9g6ecqvvCl0drclP6KCY8gAZJW79+t4+fPmHeBUWMJQGPPqqvrsWm9H5vhEF+QxvM+ge+68Wsb1xqvH5zYu9F/181KnsipeX5+jXArL4cABAAgAAABAAgAQAAAAgAQAIAAAAQAIAAAAQAIAEAAAAIAEACAAAAEACAAAAEACABAAAACABAAgAAABAAgAAABAAgA0NSa/K9EXlmw5Edfi/r9ZNseIKc9Xqdp7uNLBwAjEEAAAAIAEACAAAAEACAAAAEACADQ0gTZ4UF8ERbtqIP2zPQRVA4dACAAAAEACABAAAACABAAgAAABAAgAIAt2eKlEF2vl9n2AF25y9cWzf+IyrHImwuepgMABAAgAAABAAgAQAAAAgAQAIAAAAQAsJAncmqen8MAOgBAAAACABAAgAAABAAgAAABAAgAQAAAAgAQAMDRAbi6cKmHwwA3urpwqYcOAEYggAAAbg0A+wC4cf6nA4AOwCEAAWAMggvHHzoA6AAcAhAAxiC4cPyhA4AOECghQEs9+9MBQAcwTQrQ0s7+dXYAQoCWXvyMQGAEamhyAKef/Y06ACFASy1+4xGIEKAlFj97ALAHsDpRgFPO/vXuAIQALan4GzQCEQK0lOKXpEYVM39gD04tfEs2wXQDOLn4Gx0AQgAnF3+jRyBGIji18C3rAHQDOLX4Le8AdAM4pfDvewAIApwwVTTpyEIYYLdRutlmdsIAO+wfbbNpJRAUfHP4PwMQ+ODSvuARAAAAAElFTkSuQmCC")
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
        if p == "/pyramid":
            return self._send(200, PYR_PAGE, "text/html; charset=utf-8")
        if p == "/api/pyr":
            return self._send(200, json.dumps(pyr_api(), ensure_ascii=False), "application/json; charset=utf-8")
        if p == "/api/bist/scan":
            return self._send(200, json.dumps({"ok": True, "status": BSCAN["status"], "progress": BSCAN["progress"], "error": BSCAN["error"], "result": BSCAN["result"]}, ensure_ascii=False), "application/json; charset=utf-8")
        if p == "/api/bist/scan/run":
            if BSCAN["status"] != "taraniyor" and time.time() - BSCAN["started"] > 300:
                threading.Thread(target=bist_scan_run, daemon=True).start()
                return self._send(200, json.dumps({"ok": True, "started": True}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "started": False}), "application/json; charset=utf-8")
        if p == "/api/pf":
            return self._send(200, json.dumps(pf_api(self.headers.get("X-Pin") or ""), ensure_ascii=False), "application/json; charset=utf-8")
        if p == "/api/pf/onizle":
            q = parse_qs(u.query); m = (q.get("m") or [""])[0]; s = (q.get("s") or [""])[0].upper().strip().replace(".", "-")
            try:
                g = float((q.get("giris") or ["0"])[0])
            except Exception:
                g = 0.0
            if m not in ("us", "bist") or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,6}" if m == "us" else r"[A-Z0-9]{3,6}", s):
                return self._send(400, json.dumps({"ok": False, "error": "Geçersiz hisse kodu."}), "application/json; charset=utf-8")
            try:
                out = pf_onizle(m, s, g if g > 0 else None)[0]
            except LookupError as e:
                out = {"ok": False, "error": "“%s” için veri bulunamadı. Kodu kontrol et." % s}
            except Exception as e:
                out = {"ok": False, "error": "Veri alınamadı (%s). Biraz sonra yeniden dene." % str(e)[:120]}
            return self._send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
        if p == "/api/scan":
            return self._send(200, json.dumps({"ok": True, "status": SCAN["status"], "progress": SCAN["progress"], "error": SCAN["error"], "result": SCAN["result"]}, ensure_ascii=False), "application/json; charset=utf-8")
        if p == "/api/scan/run":
            if SCAN["status"] != "taraniyor" and time.time() - SCAN["started"] > 600:
                threading.Thread(target=run_scan, daemon=True).start()
                return self._send(200, json.dumps({"ok": True, "started": True}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "started": False}), "application/json; charset=utf-8")
        self._send(404, "bulunamadi", "text/plain; charset=utf-8")

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")
        if not p.startswith("/api/pf/"):
            return self._send(404, "bulunamadi", "text/plain; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4000:
                raise ValueError("buyuk")
            govde = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            if not isinstance(govde, dict):
                raise ValueError("bicim")
        except Exception:
            return self._send(400, json.dumps({"ok": False, "error": "Geçersiz istek."}), "application/json; charset=utf-8")
        try:
            out = pf_istek(p[len("/api/pf/"):], govde, self.headers.get("X-Pin") or "")
        except Exception as e:
            out = {"ok": False, "error": "Sunucu hatası: %s" % str(e)[:160]}
        return self._send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8105)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--test", metavar="HISSE", help="sunucuyu baslatmadan tek hisse icin sonucu ve her kaynagin durumunu yaz")
    ap.add_argument("--test-scan", metavar="N", type=int, nargs="?", const=0, default=None, help="taramayi dene: evreni kur, ilk N hisseyi (0 = hepsi) tara, sonucu yaz")
    ap.add_argument("--no-scan", action="store_true", help="arka plan taramasini kapat")
    ap.add_argument("--probe", metavar="HISSE", help="Nasdaq'in gun ici veri yanitinin yapisini yaz (canli ATR'yi dogrulamak icin)")
    ap.add_argument("--pyr-probe", action="store_true", help="Pyramid takibi icin veri kaynaklarini dene")
    ap.add_argument("--pyr-update", metavar="PIYASA", help="bist | us | kripto | hepsi: bir kez hesapla ve ozet yaz")
    ap.add_argument("--no-pyr", action="store_true", help="Pyramid arka plan takibini kapat")
    ap.add_argument("--no-pf", action="store_true", help="portfoy uyari dongusunu kapat")
    ap.add_argument("--bist-scan", action="store_true", help="BIST 50 taramasini bir kez calistir ve sonucu yaz")
    ap.add_argument("--find-key", metavar="KLASOR", help="Tiingo anahtarini bu klasorde ara, dene ve ~/.atr_env'e kaydet")
    a = ap.parse_args()
    load_env_file()
    if a.find_key:
        find_key(a.find_key); load_env_file()
        if not a.test:
            return
    if a.pyr_probe:
        pyr_probe(); pf_probe(); return
    if a.bist_scan:
        t0 = time.time(); res = bist_scan_run(verbose=True)
        if not res:
            print("BIST tarama hatasi:", BSCAN["error"]); return
        print("BIST 50: %d hisse tarandi, %d alinamadi | son bar %s | kaynak %s | %.0f sn" % (res["scanned_n"], res["failed_n"], res["bar_date"], res["source"], time.time() - t0))
        for x in res["signals"]:
            print("  %-6s %s (%d gun once) kapanis %.2f ATR %.2f stop %.2f" % (x["ticker"], x["date"], x["age"], x["close"], x["atr"], x["close"] - 2 * x["atr"]))
        return
    if a.pyr_update:
        for m in (("us", "bist") if a.pyr_update == "hepsi" else (a.pyr_update,)):
            t0 = time.time(); r = pyr_guncelle(m, sozlu=True)
            if not r:
                print("%s: HATA %s" % (m, PYR["durum"].get(m))); continue
            ac = [x for x in r["satir"] if x["kademe"]]
            print("%s: son kapanis %s | kayit baslangici %s sonrasi | %d sembol | acik pozisyon %d (resmi %d) | defter %.4f | uyari: %s | %.0f sn" % (
                r["ad"], r["son"], r["t0"], len(r["satir"]), len(ac), sum(1 for x in ac if x["resmi"]), r["ozkaynak"], "; ".join(r["uyari"]) or "yok", time.time() - t0))
        return
    if a.test:
        t = a.test.upper()
        for name, fn in source_list(t):
            t0 = time.time()
            try:
                d = fn(); print("  %-7s TAMAM  %d bar, son bar %s, ATR %.4f  (%.1f sn)" % (name, len(d["bars"]), d["bars"][-1][0], wilder_atr(d["bars"]) or 0, time.time() - t0))
            except Exception as e:
                print("  %-7s HATA   %s  (%.1f sn)" % (name, str(e)[:90], time.time() - t0))
        print(json.dumps(get_atr(t), ensure_ascii=False, indent=1)); return
    if a.probe:
        t = a.probe.upper()
        for name, url in (("chart", "https://api.nasdaq.com/api/quote/%s/chart?assetclass=stocks"), ("info", "https://api.nasdaq.com/api/quote/%s/info?assetclass=stocks")):
            try:
                d = json.loads(http_get(url % quote(t.replace("-", ".")), timeout=10, headers=NASDAQ_HD)).get("data") or {}
                print("[%s] anahtarlar: %s" % (name, sorted(d.keys())))
                for k in ("timeAsOf", "lastSalePrice", "previousClose", "marketStatus", "exchange", "companyName"):
                    if k in d:
                        print("   %s = %r" % (k, d[k]))
                if name == "chart":
                    pts = d.get("chart") or []
                    print("   nokta sayisi: %d | ilk: %s | son: %s" % (len(pts), json.dumps(pts[0])[:160] if pts else None, json.dumps(pts[-1])[:160] if pts else None))
                else:
                    print("   primaryData: %s" % json.dumps(d.get("primaryData"))[:300])
                    print("   keyStats: %s" % json.dumps(d.get("keyStats"))[:300])
            except Exception as e:
                print("[%s] HATA: %s" % (name, str(e)[:160]))
        try:
            td = nasdaq_today(t); print("grafikten seans bari ->", td)
            hb = nasdaq_hist(t)[-1]; print("resmi son gunluk bar  ->", hb)
            if td and td["date"] == hb[0]:
                print("   karsilastirma (grafik / resmi): yuksek %.2f / %.2f | dusuk %.2f / %.2f | kapanis %.2f / %.2f" % (td["h"], hb[2], td["l"], hb[3], td["c"], hb[4]))
        except Exception as e:
            print("nasdaq_today HATA:", str(e)[:160])
        print("New York saati: %s | seans acik: %s" % (ny_now().strftime("%Y-%m-%d %H:%M"), session_open()))
        r = get_atr(t); print(json.dumps({k: r.get(k) for k in ("ok", "atr", "atr_closed", "atr_live", "session_open", "live_time", "bar_date", "price", "today", "error")}, ensure_ascii=False))
        return
    if a.test_scan is not None:
        t0 = time.time(); uni = build_universe(force=True)
        print("Evren:", "; ".join(uni.get("notes", [])), "| hacmi yeterli hisse: %d" % len(uni["tickers"]))
        res = run_scan(limit=a.test_scan or None, verbose=True)
        if not res:
            print("Tarama hatasi:", SCAN["error"]); return
        print("Listeye giren: %d (aday %d) | veri yok: %d | kontrat uyusmuyor: %d | hata: %d | son bar: %s | sure: %.0f sn" % (res["scanned_n"], res["universe_n"], res["nodata_n"], res["mismatch_n"], res["failed_n"], res["bar_date"], time.time() - t0))
        print("Sinyal veren: %d" % len(res["signals"]))
        for x in res["signals"]:
            print("  %-6s %s (%d gun once) kapanis %.2f ATR %.2f stop %.2f kar al %.2f | %s" % (x["ticker"], x["date"], x["age"], x["close"], x["atr"], x["close"] - 2 * x["atr"], x["close"] + 4 * x["atr"], ", ".join("%s %s" % (k, v["sym"]) for k, v in x["venues"].items())))
        return
    try:
        SCAN["result"] = json.load(open(SCAN_FILE, encoding="utf-8")); SCAN["status"] = "hazir"
    except Exception:
        pass
    try:
        BSCAN["result"] = json.load(open(BIST_SCAN_FILE, encoding="utf-8")); BSCAN["status"] = "hazir"
    except Exception:
        pass
    if not a.no_scan:
        threading.Thread(target=scan_loop, daemon=True).start()
        threading.Thread(target=bist_scan_loop, daemon=True).start()
    if not a.no_pf:
        threading.Thread(target=pf_dongu, daemon=True).start()
    if not a.no_pyr:
        threading.Thread(target=pyr_dongu, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print("ATR Stop calisiyor: http://%s:%d" % (a.host, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
