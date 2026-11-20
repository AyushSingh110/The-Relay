# Offline demo. No API key required.

**This demo runs entirely offline. No API key required.** All answers come from the calibrated
stand-in model: it answers from the retrieved passages, with response timing calibrated from
real Groq measurements. Run `relay serve` with the default `relay.example.toml`, plus one
setting: collections and chat need the metadata store, so set `RELAY_CACHE__MODE=sqlite`.

The expected outputs below were checked in a dry run against the stand-in on 2026-11-20
(`reports/phase-v2-14-load-revalidation.md` §13).

**Stand-in latency is calibrated to real Groq latency.** In the steady load test, the first
sentence arrives at ~462 ms median (p95 608.7 ms). The first question after start-up is slower:
989 ms in the dry run.

## Before recording (not on camera)

```bash
cp relay.example.toml relay.toml   # the default lane is the stand-in, started in-process
export RELAY_CACHE__MODE=sqlite    # Windows PowerShell: $env:RELAY_CACHE__MODE = "sqlite"
```

The `v1-papers` collection (the three v1 papers) is registered on start-up, so nothing needs
uploading. A one-time setup is still required (README Quickstart: `ingest_papers.py` and
`build_indexes.py`).

## Script

| # | Time | Action | What the viewer sees |
| :-- | :-- | :-- | :-- |
| 1 | 0–6 s | Run `relay serve` | The start-up lines, including `lane: standin (standin, model qwen/qwen3.8-27b, source stand-in)` and `collections: v1-papers ready (version 1, reads the v1 small_fast index)`, ending with `serving the API and the web UI on http://127.0.0.1:8000  (Ctrl+C to stop)`. About 4 s on the test machine. |
| 2 | 6–10 s | Open <http://localhost:8000> | The Collections page, with the `v1-papers` card: "3 documents", "Version 1", `small_fast`. |
| 3 | 10–14 s | Select the `v1-papers` name (no new collection: it already exists) | The Chat page for `v1-papers`. |
| 4 | — | (Skipped: no upload. The three papers are already indexed.) | — |
| 5 | — | (Skipped: no ingestion to watch.) | — |
| 6 | 14–30 s | Type `How does multi-head attention reduce computation?` and press Enter | "Request accepted, starting now", then "Finding relevant passages…", then "Writing answer…". Then two verified sentences, each with a chip: `[attention §2]` and `[attention §3.2]`. A collapsed "Sources (4)" list (sections 2, 3.2, 3.2.2, 3.2.3) sits under the answer. |
| 7 | 30–40 s | Select a citation chip | The source panel: "Source P1" or "Source P2", the paper (`attention`, 1706.03762v7), pages and section, and the passage with the matching phrase highlighted. Close it with Escape. |
| 8 | 40–50 s | Type `What is the boiling point of water?` and press Enter | Almost at once: "These passages don't have enough information to answer this." followed by "I can only answer from three papers ("Attention Is All You Need", arXiv:1706.03762v7; Moshi, arXiv:2410.00037v1; H-Net, arXiv:2507.07955v2), and they do not cover this question." No model call is made. |
| 9 | 50–60 s | Point at the footer of the first answer | The time bar (Queue, Retrieval, Model, Verification, labelled as estimated) and a line such as "754 tokens (644 in, 110 out), 1.08 s total". |

## Notes

- **Do not use "What is the capital of France?" as the refusal example on `v1-papers`.** The
  Moshi paper contains a dialogue about 17th-century France, so the stand-in answers it with a
  verified sentence.
- **Repeating a question shows the cache.** The answer replays at once, with the same passages,
  and the footer says "from the cache".
- **Optional upload.** The upload flow from `DEMO_SCRIPT.md` (steps 3–5) also works offline,
  because ingestion uses local embedding models, not the model provider. Ask about something
  the uploaded document discusses.
