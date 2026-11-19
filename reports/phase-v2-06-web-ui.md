# Phase V2-06 Implementation Report — Web UI

- **Date:** 2026-11-19
- **Phase:** V2-06, Web UI (`docs/IMPLEMENTATION_PLAN_V2.md` §V2-06; `docs/ARCHITECTURE_V2.md` D10, §6, §11)
- **Status:** completed, pending owner review. Task 3 of the plan (opening the PDF at the cited page) is not built; see §11.
- **Real Groq calls:** 0. `results.csv` and `evidence/` are unchanged.

## 1. Objective

The goal is that a person can use The Relay without reading the API docs. The phase prompt asked for:

- **The UI itself:** a React + Vite + TypeScript app in `web/`, built to static files and served by the existing FastAPI app (no second runtime).
- **Four pages:** Collections, Upload, Chat and Status.
- **On the Chat page:**
  - verified sentences with citation chips;
  - a source panel with the matched phrase highlighted;
  - a per-answer time bar.
- **A readable state for every non-ok outcome.**
- **Usability:** the UI works at 375 px and with the keyboard alone.
- **Tooling and tests:**
  - a Bearer key read at build time from `VITE_API_KEY`;
  - `relay ui build`;
  - Jest + React Testing Library tests 1–7;
  - the 718 backend tests still passing;
  - ruff clean.

## 2. Implementation Summary

- **Backend** (small changes, each test-first):
  - `GET /`, `/status` and `/collections/...` serve `index.html`, and `/static` serves the assets (`src/relay/app/routes/ui.py`). An unbuilt UI answers 503 with the build command.
  - `relay ui build` runs `npm run build` and swaps `dist/` into `src/relay/app/static/` without losing the previous build on failure.
  - `GET /v1/collections` gains `document_count`, counted in one query.
  - The `sources` event gains `short_name` and the full passage `text`.
  - `tags_in` now returns every tag in a bracket such as `[P1, P2]`.
- **`web/`:**
  - **API client:** `api.ts` sends the Bearer key on every `/v1` call and reads `POST /v1/ask` with `fetch`. `EventSource` cannot POST, so `sse.ts` is a WHATWG-style SSE parser.
  - **Answer state:** `answer.ts` is a pure reducer over the six events. It also holds the time-bar estimate, the phrase highlighter, the chip labels and the stop-reason wording.
  - **Pages:** four pages, an accessible dialog, a source panel and the time bar.
  - **Routing:** a 50-line history router instead of a routing library.
- **Design:** an answer reads like a proof being checked.
  - Verified sentences sit on a teal rule in a serif reading face, with citation chips beside them.
  - Every other state is a tinted band with words: amber for partial, red for lost, slate-blue for refusal and unavailable, orange for a suspected scan.
  - Light and dark themes are built from tokens, and `prefers-reduced-motion` is respected.

## 3. Files Created

| Path | Purpose |
| :-- | :-- |
| `src/relay/app/routes/ui.py` | Serves the built UI on the same origin. |
| `src/relay/app/static/` | Build output (`index.html` plus 2 hashed assets), written by `relay ui build`. |
| `tests/test_v2_06_ui_backend.py` | 17 backend tests. |
| `web/package.json`, `web/package-lock.json` | Dependencies: react 19 and react-dom; dev tools: vite 8, typescript 6, jest 30, Testing Library, jest-axe, eslint 9 with jsx-a11y and react-hooks. |
| `web/vite.config.ts`, `web/tsconfig.json`, `web/jest.config.cjs`, `web/babel.config.cjs`, `web/eslint.config.js` | Build, type, test and lint configuration. |
| `web/.env.example`, `web/index.html` | The build-time key and the page shell. |
| `web/src/api.ts`, `sse.ts`, `answer.ts` | Client, SSE parser, answer state. |
| `web/src/buildConfig.ts`, `router.tsx`, `App.tsx`, `ApiContext.tsx`, `useCollection.ts`, `main.tsx`, `styles.css` | Configuration, routing, shell and theme. |
| `web/src/components/{AnswerView,Dialog,SourcePanel,TimeBar}.tsx` | The answer and its states, the dialog, the source panel and the time bar. |
| `web/src/pages/{Collections,Upload,Chat,Status}Page.tsx` | The four pages. |
| `web/src/test/{setup,buildConfig,style,fakeApi}.ts` | Test setup, and a fake API built on jest mocks. |
| `web/src/__tests__/{sse,api,answer,pages,review}.test.ts(x)` | 40 Jest tests. |
| `reports/phase-v2-06-web-ui.md` | This report. |

