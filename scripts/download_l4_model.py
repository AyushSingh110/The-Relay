"""Download the L4 entailment model (V2-07) into data/models/nli/ (gitignored).

    python scripts/download_l4_model.py            # the default model file
    python scripts/download_l4_model.py --all      # also the other file variant

Owner-approved model (docs/DECISIONS.md, 2026-11-20): cross-encoder/nli-MiniLM2-L6-H768,
Apache-2.0, pinned to one revision. It runs on onnxruntime + tokenizers (installed with
fastembed); no PyTorch. Contacts huggingface.co only; sends no project data.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from huggingface_hub import hf_hub_download

REPO = "cross-encoder/nli-MiniLM2-L6-H768"
REVISION = "b95119ce93d3e065de6214e38cd4a97b0f2f2c6d"  # pinned (2026-11-20)
DEFAULT_FILE = "onnx/model_quint8_avx2.onnx"  # int8 for AVX2 CPUs (V2-07 spike)
VARIANTS = ("onnx/model.onnx", "onnx/model_quint8_avx2.onnx")
TARGET = Path("data/models/nli/nli-MiniLM2-L6-H768")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="download both ONNX variants")
    parser.add_argument("--target", type=Path, default=TARGET)
    args = parser.parse_args(argv)
    files = ("config.json", "tokenizer.json", *(VARIANTS if args.all else (DEFAULT_FILE,)))
    for name in files:
        path = hf_hub_download(REPO, name, revision=REVISION, local_dir=args.target)
        print(f"{name}: {path}")
    print(f"L4 model ready in {args.target} (revision {REVISION[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
