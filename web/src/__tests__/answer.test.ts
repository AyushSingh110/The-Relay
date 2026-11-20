import type { AskEvent, Final, Passage } from "../api";
import { chipLabel, highlight, initialAnswer, reasonText, reduce, segments, type Answer } from "../answer";

const passage = (over: Partial<Passage> = {}): Passage => ({
  id: "P1", chunk_id: "c1", paper_id: "1706.03762v7", short_name: "Attention",
  page_range: "3-4", section: "3.2 Scaled Dot-Product Attention",
  text_excerpt: "We call our particular attention", text: "We call our particular attention " +
    "Scaled Dot-Product Attention. The input consists of queries and keys of dimension dk.",
  ...over,
});

const final = (over: Partial<Final> = {}): Final => ({
  request_id: "r", status: "ok", verdict: "VERIFIED", error_code: null, partial: false,
  refused: false, message: null, tokens_in: 900, tokens_out: 60, cache_status: "miss",
  coalesced: false, ttft_ms: 600, total_ms: 820, ...over,
});

function play(events: [number, AskEvent][]): Answer {
  return events.reduce(
    (state, [at, event]) => reduce(state, { type: "event", event, at }),
    initialAnswer("What is attention?", 0),
  );
}

test("events build the answer: queue position, stage, sources, sentences, final", () => {
  let a = play([[5, { event: "accepted", data: { request_id: "r", queue_position: 3, expected_wait_ms: 4000 } }]]);
  expect([a.queuePosition, a.expectedWaitMs, a.stage]).toEqual([3, 4000, "queued"]);
  a = reduce(a, { type: "event", at: 9, event: { event: "status", data: { stage: "queued", queue_position: 1, keepalive: true } } });
  expect(a.queuePosition).toBe(1); // the keepalive updates the position
  a = reduce(a, { type: "event", at: 20, event: { event: "status", data: { stage: "retrieving", queue_position: 0 } } });
  a = reduce(a, { type: "event", at: 30, event: { event: "sources", data: { passages: [passage()] } } });
  a = reduce(a, { type: "event", at: 40, event: { event: "status", data: { stage: "generating", queue_position: 0 } } });
  a = reduce(a, { type: "event", at: 70, event: { event: "sentence", data: { text: "A [P1].", citations: ["P1"] } } });
  a = reduce(a, { type: "event", at: 90, event: { event: "final", data: final() } });
  expect(a.sources).toHaveLength(1);
  expect(a.sentences.map((s) => s.text)).toEqual(["A [P1]."]);
  expect([a.phase, a.stage, a.final?.status]).toEqual(["done", null, "ok"]);
});

test("a stream that ends without a final is lost; one with a final is not", () => {
  const started = play([[1, { event: "accepted", data: { request_id: "r", queue_position: 0, expected_wait_ms: 0 } }]]);
  expect(reduce(started, { type: "ended", at: 5 }).phase).toBe("lost");
  const done = reduce(started, { type: "event", at: 4, event: { event: "final", data: final() } });
  expect(reduce(done, { type: "ended", at: 5 }).phase).toBe("done");
});

test("the time bar is estimated from when each stage began", () => {
  const a = play([
    [0, { event: "accepted", data: { request_id: "r", queue_position: 1, expected_wait_ms: 900 } }],
    [100, { event: "status", data: { stage: "retrieving", queue_position: 0 } }],
    [150, { event: "status", data: { stage: "generating", queue_position: 0 } }],
    [650, { event: "status", data: { stage: "verifying", queue_position: 0 } }],
    [700, { event: "final", data: final() }],
  ]);
  expect(segments(a)).toEqual({
    estimated: true,
    parts: [
      { label: "Queue", ms: 100 }, { label: "Retrieval", ms: 50 },
      { label: "Model", ms: 500 }, { label: "Verification", ms: 50 },
    ],
  });
});

test("server-sent spans are used as they are", () => {
  const spans = { queue_ms: 1, retrieval_ms: 2, model_ms: 3, verification_ms: 4 };
  const a = play([[0, { event: "final", data: final({ spans }) }]]);
  expect(segments(a)).toEqual({
    estimated: false,
    parts: [
      { label: "Queue", ms: 1 }, { label: "Retrieval", ms: 2 },
      { label: "Model", ms: 3 }, { label: "Verification", ms: 4 },
    ],
  });
});

test("the matched phrase is found in the passage, ignoring tags, case and punctuation", () => {
  const parts = highlight(passage().text!, "It is called scaled dot-product attention [P1].");
  expect(parts.filter((p) => p.match).map((p) => p.text)).toEqual(["Scaled Dot-Product Attention"]);
  expect(parts.map((p) => p.text).join("")).toBe(passage().text);
  expect(highlight("nothing shared here", "Completely different words [P1].")).toEqual([
    { text: "nothing shared here", match: false },
  ]);
});

test("a chip names the paper and section, or the pages", () => {
  expect(chipLabel(passage())).toBe("Attention §3.2");
  expect(chipLabel(passage({ section: "Abstract" }))).toBe("Attention p. 3-4");
  expect(chipLabel(passage({ short_name: null, section: null }))).toBe("1706.03762v7 p. 3-4");
});

test("an L3 stop reads as words, not a code (V2-07)", () => {
  expect(reasonText("refusal_as_claim")).toBe(
    "the next sentence said the passages do not contain the answer");
});
