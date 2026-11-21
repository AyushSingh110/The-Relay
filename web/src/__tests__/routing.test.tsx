// V2-09: the answer says which lane answered when it was not the primary, when it was
// retried on a stronger model, and what it cost; a truncated answer says so.
import { render, screen } from "@testing-library/react";

import type { AskEvent } from "../api";
import { initialAnswer, reasonText, reduce } from "../answer";
import { AnswerView } from "../components/AnswerView";
import { final } from "../test/fakeApi";

function view(events: AskEvent[]) {
  let answer = initialAnswer("What is attention?", 0);
  events.forEach((event, i) => { answer = reduce(answer, { type: "event", event, at: i + 1 }); });
  answer = reduce(answer, { type: "ended", at: 99 });
  render(<AnswerView answer={answer} onOpenSource={() => undefined} onRetry={() => undefined} />);
}

const accepted: AskEvent = {
  event: "accepted", data: { request_id: "r1", queue_position: 0, expected_wait_ms: 0 } };
const sentence: AskEvent = {
  event: "sentence", data: { text: "Attention maps queries [P1].", citations: ["P1"] } };

test("a fallback lane, an escalation and the cost are shown in the footer", () => {
  view([accepted, sentence, { event: "final", data: final({
    lane_name: "backup", fallback: true, escalated: true, cost_usd: 0.000415 }) }]);
  const meta = screen.getByText(/960 tokens/);
  expect(meta).toHaveTextContent("answered by the backup lane");
  expect(meta).toHaveTextContent("retried once on a stronger model");
  expect(meta).toHaveTextContent("cost $0.000415");
});

test("the primary lane, an unknown cost and a free stand-in add nothing", () => {
  view([accepted, sentence, { event: "final", data: final({
    lane_name: "standin", fallback: false, escalated: false, cost_usd: null }) }]);
  const meta = screen.getByText(/960 tokens/);
  expect(meta).not.toHaveTextContent(/lane|stronger|cost/);
});

test("a truncated answer has a plain reason", () => {
  expect(reasonText("truncated")).toBe("the model reached its length limit mid-sentence");
});
