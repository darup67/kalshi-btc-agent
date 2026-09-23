#!/usr/bin/env python3
"""Kalshi BTC 15-minute cushion caller. READ-ONLY: it never places an order.

Every run (launchd, once a minute) it evaluates the open KXBTC15M window and
logs the evaluation. It shows a call only when the gap between the BRTI proxy
and the strike is large against the volatility still to come — the gate in
gate.json, calibrated by backtest.py. Everything else is NO CALL.

    agent.py               print the card for the open window (no alert)
    agent.py --run         scheduled mode: log, settle, alert once per window
    agent.py --scorecard   live record of every call made so far
"""
import json, math, os, sys, subprocess, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from statistics import NormalDist
import feeds, model

HERE = os.path.dirname(os.path.abspath(__file__))
EVALS = os.path.join(HERE, "data", "evals.jsonl")
STATE = os.path.join(HERE, "data", "state.json")
CONFIG = os.path.join(HERE, "config.json")
ET = ZoneInfo("America/New_York")
MAX_PROXY_SPREAD = 60  # $ between Coinbase and Bitstamp; beyond this one feed is stale or broken


def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def ts(s):
    return int(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def realized_sigma_1m(now):
    """$ per sqrt(minute): std of 1m close changes over the last 60 min, mean of Coinbase + Bitstamp."""
    end = now - now % 60
    cb = feeds.coinbase_1m(end - 3660, end)
    bs = feeds.bitstamp_1m(end - 3660, end)
    ts_ = sorted(set(cb) & set(bs))
    closes = [(cb[t][3] + bs[t][3]) / 2 for t in ts_]
    if len(closes) < 40:
        return None
    d = [b - a for a, b in zip(closes, closes[1:])]
    mu = sum(d) / len(d)
    return math.sqrt(sum((x - mu) ** 2 for x in d) / len(d))


def historical_hit(gate, conf):
    for row in gate.get("hit_table", []):
        if row["conf_from"] <= conf < row["conf_to"] + 1e-9:
            return row
    return None


def evaluate():
    gate = load(os.path.join(HERE, "gate.json"), None)
    if not gate:
        sys.exit("gate.json missing — run backtest.py first")
    now = int(time.time())
    m = feeds.kalshi_open_window()
    if not m:
        return {"status": "no_window", "t": now}
    o_ts, c_ts = ts(m["open_time"]), ts(m["close_time"])
    ev = {"t": now, "ticker": m["ticker"], "open": m["open_time"], "close": m["close_time"],
          "strike": m.get("floor_strike"),
          "yes_bid": float(m.get("yes_bid_dollars") or 0), "yes_ask": float(m.get("yes_ask_dollars") or 0)}
    elapsed, left = (now - o_ts) / 60, (c_ts - now) / 60
    ev["minute"], ev["minutes_left"] = round(elapsed, 2), round(left, 2)

    _, px = feeds.spot_now()
    ev["feeds"] = px
    if "coinbase" not in px or "bitstamp" not in px:
        return {**ev, "status": "no_call", "why": "a calibrated feed (Coinbase or Bitstamp) is down"}
    if abs(px["coinbase"] - px["bitstamp"]) > MAX_PROXY_SPREAD:
        return {**ev, "status": "no_call", "why": f"feeds disagree by ${abs(px['coinbase'] - px['bitstamp']):.0f}"}
    spot = (px["coinbase"] + px["bitstamp"]) / 2
    ev["spot"] = round(spot, 2)
    if ev["strike"] is None:
        return {**ev, "status": "no_call", "why": "strike not published yet"}
    ev["gap"] = round(spot - ev["strike"], 2)

    sig = realized_sigma_1m(now)
    if not sig:
        return {**ev, "status": "no_call", "why": "not enough 1m history for volatility"}
    p, sd = model.p_yes(ev["gap"], left, sig, gate["sigma_proxy"])
    side = "UP" if p >= 0.5 else "DOWN"
    conf = p if side == "UP" else 1 - p
    ev.update({"sigma_1m": round(sig, 2), "sd_to_close": round(sd, 2), "p_yes": round(p, 4),
               "side": side, "conf": round(conf, 4), "z": round(abs(ev["gap"]) / sd, 2),
               "range_15m": round(sig * model.RANGE_TO_SIGMA * math.sqrt(15), 0)})
    ask = ev["yes_ask"] if side == "UP" else 1 - ev["yes_bid"]
    ev["ask"] = round(ask, 4)

    if elapsed < gate["min_minute"]:
        return {**ev, "status": "no_call", "why": f"only {elapsed:.1f} min in; cushion not formed yet (gate starts at minute {gate['min_minute']})"}
    if left < 1:
        return {**ev, "status": "no_call", "why": "under a minute left; settlement averaging has started"}
    min_conf = load(CONFIG, {}).get("min_conf_override") or gate["min_conf"]
    min_z = NormalDist().inv_cdf(min_conf)
    if conf < min_conf:
        need = min_z * sd
        return {**ev, "status": "no_call",
                "why": f"cushion ${abs(ev['gap']):.0f} = {ev['z']:.2f}σ, gate needs {min_z:.2f}σ "
                       f"(BTC above ${ev['strike'] + need:,.0f} or below ${ev['strike'] - need:,.0f})"}
    h = historical_hit(gate, conf)
    ev["hist_hit"] = h["hit"] if h else None
    ev["hist_n"] = h["n"] if h else None
    if h and 0 < ask < 1:
        ev["edge"] = round(h["hit"] - ask - model.kalshi_fee(ask), 4)
    return {**ev, "status": "call"}


def fmt_et(iso):
    return datetime.fromtimestamp(ts(iso), tz=timezone.utc).astimezone(ET).strftime("%-I:%M %p")


def card(ev):
    if ev.get("status") == "no_window":
        return "No open KXBTC15M window."
    win = f"{fmt_et(ev['open'])}–{fmt_et(ev['close'])} ET"
    head = f"BTC 15m · window {win} · minute {ev['minute']:.0f} of 15 · BRTI proxy (Coinbase+Bitstamp)"
    rows = [("Strike", f"${ev['strike']:,.2f}" if ev.get("strike") else "—")]
    if "spot" in ev:
        rows.append(("Current BTC", f"${ev['spot']:,.0f} ({ev['gap']:+,.0f} {'above' if ev['gap'] >= 0 else 'below'} strike)"))
    if "sd_to_close" in ev:
        rows.append(("Cushion", f"{ev['z']:.2f}σ — ${abs(ev['gap']):,.0f} vs ±${ev['sd_to_close']:,.0f} to close"))
    if ev["status"] != "call":
        rows.append(("Call", "NO CALL"))
        rows.append(("Why", ev.get("why", "")))
        if "range_15m" in ev:
            rows.append(("Volatility", f"${ev['range_15m']:,.0f} expected 15m range (60-min realized)"))
    else:
        rows.append(("P(" + ("above" if ev["side"] == "UP" else "below") + " strike)",
                     f"model {ev['conf']:.1%} · calls like this won {ev['hist_hit']:.1%} (n={ev['hist_n']})"))
        rows.append(("Call", f"{ev['side']} — {ev['hist_hit']:.0%}"))
        rows.append(("Volatility", f"${ev['range_15m']:,.0f} expected 15m range (60-min realized)"))
        rows.append(("Kalshi ask", f"{ev['side']} side {ev['ask'] * 100:.0f}¢"))
        e = ev.get("edge")
        if e is not None:
            rows.append(("Edge vs ask", f"{e * 100:+.1f}¢ after fee — " + ("priced in, no edge" if e <= 0 else "positive, unproven (backtest CI spans 0)")))
    w = max(len(r[0]) for r in rows)
    return head + "\n\n" + "\n".join(f"  {k.ljust(w)}  {v}" for k, v in rows)


def notify(ev, cfg):
    title = f"BTC {fmt_et(ev['close'])} {ev['side']} {ev['hist_hit']:.0%} · ${abs(ev['gap']):,.0f} cushion · ask {ev['ask'] * 100:.0f}¢"
    body = f"strike ${ev['strike']:,.2f} spot ${ev['spot']:,.0f} · {ev['z']:.2f}σ · edge {ev.get('edge', 0) * 100:+.1f}¢"
    if cfg.get("banner", True):
        subprocess.run(["osascript", "-e", f'display notification {json.dumps(body)} with title {json.dumps(title)}'],
                       capture_output=True, timeout=10)
    if cfg.get("sound", True):
        subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
    if cfg.get("speak", False):
        subprocess.Popen(["say", f"B T C {ev['side']}, {ev['hist_hit'] * 100:.0f} percent"])
    if cfg.get("email", False):
        subprocess.run(["node", os.path.expanduser("~/flip-notifier/send-email.js"), title, card(ev)],
                       capture_output=True, timeout=60)


def settle(state):
    """Fill in outcomes for calls whose window has settled."""
    pending = state.setdefault("pending", {})
    done = []
    for ticker, rec in list(pending.items()):
        if time.time() < ts(rec["close"]) + 120:
            continue
        try:
            mk = feeds.kalshi_market(ticker)
        except Exception:
            continue
        if mk.get("result") not in ("yes", "no"):
            continue
        won = (mk["result"] == "yes") == (rec["side"] == "UP")
        with open(EVALS, "a") as f:
            f.write(json.dumps({"t": int(time.time()), "ticker": ticker, "status": "settled",
                                "result": mk["result"], "side": rec["side"], "won": won,
                                "conf": rec["conf"], "hist_hit": rec["hist_hit"], "ask": rec["ask"]}) + "\n")
        done.append(ticker)
    for t in done:
        pending.pop(t)


def run():
    cfg = load(CONFIG, {}).get("alerts", {})
    state = load(STATE, {})
    try:
        settle(state)
    except Exception as e:
        print(f"settle: {e}", file=sys.stderr)
    ev = evaluate()
    with open(EVALS, "a") as f:
        f.write(json.dumps(ev) + "\n")
    if ev.get("status") == "call":
        alerted = state.setdefault("alerted", {})
        if alerted.get(ev["ticker"]) != ev["side"]:
            alerted[ev["ticker"]] = ev["side"]
            state.setdefault("pending", {})[ev["ticker"]] = {k: ev[k] for k in ("close", "side", "conf", "hist_hit", "ask")}
            notify(ev, cfg)
            print(card(ev))
        state["alerted"] = {k: v for k, v in alerted.items() if k == ev["ticker"] or k in state["pending"]}
    json.dump(state, open(STATE, "w"), indent=1)


def scorecard():
    rows = [json.loads(l) for l in open(EVALS)] if os.path.exists(EVALS) else []
    s = [r for r in rows if r.get("status") == "settled"]
    evals = [r for r in rows if r.get("status") in ("call", "no_call")]
    print(f"{len(evals)} evaluations logged, {len(s)} calls settled")
    if not s:
        return
    k = sum(r["won"] for r in s)
    brier = sum((r["hist_hit"] - r["won"]) ** 2 for r in s) / len(s)
    pnl = [int(r["won"]) - r["ask"] - model.kalshi_fee(min(max(r["ask"], .01), .99)) for r in s]
    print(f"hit rate {k}/{len(s)} = {k / len(s):.1%} · expected {sum(r['hist_hit'] for r in s) / len(s):.1%}"
          f" · Brier {brier:.4f} · paper EV at ask {sum(pnl) / len(pnl) * 100:+.1f}¢/contract (no orders placed)")


if __name__ == "__main__":
    if "--run" in sys.argv:
        run()
    elif "--scorecard" in sys.argv:
        scorecard()
    else:
        print(card(evaluate()))
