"""The call model, shared by backtest.py and agent.py so the live agent runs
exactly what was calibrated.

Direction is not forecast: every direction test in market-lab (Chronos-2,
Chronos-Bolt, AutoGluon) came back at coin-flip. What Chronos-2 does forecast
with skill is volatility (beat time-of-day+EWMA on all five futures). So a call
is: gap already on the board, divided by the volatility still to come.

  P(YES) = Phi( gap / sqrt(sigma_1m^2 * r_eff + sigma_proxy^2) )

gap        proxy spot minus strike, $
r_eff      minutes left, minus 2/3: settlement averages the last 60s of BRTI,
           and a 1-minute average of a random walk carries 1/3 min of variance
sigma_1m   $ per sqrt(minute), from the Chronos-2 15m range forecast
sigma_proxy measured error of the proxy against Kalshi's settlement values
"""
import math

RANGE_TO_SIGMA = math.sqrt(8 / math.pi)  # E[range of BM over T] = 1.596 * sigma * sqrt(T)


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def sigma_1m_from_range(range_15m):
    return range_15m / RANGE_TO_SIGMA / math.sqrt(15)


def p_yes(gap, minutes_left, sigma_1m, sigma_proxy):
    r_eff = max(minutes_left - 2 / 3, 1 / 3)
    sd = math.sqrt(sigma_1m ** 2 * r_eff + sigma_proxy ** 2)
    return phi(gap / sd), sd


def kalshi_fee(price):
    """Kalshi taker fee per contract, $: ceil(0.07 * P * (1-P) * 100) cents."""
    return math.ceil(0.07 * price * (1 - price) * 100 - 1e-9) / 100


_pipe = None
def chronos2_range(ranges_15m):
    """Median Chronos-2 forecast of the next 15m high-low range, $."""
    global _pipe
    import torch
    from chronos import BaseChronosPipeline
    if _pipe is None:
        _pipe = BaseChronosPipeline.from_pretrained("amazon/chronos-2", device_map="cpu",
                                                    torch_dtype=torch.float32)
    out = []
    for s in range(0, len(ranges_15m), 64):
        batch = [torch.tensor(x[-512:], dtype=torch.float32) for x in ranges_15m[s:s + 64]]
        q, _ = _pipe.predict_quantiles(batch, prediction_length=1, quantile_levels=[0.5])
        q = q.numpy() if hasattr(q, "numpy") else q
        out.extend(float(q[k].reshape(-1)[0]) for k in range(len(batch)))
    return out
