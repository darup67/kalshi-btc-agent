#!/usr/bin/env python3
"""Calibrate the cushion gate against real Kalshi settlements.

Uses every KXBTC15M window market-lab has recorded (outcome + strike + the
15-point YES price path), and 1-minute candles from two BRTI constituents
(Coinbase, Bitstamp). For each window and each checkpoint minute it asks:
given the gap on the board and the forecast volatility still to come, how
sure should we be, and was that right?

Three things it decides, all written to gate.json for agent.py:
  1. which proxy tracks Kalshi's settlement value best, and its error ($)
  2. whether Chronos-2 or trailing realized volatility calibrates better
  3. the minimum confidence (= minimum cushion in sigma) where calls held up,
     chosen on the first half of the data and reported on the second half

And one it only reports: whether a call beats the price Kalshi was already
showing. A 97% call on a contract trading at 98c is not an edge.

    ~/.venvs/market-ml/bin/python backtest.py
"""
import json, math, os, sys, time
from datetime import datetime, timezone
import numpy as np
import feeds, model

LAB = os.path.expanduser("~/market-lab/data/KXBTC15M.jsonl")
HERE = os.path.dirname(os.path.abspath(__file__))
CHECKPOINTS = [3, 5, 7, 9, 11, 13]
P_GRID = [0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.98, 0.99]
TARGET_HIT, MIN_N = 0.90, 30


def ts(s):
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"),) * 2
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def load_windows():
    seen = {}
    for line in open(LAB):
        r = json.loads(line)
        seen[r["ticker"]] = r
    ws = sorted(seen.values(), key=lambda r: r["open_time"])
    for w in ws:
        w["o_ts"], w["c_ts"] = ts(w["open_time"]), ts(w["close_time"])
    by_open = {w["o_ts"]: w for w in ws}
    for w in ws:
        nxt = by_open.get(w["c_ts"])
        w["settle_value"] = nxt["target"] if nxt else None
    return ws


def candles(ws):
    start = ws[0]["o_ts"] - 6 * 86400
    end = ws[-1]["c_ts"] + 60
    cache = os.path.join(HERE, "data", f"candles-{start}-{end}.json")
    if os.path.exists(cache):
        raw = json.load(open(cache))
        return {k: {int(t): v for t, v in d.items()} for k, d in raw.items()}
    print(f"fetching 1m candles {feeds.iso(start)} → {feeds.iso(end)} …", file=sys.stderr)
    c = {"coinbase": feeds.coinbase_1m(start, end), "bitstamp": feeds.bitstamp_1m(start, end)}
    both = set(c["coinbase"]) & set(c["bitstamp"])
    c["mean2"] = {t: tuple((a + b) / 2 for a, b in zip(c["coinbase"][t], c["bitstamp"][t])) for t in both}
    json.dump(c, open(cache, "w"))
    return c


def market_candles(ws):
    """Clock-aligned 1m YES bid/ask per window from Kalshi's candlestick endpoint.
    (The market-lab path is spaced by trade count, not time, so it cannot say
    what the market showed at minute m.)"""
    cache = os.path.join(HERE, "data", "kalshi-candles.json")
    have = json.load(open(cache)) if os.path.exists(cache) else {}
    todo = [w for w in ws if w["ticker"] not in have and w["settle_value"] is not None]
    for n, w in enumerate(todo):
        try:
            j = feeds.get(f"{feeds.KALSHI}/series/KXBTC15M/markets/{w['ticker']}/candlesticks"
                          f"?start_ts={w['o_ts']}&end_ts={w['c_ts']}&period_interval=1")
        except Exception:
            continue
        bars = {}
        for c in j.get("candlesticks", []):
            b, a = c.get("yes_bid", {}).get("close_dollars"), c.get("yes_ask", {}).get("close_dollars")
            if b is not None and a is not None:
                bars[str(c["end_period_ts"])] = (float(b), float(a))
        have[w["ticker"]] = bars
        if n % 100 == 99:
            json.dump(have, open(cache, "w"))
            print(f"  kalshi candles {n + 1}/{len(todo)}", file=sys.stderr)
        time.sleep(0.12)
    json.dump(have, open(cache, "w"))
    return have


def avg60(cd, t):
    """Estimate of the 60s average over [t, t+60) from that minute's candle."""
    k = cd.get(t)
    return sum(k) / 4 if k else None


