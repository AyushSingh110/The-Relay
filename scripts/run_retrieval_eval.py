"""P07 step 2: Table 3, every owner question x the three retrieval settings, one model.

  python scripts/run_retrieval_eval.py --model MODEL                       # stand-in
  python scripts/run_retrieval_eval.py --model MODEL --source real         # dry run
  python scripts/run_retrieval_eval.py --model MODEL --source real --live --max-real-calls N

Run only after the owner has selected MODEL from the model comparison. Cache off,
sequential, paced by ProviderGuard, daily safety fraction from settings. Writes
evidence/experiments/<run_id>/ (rows, traces, answers, proposed grades, audit, summary).
"""

from __future__ import annotations

import sys

from relay.experiments.eval_cli import main

if __name__ == "__main__":
    sys.exit(main(None, "retrieval_eval", __doc__ or ""))
