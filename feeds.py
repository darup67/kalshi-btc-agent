"""Price feeds. BRTI itself is paid (CF Benchmarks), so every source here is a
proxy built from its constituent exchanges; backtest.py measures each one's
error against Kalshi's actual settlement values."""
import json, time, urllib.request
from datetime import datetime, timezone

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def get(url, tries=3):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
                return json.load(r)
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(1.5 * (k + 1))


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def coinbase_1m(start, end):
    """{minute_start_ts: (o,h,l,c)} from Coinbase Exchange, 300 candles per call."""
    out = {}
    s = start
    while s < end:
        e = min(s + 300 * 60, end)
        rows = get(f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60&start={iso(s)}&end={iso(e)}")
        for t, lo, hi, op, cl, _v in rows:
            out[int(t)] = (op, hi, lo, cl)
        s = e
        time.sleep(0.25)
    return out


def bitstamp_1m(start, end):
    out = {}
    s = start
    while s < end:
        j = get(f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=60&limit=1000&start={s}")
        rows = j.get("data", {}).get("ohlc", [])
        if not rows:
            break
        for r in rows:
            t = int(r["timestamp"])
            if t < end:
                out[t] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
        last = int(rows[-1]["timestamp"])
        if last + 60 <= s:
            break
        s = last + 60
        time.sleep(0.25)
    return out


def spot_now():
    """Live BRTI proxy: median of Coinbase, Kraken, Bitstamp last trades."""
    px = {}
    try:
        px["coinbase"] = float(get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    except Exception:
        pass
    try:
        px["kraken"] = float(get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")["result"]["XXBTZUSD"]["c"][0])
    except Exception:
        pass
    try:
        px["bitstamp"] = float(get("https://www.bitstamp.net/api/v2/ticker/btcusd/")["last"])
    except Exception:
        pass
    vals = sorted(px.values())
    if not vals:
        return None, px
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return med, px


def kalshi_open_window(series="KXBTC15M"):
    j = get(f"{KALSHI}/markets?series_ticker={series}&status=open&limit=5")
    ms = sorted(j.get("markets", []), key=lambda m: m["close_time"])
    return ms[0] if ms else None


def kalshi_market(ticker):
    return get(f"{KALSHI}/markets/{ticker}")["market"]
