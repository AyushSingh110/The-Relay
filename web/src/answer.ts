// One answer's state, built from the /v1/ask events (pure functions; the Chat page renders
// it). Nothing here invents content: sentences are exactly the verified "sentence" events.

import type { AskEvent, Final, Passage, Stage } from "./api";

export type Phase = "connecting" | "streaming" | "done" | "lost" | "rejected" | "cancelled";

export interface Sentence {
  text: string;
  citations: string[];
}

export interface Answer {
  question: string;
  phase: Phase;
  requestId: string | null;
  queuePosition: number | null;
  expectedWaitMs: number | null; // quoted by "accepted", for acceptedPosition
  acceptedPosition: number | null;
  stage: Stage | null;
  rewrittenQuestion: string | null; // set only when the server changed the question (V2-08)
  sources: Passage[];
  sentences: Sentence[];
  stopped: { reason_class: string; partial: boolean } | null;
  final: Final | null;
  error: { status: number; code: string; message: string } | null;
  marks: Partial<Record<"start" | "accepted" | Stage | "lastSentence" | "final", number>>;
}

export type Action =
  | { type: "event"; event: AskEvent; at: number }
  | { type: "ended"; at: number } // the stream closed (a final may or may not have come)
  | { type: "lost"; at: number } // the connection failed mid-stream
  | { type: "rejected"; error: { status: number; code: string; message: string } }
  | { type: "cancelled" };

export function initialAnswer(question: string, at: number): Answer {
  return {
    question, phase: "connecting", requestId: null, queuePosition: null, expectedWaitMs: null,
    acceptedPosition: null, stage: null, rewrittenQuestion: null, sources: [], sentences: [], stopped: null, final: null, error: null,
    marks: { start: at },
  };
}

function mark(answer: Answer, name: keyof Answer["marks"], at: number): Answer["marks"] {
  return answer.marks[name] === undefined ? { ...answer.marks, [name]: at } : answer.marks;
}

export function reduce(answer: Answer, action: Action): Answer {
  if (answer.phase === "done" || answer.phase === "cancelled") return answer;
  switch (action.type) {
    case "ended":
      return answer.final ? answer : { ...answer, phase: "lost" };
    case "lost":
      return { ...answer, phase: "lost" };
    case "rejected":
      return { ...answer, phase: "rejected", error: action.error };
    case "cancelled":
      return { ...answer, phase: "cancelled", stage: null };
    case "event":
      return apply(answer, action.event, action.at);
  }
}

function apply(answer: Answer, event: AskEvent, at: number): Answer {
  const streaming = { ...answer, phase: "streaming" as Phase };
  switch (event.event) {
    case "accepted":
      return {
        ...streaming, requestId: event.data.request_id, stage: "queued",
        queuePosition: event.data.queue_position, expectedWaitMs: event.data.expected_wait_ms,
        acceptedPosition: event.data.queue_position, marks: mark(answer, "accepted", at),
      };
    case "status":
      return {
        ...streaming, stage: event.data.stage, queuePosition: event.data.queue_position,
        marks: mark(answer, event.data.stage, at),
        rewrittenQuestion: event.data.rewritten === true && event.data.rewritten_question
          ? event.data.rewritten_question : answer.rewrittenQuestion,
      };
    case "sources":
      return { ...streaming, sources: event.data.passages };
    case "sentence":
      return {
        ...streaming, sentences: [...answer.sentences, event.data],
        marks: { ...answer.marks, lastSentence: at },
      };
    case "stopped":
      return { ...streaming, stopped: event.data };
    case "final":
      return { ...answer, phase: "done", stage: null, final: event.data,
        marks: mark(answer, "final", at) };
  }
}

// --- the time bar --------------------------------------------------------------------

export interface Segments {
  estimated: boolean;
  parts: { label: "Queue" | "Retrieval" | "Model" | "Verification"; ms: number }[];
}

/** Queue | Retrieval | Model | Verification: the server's spans when it sends them,
 * otherwise estimated from when each stage's status event arrived. Null until final. */
