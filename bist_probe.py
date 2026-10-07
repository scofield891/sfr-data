#!/usr/bin/env python3
"""BIST icin sunucudan ulasilabilen veri kaynaklarini dener (yalniz okur, hicbir seyi degistirmez)."""
import json, time, urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def al(url, veri=None, baslik=None, timeout=12):
    h = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    if baslik: h.update(baslik)
    req = urllib.request.Request(url, data=(json.dumps(veri).encode("utf-8") if veri is not None else None), headers=h, method="POST" if veri is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def dene(ad, fn, n=700):
    t = time.time()
    try:
        r = fn(); print("[TAMAM] %s (%.1f sn)\n        %s" % (ad, time.time() - t, str(r)[:n]))
    except Exception as e:
        print("[HATA]  %s (%.1f sn)\n        %s" % (ad, time.time() - t, str(e)[:200]))


ist = datetime.now(timezone.utc) + timedelta(hours=3)
print("Istanbul saati:", ist.strftime("%Y-%m-%d %H:%M"))
TV = {"Content-Type": "application/json", "Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}
print("\n--- 1) TradingView tarayici (gun ici acilis/yuksek/dusuk/son, tek istekte cok hisse)")
dene("temel sutunlar", lambda: al("https://scanner.tradingview.com/turkey/scan", {"symbols": {"tickers": ["BIST:THYAO", "BIST:GARAN", "BIST:TRALT", "BIST:ARCLK"], "query": {"types": []}},
                                                                                "columns": ["open", "high", "low", "close", "volume"]}, TV))
dene("ek sutunlar", lambda: al("https://scanner.tradingview.com/turkey/scan", {"symbols": {"tickers": ["BIST:THYAO"], "query": {"types": []}},
                                                                             "columns": ["open", "high", "low", "close", "update_mode", "description", "change", "pricescale"]}, TV))
dene("endeks uyeligi sutunu", lambda: al("https://scanner.tradingview.com/turkey/scan", {"symbols": {"tickers": ["BIST:THYAO", "BIST:ARCLK", "BIST:SOKM"], "query": {"types": []}},
                                                                                       "columns": ["close", "indexes"]}, TV), 1500)

print("\n--- 2) Bigpara (tek hisse, gun ici)")
dene("hisseyuzeysel THYAO", lambda: al("https://bigpara.hurriyet.com.tr/api/v1/borsa/hisseyuzeysel/THYAO"))

print("\n--- 3) Is Yatirim: canli / gun ici uclar")
B = "https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/"
dene("OneEndeks THYAO", lambda: al(B + "Data.aspx/OneEndeks?endeks=THYAO"))
dene("IntradayDelay THYAO.E.BIST", lambda: (lambda s: "uzunluk %d | bas: %s | son: %s" % (len(s), s[:250], s[-250:]))(al(B + "ChartData.aspx/IntradayDelay?period=1&code=THYAO.E.BIST&last=60")))
dene("IndexHistoricalAll THYAO (gunluk)", lambda: (lambda s: "uzunluk %d | bas: %s | son: %s" % (len(s), s[:200], s[-200:]))(
    al(B + "ChartData.aspx/IndexHistoricalAll?period=1440&from=%s000000&to=%s235959&endeks=THYAO.E.BIST" % ((ist - timedelta(days=20)).strftime("%Y%m%d"), ist.strftime("%Y%m%d")))))

print("\n--- 4) Is Yatirim gunluk veri: duzeltilmis mi, bugunun satiri var mi")
for t in ("THYAO", "TRALT", "ASTOR", "SASA", "ARCLK"):
    def f(t=t):
        d2 = ist; d1 = d2 - timedelta(days=420)
        v = json.loads(al(B + "Data.aspx/HisseTekil?hisse=%s&startdate=%s&enddate=%s" % (t, d1.strftime("%d-%m-%Y"), d2.strftime("%d-%m-%Y")))).get("value") or []
        if not v: return "satir yok"
        k = ("HGDG_TARIH", "HGDG_KAPANIS", "HGDG_MIN", "HGDG_MAX", "HG_KAPANIS", "HG_MIN", "HG_MAX")
        oz = lambda x: " ".join("%s=%s" % (a.replace("HGDG_", "D.").replace("HG_", "H."), x.get(a)) for a in k)
        fark = sum(1 for x in v if x.get("HG_KAPANIS") and x.get("HGDG_KAPANIS") and abs(float(x["HG_KAPANIS"]) / float(x["HGDG_KAPANIS"]) - 1) > 0.001)
        return "%d satir | ilk: %s | son iki: %s || %s | D ile H farkli satir: %d" % (len(v), oz(v[0]), oz(v[-2]), oz(v[-1]), fark)
    dene("HisseTekil %s" % t, f, 900)
    time.sleep(0.4)

print("\n--- 5) Yahoo baska bicimde (yalniz merak icin)")
for ad, url, h in (("query2 + sade UA", "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS?range=5d&interval=1d", {"User-Agent": "Mozilla/5.0"}),
                   ("query1 + UA yok", "https://query1.finance.yahoo.com/v8/finance/chart/THYAO.IS?range=5d&interval=1d", {"User-Agent": "curl/8.5.0"})):
    dene(ad, lambda url=url, h=h: al(url, None, h)[:200])
print("\nbitti")