## 4. Files Modified

| Path | Change |
| :-- | :-- |
| `src/relay/cli.py` | `relay ui build [--web-dir]` and `_swap_in`: copy to `.new`, move old to `.old`, move `.new` in, roll back on failure. The serve banner says the UI is served. |
| `src/relay/app/compose.py` | `mount_ui(app)` after the API routers. |
| `src/relay/app/routes/ingestion.py` | `document_count` in `GET /v1/collections`. |
| `src/relay/store/repositories.py` | `DocumentRepo.counts_by_collection` (one `GROUP BY` query). |
| `src/relay/api/routes/ask_v1.py` | `sources` gains `short_name` and `text`; `tags_in` uses the verifier's tag pattern; `EXCERPT_CHARS` exported. |
| `src/relay/orchestrator/verifier.py` | `TAG_PATTERN` exported. No behaviour change. |
| `tests/test_ask_v1.py` | A flaky V2-05 assertion fixed (§9). |
| `.gitignore` | `web/node_modules/`, `web/dist/`. |
| `README.md` | Web UI quickstart, including `VITE_API_KEY`. |
| `docs/DECISIONS.md` | The V2-06 prompt's decisions, plus the [PROPOSED] rulings below. |

## 5. Technical Decisions

These follow the prompt and D10:

- React + Vite + TypeScript, with Node at build time only.
- Jest + React Testing Library for the frontend tests.
- `VITE_API_KEY` defaults to `relay-dev-key`. Any Bearer key is accepted until V2-10.
- No CORS: the UI and the API share an origin.

Rulings of mine, marked [PROPOSED] in DECISIONS.md and needing owner review:

1. **The "rerank" preset sends `balanced_rerank`.** The backend has no preset called `rerank`.
2. **The time bar is estimated in the browser** from when each `status` event arrived, and it is labelled as an estimate. `final` has no span breakdown yet (ARCH_V2 §11 plans one), and the UI uses `final.spans` when the server sends it.
   - **Cost if wrong:** events that arrive in the same network chunk share a timestamp, so retrieval often shows 0 ms.
3. **`sources` gains `short_name` and the full passage `text`.** The prompt's chips (for example `[Attention §3.2]`) need the short name, and highlighting within a 240-character excerpt rarely finds the phrase.
   - This is retrieved document text, not model output, so the V2-05 rule that nothing unverified is sent still holds.
   - **Cost:** larger `sources` events (a few KB).
4. **`document_count` counts every document of the collection, failed ones included.**
5. **The Status page derives "metadata store" from `/healthz`.** The presence of `trace_sink` means SQLite with the request log writing; there is no store field. `daily_stop: null` shows as "Not reported".
6. **A small hand-written router**, to avoid adding a routing dependency.
7. **The source panel shows the cited passage text.** The plan's "open the PDF at the cited page" needs an endpoint that serves PDFs, which does not exist.
8. **Safe refusals are shown as "No answer could be verified against these passages."** These are `refused` with verdict `BLOCKED` (the verifier failed). Showing the evidence-refusal text for them would be false.
9. **`src/relay/app/static/` is neither gitignored nor committed.** Whether to commit the build output is the owner's decision.

## 6. Commands Actually Executed

- **Setup (in `web/`):**
  - `npm install react react-dom`
  - `npm install -D …` with `eslint@9 @eslint/js@9`. The first attempt failed with ERESOLVE: eslint 10 conflicts with jsx-a11y.
- **Checks, repeated after each component and again at the end:**
  - `npx jest`, `npx eslint .`, `npx tsc --noEmit -p tsconfig.json`, `npm run build`.
- **Backend:**
  - `python -m pytest -q`
  - `python -m ruff check .`
  - targeted runs of `tests/test_v2_06_ui_backend.py`, `test_ask_v1.py` and `test_ingestion_routes.py`
  - a 40-run loop of the `ask_v1` tests to catch the flake
- **Build and serve:**
  - `relay ui build` (several times; the last run copied 3 files)
  - `relay serve --port 8765` with `RELAY_CACHE__MODE=sqlite`, and `RELAY_PATHS__STORE_PATH`, `UPLOADS_DIR` and `COLLECTIONS_DIR` pointed into the session scratchpad. The repository's `data/` was not written to.
- **Browser:** Playwright scripts against `http://127.0.0.1:8765` (see §8).
- **Final checks:**
  - `git status --short evidence results.csv` → empty
  - `sha256sum results.csv`
  - `diff -rq web/dist src/relay/app/static` → identical

