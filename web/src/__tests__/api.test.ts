import { ApiError, createHttpApi, type AskEvent } from "../api";

interface Call {
  url: string;
  init: RequestInit;
}

function reader(chunks: string[]) {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    read: async () =>
      i < chunks.length
        ? { done: false, value: encoder.encode(chunks[i++]) }
        : { done: true, value: undefined },
    releaseLock: () => undefined,
    cancel: async () => undefined,
  };
}

function fakeFetch(reply: { status: number; json?: unknown; chunks?: string[] }, calls: Call[]) {
  return (async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    return {
      ok: reply.status >= 200 && reply.status < 300,
      status: reply.status,
      json: async () => reply.json,
      body: { getReader: () => reader(reply.chunks ?? []) },
    };
  }) as unknown as typeof fetch;
}

const header = (call: Call, name: string) => new Headers(call.init.headers).get(name);

test("every /v1 request carries the Bearer key", async () => {
  const calls: Call[] = [];
  const api = createHttpApi(fakeFetch({ status: 200, json: { collections: [] } }, calls));
  await api.listCollections();
  await api.getDocument("doc-1");
  await api.addArxiv("c1", "2410.00037v1");
  expect(calls.map((c) => c.url)).toEqual([
    "/v1/collections",
    "/v1/documents/doc-1",
    "/v1/collections/c1/documents",
  ]);
  for (const call of calls) expect(header(call, "authorization")).toBe("Bearer test-key");
});

test("an error reply becomes an ApiError with the server's code and message", async () => {
  const api = createHttpApi(
    fakeFetch({ status: 409, json: { error: "collection_exists", message: "taken" } }, []),
  );
  await expect(api.createCollection({ name: "x", retrieval_preset: "small_fast" })).rejects.toEqual(
    new ApiError(409, "collection_exists", "taken"),
  );
});

test("collections are created for the configured tenant", async () => {
  const calls: Call[] = [];
  const api = createHttpApi(fakeFetch({ status: 201, json: { id: "c", name: "x", version: 0 } }, calls));
  await api.createCollection({ name: "x", retrieval_preset: "balanced_rerank" });
  expect(JSON.parse(String(calls[0]?.init.body))).toEqual({
    name: "x",
    tenant_id: "dev",
    retrieval_preset: "balanced_rerank",
  });
});

test("ask streams parsed events and reports whether a final arrived", async () => {
  const calls: Call[] = [];
  const wire = [
    'event: accepted\ndata: {"request_id": "r", "queue_position": 2, "expected_wait_ms": 3000}\n\n',
    'event: sentence\ndata: {"text": "A [P1].", "cit',
    'ations": ["P1"]}\n\nevent: final\ndata: {"status": "ok"}\n\n',
  ];
  const api = createHttpApi(fakeFetch({ status: 200, chunks: wire }, calls));
  const events: AskEvent[] = [];
  const ended = await api.ask({ question: "q?", collection_id: "c1" }, (e) => events.push(e),
    new AbortController().signal);
  expect(ended).toEqual({ sawFinal: true });
  expect(events.map((e) => e.event)).toEqual(["accepted", "sentence", "final"]);
  expect(events[1]).toEqual({ event: "sentence", data: { text: "A [P1].", citations: ["P1"] } });
  expect(header(calls[0]!, "authorization")).toBe("Bearer test-key");
  expect(JSON.parse(String(calls[0]?.init.body))).toEqual({ question: "q?", collection_id: "c1" });
});

test("a stream that ends without a final says so", async () => {
  const api = createHttpApi(
    fakeFetch({ status: 200, chunks: ['event: status\ndata: {"stage": "generating"}\n\n'] }, []),
  );
  const ended = await api.ask({ question: "q?", collection_id: "c" }, () => undefined,
    new AbortController().signal);
  expect(ended).toEqual({ sawFinal: false });
});

test("ask is refused before streaming with the server's error", async () => {
  const api = createHttpApi(
    fakeFetch({ status: 409, json: { error: "collection_not_ready", message: "no docs" } }, []),
  );
  await expect(
    api.ask({ question: "q", collection_id: "c" }, () => undefined, new AbortController().signal),
  ).rejects.toEqual(new ApiError(409, "collection_not_ready", "no docs"));
});