export function segments(answer: Answer): Segments | null {
  const final = answer.final;
  if (final === null) return null;
  if (final.spans) {
    const s = final.spans;
    return { estimated: false, parts: [
      { label: "Queue", ms: s.queue_ms }, { label: "Retrieval", ms: s.retrieval_ms },
      { label: "Model", ms: s.model_ms }, { label: "Verification", ms: s.verification_ms },
    ] };
  }
  const m = answer.marks;
  const end = m.final ?? 0;
  const begin = m.accepted ?? m.start ?? 0;
  const span = (from: number | undefined, to: number | undefined) =>
    from === undefined ? 0 : Math.max(0, Math.round((to ?? end) - from));
  const cached = final.cache_status === "exact_hit";
  return { estimated: true, parts: [
    // a follow-up's rewrite happens before retrieval: it counts as queue time here
    { label: "Queue", ms: cached ? 0 : span(begin, m.retrieving ?? m.generating) },
    { label: "Retrieval", ms: span(m.retrieving, m.generating) },
    { label: "Model", ms: span(m.generating, m.verifying ?? m.lastSentence) },
    { label: "Verification", ms: span(m.verifying, end) },
  ] };
}

// --- citations -------------------------------------------------------------------------

const SECTION_NUMBER = /^(\d+(?:\.\d+)*)\s/;

/** "Attention §3.2" (paper and section), else "Attention p. 3-4". */
export function chipLabel(passage: Passage): string {
  const name = passage.short_name ?? passage.paper_id;
  const number = passage.section ? SECTION_NUMBER.exec(passage.section) : null;
  return number ? `${name} §${number[1]}` : `${name} p. ${passage.page_range}`;
}

export interface Part {
  text: string;
  match: boolean;
}

const escape = (word: string) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const MIN_WORDS = 3;

/** The passage split around the longest run of the sentence's words (at least three, in
 * order, ignoring case, punctuation and citation tags) that it contains. */
export function highlight(passage: string, sentence: string): Part[] {
  const words = sentence.replace(/\[[^\]]*\]/g, " ").match(/[A-Za-z0-9]+/g) ?? [];
  for (let size = words.length; size >= MIN_WORDS; size -= 1) {
    for (let start = 0; start + size <= words.length; start += 1) {
      const pattern = words.slice(start, start + size).map(escape).join("[^A-Za-z0-9]+");
      const found = new RegExp(`\\b${pattern}\\b`, "i").exec(passage);
      if (found) {
        const parts = [
          { text: passage.slice(0, found.index), match: false },
          { text: found[0], match: true },
          { text: passage.slice(found.index + found[0].length), match: false },
        ];
        return parts.filter((p) => p.text !== "");
      }
    }
  }
  return [{ text: passage, match: false }];
}

// --- words for states ------------------------------------------------------------------

const REASONS: Record<string, string> = {
  ungrounded: "the next sentence was not supported by its cited passage",
  uncited: "the next sentence had no citation",
  unknown_citation: "the next sentence cited a passage that was not retrieved",
  metadata_mismatch: "the next sentence named the wrong paper, section or page",
  refusal_with_content: "the model mixed a refusal with an answer",
  refusal_as_claim: "the next sentence said the passages do not contain the answer",
  final_verification_failed: "the whole answer did not pass the final check",
  truncated: "the model reached its length limit mid-sentence",
  provider_error: "the model provider failed mid-answer",
  client_gave_up: "the request was abandoned",
  internal_error: "the service hit an internal error",
  service_shutdown: "the service is shutting down",
  unverified: "the next sentence could not be verified",
};

export function reasonText(reasonClass: string): string {
  return REASONS[reasonClass] ?? reasonClass.replace(/_/g, " ");
}

/** The expected wait now: the wait quoted by "accepted", scaled to the current position. */
export function expectedWait(answer: Answer): number {
  const { acceptedPosition, expectedWaitMs, queuePosition } = answer;
  if (!acceptedPosition || expectedWaitMs === null || queuePosition === null) return 0;
  return (expectedWaitMs * queuePosition) / acceptedPosition;
}

export const REFUSAL_TEXT = "These passages don't have enough information to answer this.";
