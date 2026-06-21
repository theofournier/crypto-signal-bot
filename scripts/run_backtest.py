"""Replay history through the engine and print the result (BUILD_PLAN Phase 8).

Thin launcher around ``learning/backtest.py`` so the backtest runs the same way as
the other entry points:

    python3 scripts/run_backtest.py                       # first config pair, all history
    python3 scripts/run_backtest.py --pair ETH/USDT       # a specific pair
    python3 scripts/run_backtest.py --days 7              # last week only
    python3 scripts/run_backtest.py --compare             # vs the live dry-run journal
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script (python3 scripts/run_backtest.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from learning.backtest import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
