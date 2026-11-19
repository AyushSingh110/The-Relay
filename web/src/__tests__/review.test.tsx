// Regression tests for the V2-06 UI review findings (one per finding).
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import type { AskEvent } from "../api";
import { initialAnswer, reduce, type Answer } from "../answer";
import { ApiProvider } from "../ApiContext";
import { AnswerView } from "../components/AnswerView";
import { ChatPage } from "../pages/ChatPage";
import { match } from "../router";
import { fakeApi, final, passage } from "../test/fakeApi";

const accepted: AskEvent = { event: "accepted",
  data: { request_id: "r", queue_position: 0, expected_wait_ms: 0 } };
const sources: AskEvent = { event: "sources", data: { passages: [passage()] } };

function answerFrom(events: AskEvent[]): Answer {
  return events.reduce((a, event, i) => reduce(a, { type: "event", event, at: i * 10 }),
    initialAnswer("q?", 0));
}

const SAFE = "I could not produce an answer that I can verify against the papers.";

test("a safe refusal (verification failed) is not shown as missing evidence", () => {
  render(<AnswerView onOpenSource={() => undefined} onRetry={() => undefined}
    answer={answerFrom([accepted, sources,
      { event: "stopped", data: { reason_class: "final_verification_failed", partial: false } },
      { event: "final", data: final({ refused: true, verdict: "BLOCKED",
        error_code: "citation_verification_failed", message: SAFE }) }])} />);
  expect(screen.getByText("No answer could be verified against these passages.")).toBeInTheDocument();
  expect(screen.getByText(SAFE)).toBeInTheDocument();
  expect(screen.queryByText(/don't have enough information/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Answer stopped/)).not.toBeInTheDocument(); // one notice, not two
});

test("retrying a lost answer moves focus to that answer, not to the page body", async () => {
  let calls = 0;
  const api = fakeApi({}, async (emit) => {
    calls += 1;
    emit(accepted);
    if (calls === 1) return false; // the first stream ends without a final
    emit({ event: "final", data: final() });
    return true;
  });
  render(<ApiProvider api={api}><ChatPage collectionId="geo" /></ApiProvider>);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Your question"), "What is attention?{Enter}");
  await user.click(await screen.findByRole("button", { name: "Connection lost — retry?" }));
  await waitFor(() => expect(screen.getByRole("article", { name: "Question: What is attention?" }))
    .toHaveFocus());
});

test("stopping an answer returns focus to the question box", async () => {
  const api = fakeApi({}, () => new Promise<boolean>(() => undefined));
  api.ask = jest.fn((_body, onEvent, signal: AbortSignal) => new Promise((_, reject) => {
    onEvent(accepted);
    signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  render(<ApiProvider api={api}><ChatPage collectionId="geo" /></ApiProvider>);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Your question"), "What is attention?{Enter}");
  await user.click(await screen.findByRole("button", { name: "Stop answering" }));
  expect(await screen.findByText("You stopped this answer.")).toBeInTheDocument();
  expect(screen.getByLabelText("Your question")).toHaveFocus();
});

test("retry is disabled, and says why, while another answer is streaming", async () => {
  let emitSecond: ((e: AskEvent) => void) | null = null;
  let call = 0;
  const api = fakeApi({}, (emit) => {
    call += 1;
    emit(accepted);
    if (call === 1) return Promise.resolve(false); // lost
    emitSecond = emit;
    return new Promise<boolean>(() => undefined); // still streaming
  });
  render(<ApiProvider api={api}><ChatPage collectionId="geo" /></ApiProvider>);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Your question"), "First?{Enter}");
  await screen.findByRole("button", { name: "Connection lost — retry?" });
  await user.type(screen.getByLabelText("Your question"), "Second?{Enter}");
  await waitFor(() => expect(emitSecond).not.toBeNull());
  expect(screen.getByRole("button", { name: "Connection lost — retry?" })).toBeDisabled();
  expect(screen.getByText(/after the current answer finishes/)).toBeInTheDocument();
});

test("a malformed collection URL is a missing page, not a crash", () => {
  expect(match("/collections/%E0/chat")).toEqual({ page: "missing" });
  expect(match("/collections/abc%20d/chat")).toEqual({ page: "chat", collectionId: "abc d" });
});

test("an answer with sentences, chips, notices and the time bar has no axe violations", async () => {
  const { container } = render(<AnswerView onOpenSource={() => undefined}
    onRetry={() => undefined} answer={answerFrom([accepted,
      { event: "status", data: { stage: "retrieving", queue_position: 0 } }, sources,
      { event: "status", data: { stage: "generating", queue_position: 0 } },
      { event: "sentence", data: { text: "Attention maps queries [P1].", citations: ["P1"] } },
      { event: "stopped", data: { reason_class: "ungrounded", partial: true } },
      { event: "final", data: final({ partial: true }) }])} />);
  await act(async () => { await Promise.resolve(); });
  expect(await axe(container)).toHaveNoViolations();
});
