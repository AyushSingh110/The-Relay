# 60-second demo script. Run against relay serve with a real API key.

The expected outputs below were checked in a dry run against the calibrated stand-in on
2026-11-20 (`reports/phase-v2-14-load-revalidation.md` §13). With a real model, the screens and
messages are the same, but the sentence wording and the timings differ.

## Before recording (not on camera)

```bash
cp relay.example.toml relay.toml
# In relay.toml, replace the stand-in [[lanes]] block with the commented Groq lane below it.
export GROQ_API_KEY=your_key_here
export MAX_REAL_CALLS=20          # hard cap on real calls for this session
export RELAY_CACHE__MODE=sqlite   # collections, uploads and chat need the metadata store
```

Have a small text-based PDF ready to drag in, for example `data/papers/1706.03762v7.pdf` (the
Attention paper, 15 pages). Open the browser at phone or laptop width, zoomed so the text is
readable.

## Script

| # | Time | Action | What the viewer sees |
| :-- | :-- | :-- | :-- |
| 1 | 0–6 s | Run `relay serve` | The start-up lines, ending with `serving the API and the web UI on http://127.0.0.1:8000  (Ctrl+C to stop)`. About 4 s on the test machine. |
| 2 | 6–9 s | Open <http://localhost:8000> | The Collections page, with a `v1-papers` card: "3 documents", "Version 1", `small_fast`. |
| 3 | 9–15 s | **New collection** → name `my-papers` → **Create collection** | The dialog closes and a `my-papers` card appears: "0 documents". |
| 4 | 15–17 s | On the `my-papers` card, select **Add documents** | The Upload page: "Add documents to my-papers". |
| 5 | 17–25 s | Drag the PDF onto the drop zone | A progress card for the file. The step line moves from "Step 1 of 5: Queued" to "Step 5 of 5: Ready" in a few seconds. The page checks every 2 s, so short stages (parsing, embedding, indexing) may pass between checks. Then: "Extracted 15 pages, 0 empty." No scan warning: it only appears when most pages are empty. |
| 6 | 25–38 s | Select **Go to chat**. Type `How does multi-head attention work?` and press Enter | Banners in turn: "Request accepted, starting now" (it says "Request queued — position N, expected wait Xs" when other requests hold the service), then "Finding relevant passages…", then "Writing answer…". Verified sentences then appear one at a time, each with a citation chip: the document's short name and section number, such as `[1706_03762v7 §3.2]`. A collapsed "Sources (4)" list sits under the answer. |
| 7 | 38–45 s | Select a citation chip | The source panel opens: "Source P1" (or whichever passage the chip cites), the paper, pages and section, and the passage with the phrase that matches the sentence highlighted. Close it with Escape. |
| 8 | 45–53 s | Type `What is the boiling point of water?` and press Enter | Almost at once: "These passages don't have enough information to answer this." followed by "I can only answer from one document (document …), and it does not cover this question." No model call is made. |
| 9 | 53–60 s | Point at the footer of the first answer | The time bar (Queue, Retrieval, Model, Verification, labelled as estimated) and a line such as "754 tokens (644 in, 110 out), 1.08 s total". |

## Notes

- **Use a question about something the document discusses.** A generic question such as "What
  problem does this paper solve?" is refused up front for a freshly uploaded PDF. The
  collection's scope check finds no subject in it, and refuses before any model call.
- **The answer text comes from the model.** If a sentence fails verification, the answer stops
  there with a yellow notice ("Answer stopped after N sentences: …"). That is the product
  working as designed, and worth showing if it happens.
- **Each answer uses one real model call.** A repeated question is served from the cache, which
  makes no call.
