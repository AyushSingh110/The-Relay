// The seven frontend tests of the V2-06 prompt, plus accessibility (axe) checks.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import type { ReactNode } from "react";

import type { AskEvent, RelayApi } from "../api";
import { initialAnswer, reduce, type Answer } from "../answer";
import { ApiProvider } from "../ApiContext";
import { AnswerView } from "../components/AnswerView";
import { ChatPage } from "../pages/ChatPage";
import { CollectionsPage } from "../pages/CollectionsPage";
import { StatusPage } from "../pages/StatusPage";
import { UploadPage } from "../pages/UploadPage";
import { document, fakeApi, final, passage } from "../test/fakeApi";

const withApi = (api: RelayApi, ui: ReactNode) => render(<ApiProvider api={api}>{ui}</ApiProvider>);

const accepted = (position: number, waitMs: number): AskEvent => ({
  event: "accepted", data: { request_id: "r1", queue_position: position, expected_wait_ms: waitMs },
});
const status = (stage: "queued" | "retrieving" | "generating" | "verifying"): AskEvent => ({
  event: "status", data: { stage, queue_position: 0 },
});
const sources: AskEvent = { event: "sources", data: { passages: [passage(), passage({
  id: "P2", chunk_id: "c2", short_name: "MoE", section: "4 Experiments", page_range: "7",
  text: "Mixture of experts routes each token to two experts.",
})] } };
const sentence = (text: string, citations: string[]): AskEvent => ({
  event: "sentence", data: { text, citations } });

async function ask(question = "What is attention?") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Your question"), question);
  await user.keyboard("{Enter}");
  return user;
}

function answerFrom(events: AskEvent[], end?: "lost"): Answer {
  let a = initialAnswer("What is attention?", 0);
  events.forEach((event, i) => { a = reduce(a, { type: "event", event, at: (i + 1) * 10 }); });
  return end ? reduce(a, { type: "ended", at: 999 }) : a;
}

// 1 --------------------------------------------------------------------------------------
test("the collections page renders a collection card from the API", async () => {
  withApi(fakeApi(), <CollectionsPage />);
  const card = await screen.findByRole("article", { name: "Geology notes" });
  expect(within(card).getByText("4 documents")).toBeInTheDocument();
  expect(within(card).getByText("Version 3")).toBeInTheDocument();
  expect(within(card).getByText("large_wide")).toBeInTheDocument();
  expect(within(card).getByRole("link", { name: "Geology notes" }))
    .toHaveAttribute("href", "/collections/geo/chat");
});

// 2 --------------------------------------------------------------------------------------
test("the upload page shows ingestion progress in order: queued then ready", async () => {
  // every state comes from polling GET /v1/documents/:id (the upload reply says "sending")
  const states = (["queued", "parsing", "embedding", "indexing", "ready"] as const)
    .map((s) => document({}, s));
  const api = fakeApi({
    uploadPdf: jest.fn(async () => ({ id: "doc1", status: "pending" })),
    getDocument: jest.fn(async () => states.shift() ?? document({}, "ready")),
  });
  withApi(api, <UploadPage collectionId="geo" pollMs={5} />);
  const seen: string[] = [];
  const observer = new MutationObserver(() => {
    const line = window.document.querySelector(".progress .state")?.textContent ?? "";
    const state = /Step \d of 5: (\w+)/.exec(line)?.[1];
    if (state && seen[seen.length - 1] !== state) seen.push(state);
  });
  observer.observe(window.document.body, { subtree: true, childList: true, characterData: true });
  await userEvent.setup().upload(screen.getByLabelText("Choose a PDF"),
    new File(["%PDF-1.7"], "rocks.pdf", { type: "application/pdf" }));
  const progress = await screen.findByRole("status", { name: "rocks.pdf" });
  await within(progress).findByText(/10 pages, 1 empty/);
  observer.disconnect();
  expect(seen).toEqual(["Queued", "Parsing", "Embedding", "Indexing", "Ready"]);
});

test("a document that is already ready shows its extraction quality", async () => {
  const scan = document({ extraction_quality: { total_pages: 10, empty_pages: 8,
    suspected_scan: true } }, "ready");
  const api = fakeApi({
    uploadPdf: jest.fn(async () => ({ id: "doc1", status: "ready", deduplicated: true })),
    getDocument: jest.fn(async () => scan),
  });
  withApi(api, <UploadPage collectionId="geo" pollMs={5} />);
  await userEvent.setup().upload(screen.getByLabelText("Choose a PDF"),
    new File(["%PDF-1.7"], "scan.pdf", { type: "application/pdf" }));
  expect(await screen.findByText("This PDF may be a scan — answers may be incomplete"))
    .toBeInTheDocument();
  expect(screen.getByText("This PDF is already in the collection.")).toBeInTheDocument();
});

