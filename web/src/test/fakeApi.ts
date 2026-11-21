import type { AskEvent, Collection, DocumentState, Final, Passage, RelayApi } from "../api";

export const collection = (over: Partial<Collection> = {}): Collection => ({
  id: "geo", name: "Geology notes", version: 3, retrieval_preset: "large_wide", rerank: false,
  buffered: false, created_at: "2026-11-19T10:00:00Z", document_count: 4, ...over,
});

export const passage = (over: Partial<Passage> = {}): Passage => ({
  id: "P1", chunk_id: "c1", paper_id: "1706.03762v7", short_name: "Attention",
  page_range: "3-4", section: "3.2 Scaled Dot-Product Attention",
  text_excerpt: "We call our particular attention",
  text: "We call our particular attention Scaled Dot-Product Attention. The input consists " +
    "of queries and keys of dimension dk.",
  ...over,
});

export const final = (over: Partial<Final> = {}): Final => ({
  request_id: "r1", status: "ok", verdict: "VERIFIED", error_code: null, partial: false,
  refused: false, message: null, tokens_in: 900, tokens_out: 60, cache_status: "miss",
  coalesced: false, ttft_ms: 600, total_ms: 820, ...over,
});

export const document = (over: Partial<DocumentState> = {},
  state: NonNullable<DocumentState["job"]>["state"] = "queued"): DocumentState => ({
  id: "doc1", collection_id: "geo", source: "upload", title: "rocks", pages: 10,
  status: state === "ready" || state === "failed" ? state : "queued",
  job: { state, attempts: 1, error_class: state === "failed" ? "PdfParseError" : null },
  extraction_quality: state === "ready"
    ? { total_pages: 10, empty_pages: 1, suspected_scan: false } : null,
  ...over,
});

/** A RelayApi whose methods are jest mocks; ``ask`` plays ``script`` (a list of events, or
 * a function that receives the event callback). */
export function fakeApi(over: Partial<RelayApi> = {},
  script: AskEvent[] | ((emit: (e: AskEvent) => void) => Promise<boolean>) = []): RelayApi {
  return {
    listCollections: jest.fn(async () => [collection()]),
    createCollection: jest.fn(async () => ({ id: "new" })),
    uploadPdf: jest.fn(async () => ({ id: "doc1", status: "queued" })),
    addArxiv: jest.fn(async () => ({ id: "doc1", status: "queued" })),
    getDocument: jest.fn(async () => document()),
    createConversation: jest.fn(async () => ({ id: "conv1" })),
    whoami: jest.fn(async () => ({ tenant_id: "dev", tenant_name: "development", key_id: null,
      role: "admin", dev_key: true, key: "development key", daily_limit: null })),
    health: jest.fn(async () => ({
      status: "ok", cache_mode: "sqlite", cache_entries: 2, in_flight: 0, queue_depth: 0,
      queue_active: 0, queue_capacity: 8, daily_stop: false, circuit: "closed",
      trace_failures: 0, trace_sink: { written: 12, failed: 1, dropped: 0, pending: 0 },
      quota: [{ lane: "standin", model: "qwen/qwen3.8-27b", tpm_available: 6200, tpm_limit: 8000,
        requests_last_minute: 2, rpm_limit: null, day_requests: 3, rpd_limit: null,
        circuit: "closed", daily_stop: false }],
    })),
    ask: jest.fn(async (_body, onEvent) => {
      if (typeof script === "function") return { sawFinal: await script(onEvent) };
      for (const event of script) {
        await Promise.resolve();
        onEvent(event);
      }
      return { sawFinal: script.some((e) => e.event === "final") };
    }),
    ...over,
  };
}