## 7. Test Results

These are the final runs, after the last edit:

| Suite | Result |
| :-- | :-- |
| Backend `python -m pytest -q` | **735 passed** in 74.1 s (718 before this phase, plus 17 new) |
| `python -m ruff check .` | All checks passed |
| Frontend `npx jest` | **40 passed**, 5 suites, with no act() warnings and no console errors |
| `npx eslint .` (jsx-a11y strict, react-hooks, typescript-eslint) | 0 problems |
| `npm run build` (`tsc --noEmit`, then `vite build`) | No TypeScript errors; `dist/` holds `index.html`, 9.5 kB of CSS and 252.5 kB of JS (78.8 kB gzipped) |

**The seven prompt tests**, all in `web/src/__tests__/pages.test.tsx`:

1. A collection card renders from the API (name, "4 documents", "Version 3", preset, link to chat).
2. The upload page shows progress in order. A MutationObserver records the state line, and the test asserts exactly `Queued → Parsing → Embedding → Indexing → Ready`, followed by the extraction quality.
3. A `sentence` event's text is appended to the answer, and the footer shows the token count.
4. A `stopped` event shows "Answer stopped after 1 sentence: …not supported by its cited passage".
5. A chip click opens the source panel with the right passage (paper, pages, section), and the matched phrase is in a `<mark>`. Escape closes the panel.
6. An `accepted` event shows "Request queued — position 3, expected wait 5s", and a keepalive updates the position.
7. Every honest state renders: queued, partial, refusal, unavailable (daily limit, with pointers), unavailable before retrieval (shed), connection lost (retry calls back), ingestion failed (retry re-uploads), and suspected scan.

**Other frontend tests:**

- SSE parsing, including every chunk size and CRLF split across chunks.
- The API client: Bearer key on every `/v1` call, error codes, tenant on create, and the stream's `sawFinal`.
- The reducer and the time-bar arithmetic.
- The highlighter and the chip labels.
- jest-axe with no violations on the four pages, and on an answer with chips, a notice and a time bar.
- Status and the create-collection dialog.
- The review regressions (§10).

**Backend tests** (`tests/test_v2_06_ui_backend.py`, 17):

- The UI is served at `/` and at every app route, and it does not shadow the API.
- Static paths cannot escape the build directory; raw ASGI paths are used, because httpx would normalise `..`.
- An unbuilt UI answers 503 and `/static` answers 404.
- `relay serve` mounts the UI.
- `ui build`: the success swap, a failed npm build, a missing `index.html`, a failed swap (old files kept, no leftovers), and three missing-prerequisite messages; plus parsing of the CLI command.
- Document counts, both in the route and in one query.
- `sources` carries `short_name` and `text`.
- Multi-tag brackets give every citation.

## 8. Actual Output and Generated Artifacts

### Browser test

Playwright drove `relay serve` with the embedded calibrated stand-in, in sqlite mode, on loopback. There were 0 real calls. Viewports were 375 × 812 and 1280 × 800.

| Check | Observed |
| :-- | :-- |
| `GET /` | 200, the Collections page; no console errors or warnings |
| Keyboard order, Collections | Skip link, The Relay, Collections, Status, New collection, `v1-papers`, Add documents |
| New collection by keyboard | Enter opens the dialog with focus in Name. Tab goes to the preset, Create and Cancel, then wraps back to Name. Enter on Create closes the dialog, focus returns to New collection, and the card shows `large_wide`. |
| Upload by keyboard | Tab reaches the file input and Space opens the chooser. A `.txt` file is refused before upload ("Only PDF files can be added"). `rocks.pdf` reaches Ready with "Extracted 3 pages, 0 empty". `scan.pdf` reaches Ready with "10 pages, 9 empty" and the orange "This PDF may be a scan — answers may be incomplete". The repeat upload says "already in the collection". |
| Chat, real stream | Sending, then "Finding relevant passages…", then "Writing answer…". The first verified sentence appeared about 1.1 s after I began typing. Verified sentences arrive with chips; the footer and time bar appear at `final`. |
| Chip → panel → Escape (keyboard) | The panel opens titled "Source P2" with paper, pages and section, and highlights "We call our particular attention "Scaled Dot-Product Attention" (Figure 2". Escape closes it, and focus returns to the chip. |
| Queued (real) | Two requests occupied both running slots, so the UI request waited: "Request queued — position 1, expected wait 12s". |
| Refusal (real) | The evidence gate refused an out-of-scope question: "These passages don't have enough information to answer this." followed by the server's message. |
| Unavailable / shed (real) | The queue was full: "The service is at capacity and did not accept this request. Relevant passages: The request ended before any passages were retrieved." |
| Cache hit (real) | "Served from the cache: no queue, retrieval or model time", 0 tokens. |
| Partial (real) | Retrying a lost answer gave a real stand-in answer: 1 sentence, then "Answer stopped after 1 sentence: the next sentence had no citation." |
| Injected states (Playwright `route.fulfill` with SSE bodies) | Partial (ungrounded); "Daily limit reached. Relevant passages:" with a pointer chip; connection lost with a red "Connection lost — retry?" (its retry then reached the real server); safe refusal; "Ingestion failed: PdfParseError" with Retry upload. |
| 375 px | No horizontal overflow on Collections, Upload, Chat or Status once the fix in §9 was in. |
| Status | Health OK; cache mode `exact`; "SQLite, request log writing"; queue 0/1; circuit closed; daily stop "Not reported"; request log written/failed/dropped/pending; quota "coming in V2-11". |
| Unknown paths | `/v1/nothing` returns a 404 from the API (not `index.html`). `/collections/x/whatever` shows the UI's "Page not found". |

