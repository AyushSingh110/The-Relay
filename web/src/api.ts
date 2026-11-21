// The Relay's HTTP API as the UI uses it (docs/ARCHITECTURE_V2.md §9). Every /v1 request
// carries the Bearer key; errors come back as ApiError with the server's stable code.

import { API_KEY } from "./buildConfig";
import { SSEParser } from "./sse";

export type Preset = "small_fast" | "large_wide" | "balanced_rerank";

export interface Collection {
  id: string;
  name: string;
  version: number;
  retrieval_preset: string;
  rerank: boolean;
  buffered: boolean;
  created_at: string;
  document_count: number;
}

export type IngestState = "queued" | "parsing" | "embedding" | "indexing" | "ready" | "failed";

export interface ExtractionQuality {
  total_pages: number;
  empty_pages: number;
  suspected_scan: boolean;
}

export interface DocumentState {
  id: string;
  collection_id: string;
  source: string;
  title: string | null;
  pages: number | null;
  status: string;
  job: { state: IngestState; attempts: number; error_class: string | null } | null;
  extraction_quality: ExtractionQuality | null;
}

export interface Added {
  id: string;
  status: string;
  deduplicated?: boolean;
  requeued?: boolean;
}

export interface Passage {
  id: string; // "P1"
  chunk_id: string;
  paper_id: string;
  short_name: string | null;
  page_range: string;
  section: string | null;
  text_excerpt: string | null;
  text: string | null;
}

// "rewriting" (V2-08): a follow-up is being turned into a standalone question
export type Stage = "queued" | "rewriting" | "retrieving" | "generating" | "verifying";

export interface StatusData {
  stage: Stage;
  queue_position: number;
  keepalive?: boolean;
  // the second "rewriting" status of a follow-up carries the result (V2-08)
  rewritten_question?: string;
  original_question?: string;
  rewritten?: boolean;
  fallback?: string | null;
}

export interface AskBody {
  question: string;
  collection_id: string;
  conversation_id?: string;
}

export interface Final {
  request_id: string;
  status: string;
  verdict: string;
  error_code: string | null;
  partial: boolean;
  refused: boolean;
  message: string | null;
  tokens_in: number;
  tokens_out: number;
  cache_status: string;
  coalesced: boolean;
  ttft_ms: number | null;
  total_ms: number;
  conversation_id?: string | null;
  rewritten_question?: string | null;
  // V2-09: the lane that answered and the request's full cost (null: a price is unknown)
  lane_name?: string | null;
  fallback?: boolean;
  escalated?: boolean;
  cost_usd?: number | null;
  // per-stage times, when the server sends them (planned in ARCHITECTURE_V2 §11)
  spans?: { queue_ms: number; retrieval_ms: number; model_ms: number; verification_ms: number };
}

export type AskEvent =
  | { event: "accepted"; data: { request_id: string; queue_position: number; expected_wait_ms: number;
      conversation_id?: string | null } }
  | { event: "status"; data: StatusData }
  | { event: "sources"; data: { passages: Passage[] } }
  | { event: "sentence"; data: { text: string; citations: string[] } }
  | { event: "stopped"; data: { reason_class: string; partial: boolean } }
  | { event: "final"; data: Final };

export interface Health {
  status: string;
  cache_mode: string;
  cache_entries: number;
  in_flight: number;
  queue_depth: number;
  queue_active: number;
  queue_capacity: number;
  daily_stop: boolean | null;
  circuit: string | null;
  trace_failures: number;
  trace_sink?: { written: number; failed: number; dropped: number; pending: number };
  quota?: QuotaLane[]; // V2-11: every provider lane's live quota
}

export interface QuotaLane {
  lane: string;
  model: string;
  tpm_available: number | null;
  tpm_limit: number | null;
  requests_last_minute: number;
  rpm_limit: number | null;
  day_requests: number;
  rpd_limit: number | null;
  circuit: string;
  daily_stop: boolean;
}

