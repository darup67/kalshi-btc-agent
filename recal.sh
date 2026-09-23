#!/bin/bash
# Weekly: re-run the backtest on everything market-lab has recorded since,
# rewrite gate.json by the same pre-registered rule, and commit the call log.
set -euo pipefail
cd "$(dirname "$0")"
~/.venvs/market-ml/bin/python backtest.py > /dev/null
~/.venvs/market-ml/bin/python agent.py --scorecard > results/scorecard.txt
git add gate.json results data/evals.jsonl
git diff --cached --quiet || git commit -q -m "Weekly recalibration $(date +%F)"
git push -q origin HEAD 2>&1 || echo "push failed" >&2
