"""Print (and optionally DM) the performance postmortem (BUILD_PLAN Phase 7).

Thin launcher around ``learning/postmortem.py`` so the postmortem runs the same way
as the other entry points:

    python3 scripts/run_postmortem.py                 # judge the whole dry journal
    python3 scripts/run_postmortem.py --days 7        # last week only
    python3 scripts/run_postmortem.py --telegram      # also DM the summary
    python3 scripts/run_postmortem.py --mode live     # the live journal
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script (python3 scripts/run_postmortem.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from learning.postmortem import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