/** GET /v1/whoami (V2-10): the tenant and key the UI is using (never the key itself). */
export interface WhoAmI {
  tenant_id: string;
  tenant_name: string | null;
  key_id: string | null;
  role: string;
  dev_key: boolean;
  key: string;
  daily_limit: number | null;
}

export interface RelayApi {
  listCollections(): Promise<Collection[]>;
  createCollection(input: { name: string; retrieval_preset: Preset }): Promise<{ id: string }>;
  uploadPdf(collectionId: string, file: File): Promise<Added>;
  addArxiv(collectionId: string, arxivId: string): Promise<Added>;
  getDocument(documentId: string): Promise<DocumentState>;
  health(): Promise<Health>;
  whoami(): Promise<WhoAmI>;
  /** A new conversation in the collection: follow-up questions are read in its context. */
  createConversation(collectionId: string): Promise<{ id: string }>;
  /** Resolves when the stream ends; ``sawFinal`` is false if it ended early. */
  ask(
    body: AskBody,
    onEvent: (event: AskEvent) => void,
    signal: AbortSignal,
  ): Promise<{ sawFinal: boolean }>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const ASK_EVENTS = new Set(["accepted", "status", "sources", "sentence", "stopped", "final"]);

export function createHttpApi(fetchImpl: typeof fetch = (...args) => fetch(...args)): RelayApi {
  const auth = { authorization: `Bearer ${API_KEY}` };

  async function failure(response: Response): Promise<ApiError> {
    let body: { error?: string; message?: string } = {};
    try {
      body = (await response.json()) as typeof body;
    } catch {
      // not JSON (a proxy page, say): the status alone describes it
    }
    return new ApiError(
      response.status,
      body.error ?? `http_${response.status}`,
      body.message ?? `The server answered with HTTP ${response.status}.`,
    );
  }

  async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetchImpl(path, {
      ...init,
      headers: { ...auth, ...(init.headers as Record<string, string> | undefined) },
    });
    if (!response.ok) throw await failure(response);
    return (await response.json()) as T;
  }

  const json = (body: unknown): RequestInit => ({
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

  return {
    async listCollections() {
      return (await call<{ collections: Collection[] }>("/v1/collections")).collections;
    },
    createCollection(input) {
      // V2-10: the server creates it for the API key's own tenant
      return call("/v1/collections", json({ name: input.name,
        retrieval_preset: input.retrieval_preset }));
    },
    uploadPdf(collectionId, file) {
      const form = new FormData();
      form.append("file", file, file.name);
      return call(`/v1/collections/${encodeURIComponent(collectionId)}/documents`, {
        method: "POST",
        body: form,
      });
    },
    addArxiv(collectionId, arxivId) {
      return call(`/v1/collections/${encodeURIComponent(collectionId)}/documents`,
        json({ arxiv_id: arxivId }));
    },
    getDocument(documentId) {
      return call(`/v1/documents/${encodeURIComponent(documentId)}`);
    },
    createConversation(collectionId) {
      return call("/v1/conversations", json({ collection_id: collectionId }));
    },
    whoami() {
      return call("/v1/whoami");
    },
    async health() {
      const response = await fetchImpl("/healthz");
      if (!response.ok) throw await failure(response);
      return (await response.json()) as Health;
    },
    async ask(body, onEvent, signal) {
      const response = await fetchImpl("/v1/ask", {
        method: "POST",
        headers: { ...auth, "content-type": "application/json", accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal,
      });
      if (!response.ok) throw await failure(response);
      if (response.body === null) return { sawFinal: false };
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SSEParser();
      let sawFinal = false;
      try {
        for (;;) {
          const { done, value } = await reader.read();
          const text = done ? decoder.decode() : decoder.decode(value, { stream: true });
          for (const raw of parser.feed(text)) {
            if (!ASK_EVENTS.has(raw.event)) continue; // unknown events are ignored
            const event = { event: raw.event, data: JSON.parse(raw.data) } as AskEvent;
            onEvent(event);
            if (event.event === "final") sawFinal = true;
          }
          if (done || sawFinal) break;
        }
      } finally {
        if (!sawFinal) await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
      return { sawFinal };
    },
  };
}
