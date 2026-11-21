// V2-12: the evaluation review screen (/eval): answers with their passages and citation chips,
// the three graders side by side, and one-click owner grading with notes.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import type { EvalRunDetail, EvalSummary, RelayApi } from "../api";
import { createHttpApi } from "../api";
import { ApiProvider } from "../ApiContext";
import { EvalReviewPage } from "../pages/EvalReviewPage";
import { match } from "../router";
import { fakeApi } from "../test/fakeApi";

const run: EvalRunDetail = {
  run: { id: "run1", eval_set_id: "set1", kind: "replay", lane: "standin", model_id: "m",
    retrieval_setting: "small_fast", status: "complete", created_at: "2026-11-21T00:00:00Z",
    source: "exp-1" },
  answers: [
    { id: "a1", source_id: "q01", question: "Why the Transformer?", answerable: true,
      answer_text: "Recurrence is sequential [P1].", refused: false, verdict: "VERIFIED",
      citations: [], passages: [{ paper_id: "1706.03762v7", short_name: "attention",
        page_start: 2, page_end: 2, section: "1 Introduction",
        text: "Recurrent models are sequential." }],
      grades: { rubric: { verdict: "wrong", score: 0, notes: '{"reasons": ["missing facts"]}' },
        entailment: { verdict: null, score: 0.812, notes: null }, human: null } },
    { id: "a2", source_id: "q16", question: "What is GPT-9?", answerable: false,
      answer_text: "", refused: true, verdict: "EARLY_REFUSAL", citations: [], passages: [],
      grades: { rubric: { verdict: "refused_correctly", score: 1, notes: null },
        entailment: null, human: { verdict: "UNANSWERABLE_CORRECT", score: 1, notes: null } } },
  ],
};

const summary: EvalSummary = {
  answers: 2,
  rubric: { correct: 0, answerable: 1, refused_correctly: 1, unanswerable: 1,
    correctness: 0, refusal_precision: 1, grounded: 1, grounded_rate: 1 },
  entailment: { scored: 1, mean: 0.812 },
  human: { graded: 1, verdicts: { UNANSWERABLE_CORRECT: 1 }, correct_share: 1 },
  rubric_vs_human: { both: 1, agree: 1 },
  l4_calibration: "needs owner grades (the human grader) on scored answers",
};

function evalApi(over: Partial<RelayApi> = {}): RelayApi {
  return fakeApi({
    evalSets: jest.fn(async () => [{ id: "set1", name: "v1", collection_id: "v1-papers",
      collection_name: "v1-papers", questions: 21, created_at: "2026-11-21" }]),
    evalRuns: jest.fn(async () => [run.run]),
    evalRun: jest.fn(async () => run),
    evalSummary: jest.fn(async () => summary),
    gradeAnswer: jest.fn(async () => ({ id: "g1", answer_id: "a1", verdict: "CORRECT", score: 1 })),
    ...over,
  });
}

test("/eval is the review screen", () => {
  expect(match("/eval")).toEqual({ page: "eval" });
});

test("the screen shows each answer, its passages and the three graders side by side", async () => {
  const { container } = render(<ApiProvider api={evalApi()}><EvalReviewPage /></ApiProvider>);
  const first = await screen.findByRole("article", { name: /q01/ });
  expect(within(first).getByText("Why the Transformer?")).toBeInTheDocument();
  expect(within(first).getByRole("button", { name: /Source P1/ })).toBeInTheDocument();
  expect(within(first).getByText("wrong")).toBeInTheDocument(); // rubric
  expect(within(first).getByText("0.812")).toBeInTheDocument(); // entailment
  expect(within(first).getByText("Not reviewed")).toBeInTheDocument(); // human
  const sideBySide = screen.getByRole("table", { name: "Graders side by side" });
  expect(within(sideBySide).getByText("0.0% (0 of 1)")).toBeInTheDocument();
  expect(within(sideBySide).getByText("0.812 over 1 answer")).toBeInTheDocument();
  expect(await axe(container)).toHaveNoViolations();
});

test("one click records the owner's grade with notes", async () => {
  const api = evalApi();
  render(<ApiProvider api={api}><EvalReviewPage /></ApiProvider>);
  const first = await screen.findByRole("article", { name: /q01/ });
  const user = userEvent.setup();
  await user.type(within(first).getByLabelText("Notes for q01"), "facts are there");
  await user.click(within(first).getByRole("button", { name: "Correct" }));
  await waitFor(() => expect(api.gradeAnswer).toHaveBeenCalledWith(
    { answer_id: "a1", verdict: "CORRECT", notes: "facts are there" }));
  expect(await within(first).findByText("Correct", { selector: ".human-verdict" }))
    .toBeInTheDocument();
  expect(api.evalSummary).toHaveBeenCalledTimes(2); // the totals are refreshed
});

test("an unanswerable question can be graded as a correct refusal", async () => {
  render(<ApiProvider api={evalApi()}><EvalReviewPage /></ApiProvider>);
  const second = await screen.findByRole("article", { name: /q16/ });
  expect(within(second).getByRole("button", { name: "Correct refusal" })).toBeInTheDocument();
  expect(within(second).getByText("The answer was a refusal.")).toBeInTheDocument();
});

test("the eval API calls", async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  const api = createHttpApi(async (url, init) => {
    calls.push({ url: String(url), init: init ?? {} });
    return { ok: true, status: 200, json: async () => ({ sets: [], runs: [] }) } as Response;
  });
  await api.evalSets();
  await api.evalRuns("s 1");
  await api.gradeAnswer({ answer_id: "a1", verdict: "PARTIAL", notes: null });
  expect(calls.map((c) => c.url)).toEqual(
    ["/v1/eval/sets", "/v1/eval/sets/s%201/runs", "/v1/eval/grades"]);
  expect(JSON.parse(String(calls[2]!.init.body))).toEqual(
    { answer_id: "a1", verdict: "PARTIAL", notes: null });
});
