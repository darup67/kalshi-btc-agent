# kalshi-btc-agent

Calls on Kalshi's BTC 15-minute contracts (`KXBTC15M`), **shown only when the
cushion is adequate**. It is read-only: it never places an order, and nothing
in this repo holds Kalshi or Robinhood credentials.

```
BTC 15m · window 3:15 PM–3:30 PM ET · minute 9 of 15 · BRTI proxy (Coinbase+Bitstamp)

  Strike               $84,361.36
  Current BTC          $84,540 (+179 above strike)
  Cushion              2.61σ — $179 vs ±$69 to close
  P(above strike)      model 99.5% · calls like this won 97.5% (n=1571)
  Call                 UP — 98%
  Volatility           $150 expected 15m range (60-min realized)
  Kalshi ask           UP side 97¢
  Edge vs ask          -0.1¢ after fee — priced in, no edge
```
(Illustrative. Anything under the gate prints `NO CALL` and the reason.)

## Commands

```
~/.venvs/market-ml/bin/python agent.py              # card for the open window
~/.venvs/market-ml/bin/python agent.py --scorecard  # live record of every call
~/.venvs/market-ml/bin/python backtest.py           # recalibrate gate.json
```

| launchd label | schedule | does |
|---|---|---|
| `com.dhruv.kalshibtc` | every minute | evaluate, log to `data/evals.jsonl`, settle old calls, alert once per window when the gate passes |
| `com.dhruv.kalshibtc.recal` | Sundays 6:00 | re-run the backtest, rewrite `gate.json`, commit and push |

Alert channels are in `config.json` (banner title + sound by default; this Mac
hides notification bodies, so the call rides in the title).

## How a call is made

Direction is **not** forecast. Every direction test in `market-lab` (Chronos-2,
Chronos-Bolt, AutoGluon) came back at coin-flip. What matters is the gap that is
already on the board, measured against the volatility still to come:

```
P(YES) = Φ( gap / sqrt(σ_1m² · r_eff + σ_proxy²) )
```

- **gap:** spot minus strike. Spot is the mean of Coinbase and Bitstamp, two
  BRTI constituents. That mean tracked Kalshi's settlement value best
  (RMSE $8.77; see `results/`). Kraken is fetched live as a third check.
- **r_eff:** minutes left minus ⅔. Kalshi settles on the *60-second average* of
  BRTI before the close, and the strike is the same average before the open.
- **σ_1m:** 60-minute realized volatility. The backtest compared this with
  Chronos-2's range forecast; they tied (Brier 0.1636 vs 0.1643 on the fit
  half), so the simpler one runs live. Chronos-2 stays in `backtest.py` as the
  challenger and is re-tested every week.
- **σ_proxy:** the proxy's measured error, carried as extra variance.

The gate: show a call only when the model's confidence is ≥ `min_conf`, which is
equivalent to a cushion of ≥ `min_z` σ. The threshold is set by a rule fixed
before the data was seen: *the lowest confidence where the fit half's hit rate
has a 95% Wilson lower bound ≥ 90%*. It is reported on the untouched second half.
The card shows the **historical hit rate** for that confidence band, not the raw
model number, because the raw number runs hot (stated 90–95% → won 88.9%).

## What the backtest found (1,720 windows, Sep 5–23 2026)

| gate (min conf) | cushion | test-half calls | test hit | avg ask paid | EV/contract after fee |
|---|---|---|---|---|---|
| 0.85 | 1.04σ | 1,934 | 93.5% | 93¢ | −0.8¢ |
| 0.95 | 1.64σ | 1,113 | 97.6% | 96¢ | +0.2¢ |
| 0.99 | 2.33σ | 660 | 98.3% | 98¢ | −0.5¢ |

**The calls are accurate, and the market already knows.** Kalshi's own mid at
the same minute is as well calibrated as the model (Brier 0.1495 vs 0.1522).
Buying the called side at the ask nets roughly zero after fees at every gate.
The one cell that looked positive (minute 3: +6.3¢, n=77) has a CI of
[−0.5¢, +12.4¢] and was not pre-registered. It is a hypothesis for the live
scorecard, not a finding. Minute 13 is significantly *negative* (−2.2¢
[−3.7, −0.7]): late calls overpay.

Use this as a filter that says when a window is effectively decided. It does
not show an edge.

## Review of the "24/7 autonomous trading agent" prompt

**Adopted:**
- **The code owns every threshold.** The gate, sizing inputs and data-quality
  checks are deterministic Python. No model output is trusted to self-limit.
- **A compact, causal state.** Each evaluation uses only data timestamped before
  the call (proxy, strike, 60-min realized vol). The backtest aligns everything
  to the minute of the call.
- **Calibration measured by Brier score, plus a scheduled review.** The weekly
  `recal.sh` and `--scorecard` do this.
- **Deterministic refusals:** feed disagreement over $60, a missing feed, no
  strike yet, fewer than 3 minutes elapsed, or under 1 minute left all mean NO CALL.
- **"What could I be wrong about?"**, below.

**Rejected, and why:**
- **Autonomous execution.** Standing rule: never auto-fire trades, and propose
  plus wait for an explicit go every time. This agent only calls.
- **AgenKit (agenkit.xyz) and "Jev" (console.typesafe.ai).** The prompt reads as
  an ad for both. Neither is needed: an 81 ms decision loop buys nothing on a
  15-minute contract. Routing live state and an API key through an unverified
  third party is a real risk for no gain.
- **Gating on model confidence > 0.80.** The measured problem here *is* model
  confidence: this session's ad-hoc Chronos runs said 85–93% on coin-flips.
  The gate uses calibrated hit rates instead.
- **Quarter-Kelly sizing on the model's probability.** Kelly on an overconfident
  probability oversizes. With no measured edge, Kelly says size zero.
- **Rewriting the schema every night.** Tuning to each day's results is
  overfitting. Here the *rule* is fixed; only its inputs update, weekly, with
  the diff committed.
- **Researching 5–10 "asymmetric" alts.** That is out of scope for a BTC
  15-minute caller, and it's the speculation the prompt's own final check warns
  about.

## What could I be wrong about?

1. **Proxy ≠ BRTI.** BRTI weights more venues than Coinbase and Bitstamp. The
   $8.77 RMSE is measured and priced in, but a venue outage or dislocation
   could make it much worse for a few minutes. The $60 disagreement check is
   the only guard.
2. **Three weeks is one regime.** Sep 5–23 includes a selloff (Sep 14) but no
   crash, no halt and no weekend-gap stress. Tail volatility under-estimated by
   60-minute realized vol is exactly when a "98%" call fails.
3. **The hit table is in-sample for the card.** The gate was chosen on the fit
   half, but the displayed hit rates use all data. Expect live results slightly
   below them; the scorecard will say.
4. **Fills are not the ask.** EV assumes you buy at the last-minute ask close.
   Real size walks the book, so actual EV is worse than shown.
5. **Settled outcomes come from Kalshi's API**, which keeps only ~2 days of
   settled markets. If the agent is down for longer, those calls stay pending
   forever. They are logged, but never scored.