test("retrying a failed upload keeps keyboard focus on its progress card", async () => {
  const api = fakeApi({ getDocument: jest.fn(async () => document({}, "failed")) });
  withApi(api, <UploadPage collectionId="geo" pollMs={5} />);
  const user = userEvent.setup();
  await user.upload(screen.getByLabelText("Choose a PDF"),
    new File(["%PDF-1.7"], "broken.pdf", { type: "application/pdf" }));
  await user.click(await screen.findByRole("button", { name: "Retry upload of broken.pdf" }));
  expect(screen.getByRole("status", { name: "broken.pdf" })).toHaveFocus();
});

test("the upload page refuses a file that is not a PDF before uploading", async () => {
  const api = fakeApi();
  withApi(api, <UploadPage collectionId="geo" pollMs={5} />);
  const user = userEvent.setup({ applyAccept: false });
  await user.upload(screen.getByLabelText("Choose a PDF"),
    new File(["hello"], "notes.txt", { type: "text/plain" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Only PDF files can be added");
  expect(api.uploadPdf).not.toHaveBeenCalled();
});

// 3 --------------------------------------------------------------------------------------
test("the chat page appends a sentence event's text to the answer", async () => {
  const api = fakeApi({}, [accepted(0, 0), status("retrieving"), sources, status("generating"),
    sentence("Attention maps a query to an output [P1].", ["P1"]),
    sentence("Experts route tokens [P2].", ["P2"]), { event: "final", data: final() }]);
  withApi(api, <ChatPage collectionId="geo" />);
  await ask();
  const answer = await screen.findByRole("region", { name: "Answer" });
  await waitFor(() => expect(answer).toHaveTextContent("Experts route tokens"));
  expect(answer).toHaveTextContent("Attention maps a query to an output");
  expect(api.ask).toHaveBeenCalledWith({ question: "What is attention?", collection_id: "geo",
    conversation_id: "conv1" }, // V2-08: every chat page keeps one conversation
    expect.any(Function), expect.any(AbortSignal));
  expect(await screen.findByText(/960 tokens/)).toBeInTheDocument(); // the footer
});

// 4 --------------------------------------------------------------------------------------
test("the chat page shows a stopped notice on a stopped event", async () => {
  withApi(fakeApi({}, [accepted(0, 0), sources, status("generating"),
    sentence("Attention maps a query [P1].", ["P1"]),
    { event: "stopped", data: { reason_class: "ungrounded", partial: true } },
    { event: "final", data: final({ partial: true, verdict: "BLOCKED" }) }]),
  <ChatPage collectionId="geo" />);
  await ask();
  const notice = await screen.findByText(/Answer stopped after 1 sentence/);
  expect(notice).toHaveTextContent("not supported by its cited passage");
});

// 5 --------------------------------------------------------------------------------------
test("a citation chip opens the source panel with the cited passage", async () => {
  withApi(fakeApi({}, [accepted(0, 0), sources, status("generating"),
    sentence("Experts route each token to two experts [P2].", ["P2"]),
    { event: "final", data: final() }]), <ChatPage collectionId="geo" />);
  const user = await ask();
  await user.click(await screen.findByRole("button", { name: /MoE §4/ }));
  const panel = await screen.findByRole("dialog", { name: "Source P2" });
  expect(panel).toHaveTextContent("MoE");
  expect(panel).toHaveTextContent("pages 7");
  expect(panel).toHaveTextContent("4 Experiments");
  expect(within(panel).getByText("each token to two experts").tagName).toBe("MARK");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

// 6 --------------------------------------------------------------------------------------
test("the chat page shows the queue position from an accepted event", async () => {
  let emit: (e: AskEvent) => void = () => undefined;
  const api = fakeApi({}, (onEvent) => {
    emit = onEvent;
    return new Promise<boolean>(() => undefined); // stays open
  });
  withApi(api, <ChatPage collectionId="geo" />);
  await ask();
  act(() => emit(accepted(3, 4200)));
  expect(await screen.findByText("Request queued — position 3, expected wait 5s"))
    .toBeInTheDocument();
  act(() => emit({ event: "status", data: { stage: "queued", queue_position: 1, keepalive: true } }));
  expect(await screen.findByText(/Request queued — position 1/)).toBeInTheDocument();
  act(() => emit(status("retrieving")));
  expect(await screen.findByText("Finding relevant passages…")).toBeInTheDocument();
});

// 7 --------------------------------------------------------------------------------------
describe("every honest state renders", () => {
  const view = (answer: Answer) =>
    render(<AnswerView answer={answer} onOpenSource={() => undefined} onRetry={() => undefined} />);

  test("queued", () => {
    view(answerFrom([accepted(2, 1500)]));
    expect(screen.getByText("Request queued — position 2, expected wait 2s")).toBeInTheDocument();
  });

  test("partial answer", () => {
    view(answerFrom([accepted(0, 0), sources, sentence("A [P1].", ["P1"]),
      sentence("B [P1].", ["P1"]),
      { event: "stopped", data: { reason_class: "uncited", partial: true } },
      { event: "final", data: final({ partial: true }) }]));
    expect(screen.getByText(/Answer stopped after 2 sentences/)).toHaveTextContent("no citation");
  });

  test("refusal", () => {
    view(answerFrom([accepted(0, 0), sources, { event: "final", data: final({
      refused: true, verdict: "REFUSAL_VERIFIED", message: "I can't answer this from the papers." }) }]));
    expect(screen.getByText("These passages don't have enough information to answer this."))
      .toBeInTheDocument();
    expect(screen.getByText("I can't answer this from the papers.")).toBeInTheDocument();
  });

  test("unavailable: daily limit, with the passage pointers", () => {
    view(answerFrom([accepted(0, 0), sources, { event: "final", data: final({
      status: "rate_limited", verdict: "NOT_RUN", error_code: "daily_budget_exhausted",
      message: "The daily model budget for this service is used up." }) }]));
    expect(screen.getByText("Daily limit reached. Relevant passages:")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Attention §3.2/ })).toBeInTheDocument();
  });

  test("unavailable before retrieval (shed): says no passages were retrieved", () => {
    view(answerFrom([accepted(1, 900), { event: "final", data: final({
      status: "rate_limited", verdict: "NOT_RUN", error_code: "queue_full",
      message: "The service is at capacity and did not accept this request." }) }]));
    expect(screen.getByText(/at capacity and did not accept this request\. Relevant passages:/))
      .toBeInTheDocument();
    expect(screen.getByText("The request ended before any passages were retrieved."))
      .toBeInTheDocument();
  });

  test("connection lost", () => {
    const onRetry = jest.fn();
    render(<AnswerView answer={answerFrom([accepted(0, 0), sentence("A [P1].", ["P1"])], "lost")}
      onOpenSource={() => undefined} onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Connection lost");
    screen.getByRole("button", { name: "Connection lost — retry?" }).click();
    expect(onRetry).toHaveBeenCalled();
  });

  test("ingestion failed, with a retry", async () => {
    const api = fakeApi({ getDocument: jest.fn(async () => document({}, "failed")) });
    withApi(api, <UploadPage collectionId="geo" pollMs={5} />);
    await userEvent.setup().upload(screen.getByLabelText("Choose a PDF"),
      new File(["%PDF-1.7"], "broken.pdf", { type: "application/pdf" }));
    expect(await screen.findByText(/PdfParseError/)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry upload of broken.pdf" }));
    await waitFor(() => expect(api.uploadPdf).toHaveBeenCalledTimes(2));
  });

  test("suspected scan", async () => {
    const scan = document({ extraction_quality: { total_pages: 10, empty_pages: 8,
      suspected_scan: true } }, "ready");
    withApi(fakeApi({ getDocument: jest.fn(async () => scan) }),
      <UploadPage collectionId="geo" pollMs={5} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("arXiv ID"), "2410.00037v1");
    await user.click(screen.getByRole("button", { name: "Add from arXiv" }));
    expect(await screen.findByText("This PDF may be a scan — answers may be incomplete"))
      .toBeInTheDocument();
  });
});

// accessibility (jest-axe) ----------------------------------------------------------------
test("the pages have no detectable accessibility violations", async () => {
  const api = fakeApi({ getDocument: jest.fn(async () => document({}, "ready")) },
    [accepted(0, 0), sources, sentence("Attention maps a query [P1].", ["P1"]),
      { event: "final", data: final() }]);
  for (const page of [<CollectionsPage key="c" />, <UploadPage key="u" collectionId="geo" />,
    <StatusPage key="s" />, <ChatPage key="h" collectionId="geo" />]) {
    const { container, unmount } = withApi(api, page);
    await screen.findAllByRole("heading", { level: 1 });
    await act(async () => { await Promise.resolve(); });
    expect(await axe(container)).toHaveNoViolations();
    unmount();
  }
});

test("the status page shows the health report and the quota placeholder", async () => {
  withApi(fakeApi(), <StatusPage />);
  expect(await screen.findByText("sqlite")).toBeInTheDocument();
  const log = screen.getByRole("table", { name: "Request log" });
  expect(within(log).getByText("12")).toBeInTheDocument();
  expect(screen.getByText(/coming in V2-11/)).toBeInTheDocument();
});

test("the collections page creates a collection from the modal", async () => {
  const api = fakeApi();
  withApi(api, <CollectionsPage />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "New collection" }));
  const dialog = screen.getByRole("dialog", { name: "New collection" });
  expect(within(dialog).getByLabelText("Name")).toHaveFocus();
  await user.type(within(dialog).getByLabelText("Name"), "Papers");
  await user.selectOptions(within(dialog).getByLabelText("Retrieval preset"), "balanced_rerank");
  await user.click(within(dialog).getByRole("button", { name: "Create collection" }));
  expect(api.createCollection).toHaveBeenCalledWith({ name: "Papers",
    retrieval_preset: "balanced_rerank" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});
