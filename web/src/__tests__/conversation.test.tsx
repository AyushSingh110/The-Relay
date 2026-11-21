// V2-08: the chat page keeps one conversation per page, so follow-ups ("what is its
// latency?") are rewritten by the server; the rewrite is shown to the user.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import type { AskEvent, RelayApi } from "../api";
import { initialAnswer, reduce } from "../answer";
import { ApiProvider } from "../ApiContext";
import { ChatPage } from "../pages/ChatPage";
import { fakeApi, final } from "../test/fakeApi";

const accepted: AskEvent = {
  event: "accepted", data: { request_id: "r1", queue_position: 0, expected_wait_ms: 0 } };
const sentence: AskEvent = {
  event: "sentence", data: { text: "Moshi has a latency of 200 ms [P1].", citations: ["P1"] } };
const rewriting: AskEvent = { event: "status", data: { stage: "rewriting", queue_position: 0 } };
const rewritten = (question: string, changed: boolean): AskEvent => ({
  event: "status", data: { stage: "rewriting", queue_position: 0, rewritten_question: question,
    original_question: "And what is its latency?", rewritten: changed, fallback: null } });
const done: AskEvent = { event: "final", data: final() };

async function ask(question: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Your question"), question);
  await user.keyboard("{Enter}");
}

const turns = () => screen.getAllByRole("article");

test("the first question starts a conversation and follow-ups reuse it", async () => {
  const api = fakeApi({}, [accepted, sentence, done]);
  render(<ApiProvider api={api}><ChatPage collectionId="geo" /></ApiProvider>);
  await ask("How does Moshi work?");
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
  await screen.findByText(/960 tokens/);
  await ask("And what is its latency?");
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(2));
  expect(api.createConversation).toHaveBeenCalledTimes(1);
  expect(api.createConversation).toHaveBeenCalledWith("geo");
  const bodies = (api.ask as jest.Mock).mock.calls.map((call) => call[0]);
  expect(bodies).toEqual([
    { question: "How does Moshi work?", collection_id: "geo", conversation_id: "conv1" },
    { question: "And what is its latency?", collection_id: "geo", conversation_id: "conv1" },
  ]);
});

test("a rewritten follow-up shows how the question was read", async () => {
  const api = fakeApi({}, [accepted, rewriting,
    rewritten("What is Moshi's latency?", true), sentence, done]);
  render(<ApiProvider api={api}><ChatPage collectionId="geo" /></ApiProvider>);
  await ask("And what is its latency?");
  const turn = (await screen.findAllByRole("article"))[0]!;
  expect(await within(turn).findByText("Interpreted as: What is Moshi's latency?"))
    .toBeInTheDocument();
  expect(await axe(turn)).toHaveNoViolations();
});

test("an unchanged follow-up shows no interpretation", async () => {
  const api = fakeApi({}, [accepted, rewriting,
    rewritten("And what is its latency?", false), sentence, done]);
  render(<ApiProvider api={api}><ChatPage collectionId="geo" /></ApiProvider>);
  await ask("And what is its latency?");
  await screen.findByText(/960 tokens/);
  expect(within(turns()[0]!).queryByText(/Interpreted as/)).not.toBeInTheDocument();
});

test("while the question is rewritten the banner says so", () => {
  let answer = initialAnswer("And what is its latency?", 0);
  answer = reduce(answer, { type: "event", event: accepted, at: 1 });
  answer = reduce(answer, { type: "event", event: rewriting, at: 2 });
  expect(answer.stage).toBe("rewriting");
  answer = reduce(answer, { type: "event", event: rewritten("What is Moshi's latency?", true),
    at: 3 });
  expect(answer.rewrittenQuestion).toBe("What is Moshi's latency?");
});

test("the banner text for the rewriting stage", async () => {
  const api = fakeApi({}, async (emit) => {
    emit(accepted);
    emit(rewriting);
    return new Promise<boolean>(() => undefined); // stays on the rewriting stage
  });
  render(<ApiProvider api={api}><ChatPage collectionId="geo" /></ApiProvider>);
  await ask("And what is its latency?");
  expect(await screen.findByText("Reading your question in context…")).toBeInTheDocument();
});

test("if a conversation cannot be started the question is still asked", async () => {
  const api: RelayApi = fakeApi({
    createConversation: jest.fn(async () => { throw new Error("offline"); }),
  }, [accepted, sentence, done]);
  render(<ApiProvider api={api}><ChatPage collectionId="geo" /></ApiProvider>);
  await ask("How does Moshi work?");
  await waitFor(() => expect(api.ask).toHaveBeenCalledWith(
    { question: "How does Moshi work?", collection_id: "geo" },
    expect.any(Function), expect.any(AbortSignal)));
});