def main():
    ws = load_windows()
    C = candles(ws)
    lines = [f"# Cushion-gate backtest — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             "", f"{len(ws)} recorded KXBTC15M windows, {ws[0]['open_time']} → {ws[-1]['close_time']}", ""]

    # 1. proxy error against Kalshi's own settlement values
    lines += ["## 1. Proxy vs Kalshi settlement value (60s BRTI average)", "",
              "| proxy | n | median abs $ | p90 abs $ | RMSE $ |", "|---|---|---|---|---|"]
    perr = {}
    for name, cd in C.items():
        e = [avg60(cd, w["c_ts"] - 60) - w["settle_value"] for w in ws
             if w["settle_value"] and avg60(cd, w["c_ts"] - 60) is not None]
        e = np.array(e)
        perr[name] = float(np.sqrt(np.mean(e ** 2)))
        lines.append(f"| {name} | {len(e)} | {np.median(np.abs(e)):.2f} | {np.percentile(np.abs(e), 90):.2f} | {perr[name]:.2f} |")
    proxy = min(perr, key=perr.get)
    cd, sig_p = C[proxy], perr[proxy]
    lines += ["", f"Best proxy: **{proxy}**, RMSE ${sig_p:.2f}. It enters every call as extra variance.", ""]

    # 15m ranges on the proxy, for the volatility forecast
    buckets = {}
    for t, (o, h, l, c) in cd.items():
        b = t - t % 900
        hi, lo = buckets.get(b, (-1e18, 1e18))
        buckets[b] = (max(hi, h), min(lo, l))
    bts = sorted(buckets)
    rng = {b: buckets[b][0] - buckets[b][1] for b in bts}

    MK = market_candles(ws)
    usable, ctxs = [], []
    for w in ws:
        past = [rng[b] for b in bts if b < w["o_ts"]][-512:]
        if len(past) < 96 or w["settle_value"] is None:
            continue
        closes = [cd[t][3] for t in range(w["o_ts"] - 3600, w["o_ts"], 60) if t in cd]
        if len(closes) < 50:
            continue
        w["sig_real"] = float(np.std(np.diff(closes)))
        usable.append(w)
        ctxs.append(past)
    print(f"Chronos-2 on {len(ctxs)} windows …", file=sys.stderr)
    t0 = time.time()
    for w, r in zip(usable, model.chronos2_range(ctxs)):
        w["sig_chr"] = model.sigma_1m_from_range(r)
    print(f"  {time.time() - t0:.0f}s", file=sys.stderr)

    # 2. build every (window, checkpoint) observation
    obs = []
    for i, w in enumerate(usable):
        for m in CHECKPOINTS:
            k = cd.get(w["o_ts"] + (m - 1) * 60)
            if not k:
                continue
            q = MK.get(w["ticker"], {}).get(str(w["o_ts"] + m * 60))
            if not q or not (0 < q[0] <= q[1] < 1):
                continue
            gap = k[3] - w["target"]
            o = {"i": i, "m": m, "gap": gap, "y": int(w["settled_yes"]),
                 "mkt": (q[0] + q[1]) / 2, "ask_yes": q[1], "ask_no": 1 - q[0]}
            for tag in ("real", "chr"):
                o["p_" + tag], o["sd_" + tag] = model.p_yes(gap, 15 - m, w["sig_" + tag], sig_p)
            obs.append(o)
    half = len(usable) // 2
    A = [o for o in obs if o["i"] < half]
    B = [o for o in obs if o["i"] >= half]

    def brier(os_, key):
        return float(np.mean([(o[key] - o["y"]) ** 2 for o in os_]))

    lines += ["## 2. Calibration (Brier, lower is better; all checkpoints)", "",
              "| source | fit half | test half |", "|---|---|---|"]
    for key, lab in (("p_real", "gap / realized vol"), ("p_chr", "gap / Chronos-2 vol"), ("mkt", "Kalshi mid at the same minute")):
        lines.append(f"| {lab} | {brier(A, key):.4f} | {brier(B, key):.4f} |")
    vol = "chr" if brier(A, "p_chr") <= brier(A, "p_real") else "real"
    lines += ["", f"Volatility source chosen on the fit half: **{'Chronos-2' if vol == 'chr' else 'realized'}**.", ""]

    # 3. gate: smallest confidence whose calls held >= TARGET_HIT (Wilson lower bound) on the fit half
    def calls(os_, pmin):
        out = []
        for o in os_:
            p = o["p_" + vol]
            side_yes = p >= 0.5
            conf = p if side_yes else 1 - p
            if conf >= pmin:
                px = o["ask_yes"] if side_yes else o["ask_no"]
                win = o["y"] == int(side_yes)
                out.append((win, conf, px, o))
        return out

    lines += ["## 3. Gate sweep", "",
              "Call = the side the model favours, only when its confidence clears the bar. "
              "`paid` = Kalshi ask for that side at that minute (clock-aligned candles, spread included). "
              "`EV/contract` = win − paid − fee; positive means the call beat the market.", "",
              "| min conf | ≈ cushion σ | fit n | fit hit [95% CI] | test n | test hit [95% CI] | test avg conf | test avg paid | test EV/contract |",
              "|---|---|---|---|---|---|---|---|---|"]
    chosen = None
    for pmin in P_GRID:
        a, b = calls(A, pmin), calls(B, pmin)
        ka, kb = sum(x[0] for x in a), sum(x[0] for x in b)
        la, ha = wilson(ka, len(a))
        lb, hb = wilson(kb, len(b))
        ev = np.mean([x[0] - x[2] - model.kalshi_fee(min(max(x[2], .01), .99)) for x in b]) if b else float("nan")
        z = abs(float(np.sqrt(2) * __import__("scipy").special.erfinv(2 * pmin - 1))) if pmin < 1 else float("inf")
        lines.append(f"| {pmin:.2f} | {z:.2f} | {len(a)} | {ka / max(len(a), 1):.1%} [{la:.0%}–{ha:.0%}] | {len(b)} | "
                     f"{kb / max(len(b), 1):.1%} [{lb:.0%}–{hb:.0%}] | "
                     f"{np.mean([x[1] for x in b]) if b else float('nan'):.1%} | {np.mean([x[2] for x in b]) if b else float('nan'):.2f} | {ev:+.3f} |")
        if chosen is None and len(a) >= MIN_N and la >= TARGET_HIT:
            chosen = pmin
    if chosen is None:
        chosen = P_GRID[-1]
    zc = float(np.sqrt(2) * __import__("scipy").special.erfinv(2 * chosen - 1))

    # per-checkpoint view at the chosen gate
    lines += ["", f"## 4. At the chosen gate ({chosen:.2f}), by minute into the window (test half)", "",
              "EV 95% CI is a bootstrap over calls; minute-by-minute cells were not pre-registered, "
              "so a positive cell is a hypothesis for the live scorecard, not a finding.", "",
              "| minute | calls | hit | avg ask paid | EV/contract [95% CI] | median cushion $ |", "|---|---|---|---|---|---|"]
    rng_ = np.random.default_rng(7)
    for m in CHECKPOINTS:
        b = [x for x in calls(B, chosen) if x[3]["m"] == m]
        if not b:
            lines.append(f"| {m} | 0 | — | — | — | — |")
            continue
        pnl = np.array([x[0] - x[2] - model.kalshi_fee(min(max(x[2], .01), .99)) for x in b])
        bs = [rng_.choice(pnl, len(pnl)).mean() for _ in range(2000)]
        lines.append(f"| {m} | {len(b)} | {np.mean([x[0] for x in b]):.1%} | {np.mean([x[2] for x in b]):.2f} | "
                     f"{pnl.mean():+.3f} [{np.percentile(bs, 2.5):+.3f}, {np.percentile(bs, 97.5):+.3f}] | "
                     f"{np.median([abs(x[3]['gap']) for x in b]):.0f} |")

    # what a stated confidence has actually been worth, all data (shown on the card)
    hit_table = []
    edges = [0.80, 0.85, 0.90, 0.95, 0.98, 1.0001]
    allc = calls(A + B, 0.80)
    for lo, hi in zip(edges, edges[1:]):
        sel = [x for x in allc if lo <= x[1] < hi]
        if sel:
            k = sum(x[0] for x in sel)
            l, h = wilson(k, len(sel))
            hit_table.append({"conf_from": lo, "conf_to": min(hi, 1.0), "n": len(sel),
                              "hit": round(k / len(sel), 4), "ci": [round(l, 3), round(h, 3)]})
    gate = {"proxy": proxy, "sigma_proxy": round(sig_p, 2), "vol_source": vol,
            "min_conf": chosen, "min_z": round(zc, 2), "min_minute": 3, "hit_table": hit_table,
            "calibrated_on": f"{len(usable)} windows, {usable[0]['open_time']} → {usable[-1]['close_time']}",
            "calibrated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    json.dump(gate, open(os.path.join(HERE, "gate.json"), "w"), indent=2)
    lines += ["", "## gate.json", "", "```json", json.dumps(gate, indent=2), "```"]
    out = os.path.join(HERE, "results", f"backtest-{datetime.now().strftime('%Y-%m-%d')}.md")
    open(out, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
