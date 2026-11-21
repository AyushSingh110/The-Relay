#!/bin/sh
# The Relay container entrypoint.
#
# Before `relay serve` starts for the first time, it fetches the three sample papers
# (arxiv.org) and the embedding models (Hugging Face), then builds the sample collection's
# indexes. That needs the network once. The results are kept in /app/data, a volume in
# compose.yaml, so later starts skip this. Set RELAY_BOOTSTRAP=0 to skip it.
# Any other command (the stand-in, relay doctor, a shell) runs unchanged.
set -eu

ready() {
  for preset in small_fast balanced_rerank large_wide; do
    [ -f "data/indexes/$preset/index_manifest.json" ] || return 1
  done
  ls -d data/models/models--* >/dev/null 2>&1
}

if [ "${RELAY_BOOTSTRAP:-1}" = "1" ] && [ "${1:-}" = "relay" ] && [ "${2:-}" = "serve" ] \
    && ! ready; then
  echo "first start: fetching the sample papers and models, building the indexes ..."
  python scripts/ingest_papers.py
  python scripts/build_indexes.py --download-models
  python scripts/build_indexes.py
fi

exec "$@"
