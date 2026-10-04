#!/usr/bin/env python3
"""ATR Stop - telefon icin web uygulamasi (tek dosya, ek paket gerekmez).

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
nav.tabs{position:fixed;left:0;right:0;bottom:0;display:grid;grid-template-columns:1fr 1fr;background:var(--surface);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom,0px);z-index:5}
nav.tabs button{font:inherit;font-size:12px;font-weight:650;letter-spacing:.03em;display:flex;flex-direction:column;align-items:center;gap:3px;padding:9px 6px 8px;border:0;background:transparent;color:var(--muted);cursor:pointer}
nav.tabs button[aria-pressed="true"]{color:var(--accent)}
body{padding-bottom:calc(84px + env(safe-area-inset-bottom,0px))}
</style></head>
<body>
<div class="wrap">
  <header><h1>ATR Stop</h1><span class="eyebrow">2×ATR stop · 2R hedef</span></header>

  <div id="tabScan" class="wrap tabp">
  <section class="scan" aria-label="Tarama">
    <div class="scanhead"><span class="lab">Tarama · 55 günlük kırılım · long</span><button type="button" class="mini" id="rescan">Yenile</button></div>
    <div class="scanmeta" id="scanmeta">Tarama sonucu yükleniyor…</div>
    <div id="siglist"></div>
  </section>
  <div class="note">Sinyal: günlük kapanış, önceki 55 günün en yükseğini yukarı keser. Liste ABD kapanışından sonra kendiliğinden güncellenir; gün içi fiyat hareketleri kapanışa kadar listeye girmez. Bir hisseye dokununca hesaplayıcıda açılır.</div>
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

  <div class="note">Stop = giriş ∓ 2 × ATR(14). Kâr al = giriş ± 4 × ATR(14). ATR, hissenin ABD borsasındaki günlük barlarından hesaplanır; seans açıkken bugünün barını da içerir ve dakikada bir güncellenir. Stop ve kâr al senin yazdığın giriş fiyatına göre yerleşir. Komisyon ve fonlama dahil değil.</div>
  </div>
</div>

<nav class="tabs" aria-label="Sekmeler">
  <button type="button" id="navScan" aria-pressed="true"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><span>Tarama</span></button>
  <button type="button" id="navCalc" aria-pressed="false"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M3 12h18M7 7v10M17 7v10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><span>Hesaplayıcı</span></button>
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

// ---- sekmeler ----
function showTab(name){
  const scan = name!=='calc';
  $('tabScan').hidden = !scan; $('tabCalc').hidden = scan;
  $('navScan').setAttribute('aria-pressed', scan); $('navCalc').setAttribute('aria-pressed', !scan);
  window.scrollTo(0,0);
  try{ localStorage.setItem('atrtab', scan?'scan':'calc'); }catch(e){}
  if(scan) loadScan();
}
$('navScan').addEventListener('click', ()=>showTab('scan'));
$('navCalc').addEventListener('click', ()=>showTab('calc'));
try{ if(localStorage.getItem('atrtab')==='calc') showTab('calc'); }catch(e){}
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
        if p == "/api/scan":
            return self._send(200, json.dumps({"ok": True, "status": SCAN["status"], "progress": SCAN["progress"], "error": SCAN["error"], "result": SCAN["result"]}, ensure_ascii=False), "application/json; charset=utf-8")
        if p == "/api/scan/run":
            if SCAN["status"] != "taraniyor" and time.time() - SCAN["started"] > 600:
                threading.Thread(target=run_scan, daemon=True).start()
                return self._send(200, json.dumps({"ok": True, "started": True}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "started": False}), "application/json; charset=utf-8")
        self._send(404, "bulunamadi", "text/plain; charset=utf-8")

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
    ap.add_argument("--find-key", metavar="KLASOR", help="Tiingo anahtarini bu klasorde ara, dene ve ~/.atr_env'e kaydet")
    a = ap.parse_args()
    load_env_file()
    if a.find_key:
        find_key(a.find_key); load_env_file()
        if not a.test:
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
    if not a.no_scan:
        threading.Thread(target=scan_loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print("ATR Stop calisiyor: http://%s:%d" % (a.host, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