Screenshots are in the session scratchpad (not in the repository): `C:\Users\ASUS\AppData\Local\Temp\claude\66cb0c22af57b6a0\7a9c497f-536e-465a-a0f8-bdc5966e7701\scratchpad\ui-run\screens\` (13 PNGs).

## 9. Errors and Root-Cause Analysis

| Error | Root cause | Fix |
| :-- | :-- | :-- |
| `npm install` ERESOLVE | eslint 10 is newer than `eslint-plugin-jsx-a11y`'s peer range (≤ 9). | Pinned `eslint@9` and `@eslint/js@9`. |
| Jest: `(0, _dom.configure) is not a function` | My `moduleNameMapper` rule for `./config` also caught `@testing-library/dom`'s own internal `./config`. | Renamed our module to `buildConfig` and mapped only that name. |
| `test_a_failed_sentence_stops_the_stream` failed once in about 70 runs | The V2-05 assertion `"999" not in stream.raw` could match timing floats such as `total_ms: 45.999`. I measured roughly 0.2 % per float, with several floats per stream. | The test now checks the failed sentence's own words (`"has 999 layers"`). This is a pre-existing V2-05 test bug, fixed in this phase. |
| Horizontal overflow at 375 px (found in the browser, not by unit tests) | CSS grid items default to `min-width: auto`, so the long `nowrap` time-bar labels widened the column. | `grid-template-columns: minmax(0, 1fr)`, `min-width: 0` and `overflow-wrap: anywhere`. |
| The 6-request browser load test showed no queue | The browser allows 6 connections per host, so the extra requests waited in the browser rather than in the server's queue. | Load was generated from Node instead (test method only; no code change). |
| My own test bugs | A rename stub that also blocked the rollback; a generator expression used as an unpacked list; a fragile progress test. | Fixed in the tests. |
| Process deviation | `sse.ts` was written before its test. The shed-state wording test was written after its fix. | For `sse.ts`, I wrote the test and then mutated the parser (removed the CRLF guard): 1 of 2 tests failed, and passed again once restored. |

## 10. Fixes Applied

**Review 1: the backend diff** (`pr-review-toolkit:code-reviewer`). All five findings were fixed or documented:

- **Important:**
  - ruff E501 → fixed.
  - `ui_build` could half-delete the previous build and then crash → staged swap with rollback; the test `test_a_failed_swap_keeps_the_previous_static_files` failed first and now passes.
  - The traversal test was a no-op → it now sends raw ASGI paths.
- **Minor:**
  - `document_count` loaded every row → `counts_by_collection`, with a test that failed first.
  - Cached answers have no passage text → documented (§11).

**Review 2: the whole UI.** All Critical and Important findings were fixed, and the Minor findings were fixed as well. Each fix has a test in `web/src/__tests__/review.test.tsx` or `pages.test.tsx` that failed (7 failing) before the fix:

- **Important:**
  - A safe refusal showed "passages don't have enough information" alongside "No sentence could be verified" → one accurate notice.
  - `tags_in` kept only the first tag of `[P1, P2]`, so buffered and cached answers lost chips → fixed in the backend, with a test that failed first. This was a V2-05 code bug.
  - Focus fell to `<body>` after Retry, Stop or Retry upload → focus moves to the answer, the question box or the progress card.
- **Minor:**
  - The time bar drew 4 equal segments when everything was 0 ms → the track is left empty.
  - A duplicate "ready" document never showed its extraction quality → it is fetched once.
  - A malformed URL escape crashed the app → it is shown as "Page not found".
  - "Retry" did nothing while another answer was streaming → it is disabled, with a reason.
  - Weak tests were strengthened: progress order, real `status` values (`rate_limited`), axe on a full answer, and a no-op assertion removed.

**Found in the browser:**
- The source panel opened scrolled to the bottom (focus landed on Close at the end) → Close moved into the header, and focus now goes to the panel.
- The daily-limit message sat between the label and the list → moved below the list.
- The "no passages" wording was wrong for a shed request → fixed.
- A duplicated paper ID was shown → fixed.

## 11. Outstanding Issues

- **No PDF view:** the plan's "open the PDF at the cited page" is not built. It needs an endpoint that serves a tenant's PDF; candidates are V2-10 (auth) or V2-11.
- **Cached answers carry no passage text:** chips fall back to the paper ID and page, and the panel says the text was not kept. A fix would look up chunks by `chunk_id` on a cache hit.
- **Time-bar accuracy:** the time bar is a browser estimate until `final` carries spans (ARCH_V2 §11; V2-11).
- **API key in the bundle:** the dev key is compiled into the bundle. That is fine while any key is accepted, but it must change with V2-10.
- **Limited queue visibility:** with `queue_capacity` 1 on the default settings, a user can only ever see "position 1".
- **Upload page history:** the upload page lists only this session's uploads. There is no `GET /v1/collections/{id}/documents` endpoint.

## 12. Acceptance-Criteria Status

| Criterion | Status | Evidence |
| :-- | :-- | :-- |
| The full flow works in a browser against the stand-in with no real API key | **Met** | §8: create collection, upload, ingest to Ready, ask, chips, panel, status; 0 real calls |
| Every non-ok state from the table renders correctly | **Met** | Jest test 7 (8 states), plus the browser (§8): 5 states triggered for real (queued, refusal, shed, partial, cache) and the rest injected |
| Works at 375 px | **Met** | No overflow on all four pages (after the §9 fix); screenshots |
| Keyboard reaches every interactive element | **Met** | §8 keyboard runs: nav, dialog, file input, question, chips, panel, retry; jsx-a11y strict lint at 0 problems; jest-axe clean |
| `npm run build`: no TypeScript errors, no console errors on load | **Met** | §7; Playwright console clean |
| `relay ui build` copies `dist/`; `relay serve` serves `index.html` at `/` | **Met** | `diff -rq` identical; `GET /` 200 |
| All 718 backend tests still pass | **Met** | 735 passed (718 plus 17 new) |
| All 7 frontend tests pass | **Met** | 40 of 40 Jest tests, including tests 1–7 |
| ruff clean | **Met** | All checks passed |
| `results.csv` and `evidence/` unchanged | **Met** | `git status` empty; sha256 starts `8dfb10df` |
| Plan task 3: open the PDF at the cited page | **Not met** | §11 |
| Plan task 6: status page quota headroom | Placeholder, as the prompt specified | "coming in V2-11" |

## 13. Remaining Risks

- **Built output can go stale:** `src/relay/app/static/` is generated, so if it is committed it can fall behind `web/src`. A CI build check would catch that later.
- **Fast-moving toolchain:** the npm versions are recent (vite 8, typescript 6, React 19, jest 30); `package-lock.json` pins them.
- **Browser coverage:** the browser checks ran in Playwright's Chromium only, with no other engines.

## 14. Recommended Next Phase

V2-07 (Verifier v2), after the owner has reviewed this report and the [PROPOSED] rulings in §5. I have not started it.

### Plugins used, and whether they helped

| Plugin | Use | Result |
| :-- | :-- | :-- |
| code-review (`pr-review-toolkit:code-reviewer`) | Once on the backend diff, once over the whole UI | 5 + 7 real findings plus test gaps; all handled (§10) |
| superpowers:verification-before-completion | Fresh final runs before every claim | §7 numbers are from the last run |
| superpowers:test-driven-development | Test first for each module and fix | Two lapses, recorded in §9 |
| frontend-design | Design direction | Proof-checking metaphor, tokens, light and dark |
| Accessibility "plugins" | `eslint-plugin-jsx-a11y` (strict) and `jest-axe`, installed in `web/` | Caught a static drag zone, an anchor with no content, and dialog key handling |
| Playwright (browser) | Real-browser checks at 375 px, keyboard only | Found the 375 px overflow and the panel-scroll problem, which unit tests missed |
