"""P07 step 1: model comparison on a few owner questions (default: 6 x shortlist).

  python scripts/run_model_comparison.py                                   # stand-in
  python scripts/run_model_comparison.py --source real                     # dry run
  python scripts/run_model_comparison.py --source real --live --max-real-calls 24

Cache off, sequential, paced per model by ProviderGuard, daily safety fraction from
settings. Writes evidence/experiments/<run_id>/ (rows, traces, answers, proposed grades,
audit, summary). The owner then selects the model; this script does not.
"""

from __future__ import annotations

import sys

from relay.experiments.eval_cli import main

if __name__ == "__main__":
    sys.exit(main(None, "model_comparison", __doc__ or ""))
