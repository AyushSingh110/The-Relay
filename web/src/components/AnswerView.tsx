// One question and its answer, in every state the stream can end in (plan V2-06 "honest
// states"). Every state has words, not only a colour.
import { useState } from "react";

import type { Passage } from "../api";
import {
  chipLabel, expectedWait, reasonText, REFUSAL_TEXT, segments, type Answer, type Sentence,
} from "../answer";
import { TimeBar } from "./TimeBar";

const TAGS = /\s*\[\s*P\s*\d+(?:\s*[,;]\s*P?\s*\d+)*\s*\]/gi;

export type OpenSource = (passageId: string, sentence: string | null) => void;

interface Props {
  answer: Answer;
  onOpenSource: OpenSource;
  onRetry: () => void;
  retryBlocked?: boolean; // another answer is streaming: retry must wait
  id?: string;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function Banner({ answer }: { answer: Answer }) {
  if (answer.phase === "connecting") {
    return <p className="banner" role="status">Sending your question…</p>;
  }
  if (answer.phase !== "streaming") return null;
  let text: string;
  switch (answer.stage) {
    case "queued": {
      const position = answer.queuePosition ?? 0;
      const wait = Math.ceil(expectedWait(answer) / 1000);
      text = position > 0
        ? `Request queued — position ${position}, expected wait ${wait}s`
        : "Request accepted, starting now";
      break;
    }
    case "rewriting":
      text = "Reading your question in context…";
      break;
    case "retrieving":
      text = "Finding relevant passages…";
      break;
    case "generating":
      text = "Writing answer…";
      break;
    case "verifying":
      text = "Checking the whole answer…";
      break;
    default:
      text = "Working…";
  }
  return (
    <p className={`banner banner-${answer.stage ?? "work"}`} role="status">
      <span className="pulse" aria-hidden="true" />
      {text}
    </p>
  );
}

function Chip({ tag, passage, sentence, onOpen }: {
  tag: string; passage: Passage | undefined; sentence: string | null; onOpen: OpenSource;
}) {
  const label = passage ? chipLabel(passage) : tag;
  return (
    <button type="button" className="chip" onClick={() => onOpen(tag, sentence)}
      aria-label={`Source ${tag}: ${label}`}>
      [{label}]
    </button>
  );
}

function SentenceLine({ sentence, sources, onOpen }: {
  sentence: Sentence; sources: Passage[]; onOpen: OpenSource;
}) {
  return (
    <span className="sentence">
      {sentence.text.replace(TAGS, "")}
      {sentence.citations.map((tag) => (
        <Chip key={tag} tag={tag} passage={sources.find((p) => p.id === tag)}
          sentence={sentence.text} onOpen={onOpen} />
      ))}{" "}
    </span>
  );
}

function Pointers({ sources, onOpen, ended = false }: {
  sources: Passage[]; onOpen: OpenSource; ended?: boolean;
}) {
  if (sources.length === 0) {
    return <p>{ended ? "The request ended before any passages were retrieved."
      : "No passages were found for this question."}</p>;
  }
  return (
    <ul className="pointers">
      {sources.map((p) => (
        <li key={p.id}>
          <Chip tag={p.id} passage={p} sentence={null} onOpen={onOpen} />
          <span> pages {p.page_range}{p.section ? `, ${p.section}` : ""}</span>
        </li>
      ))}
    </ul>
  );
}

/** No answer at all (shed, budget, provider down): only passage pointers can be shown. */
function unavailable(answer: Answer): boolean {
  const final = answer.final;
  return final !== null && !final.refused && final.status !== "ok" && answer.stopped === null &&
    answer.sentences.length === 0;
}

/** The retrieved passages, collapsed. Rendered only while open (as browsers expose it). */
function SourcesList({ sources, onOpen }: { sources: Passage[]; onOpen: OpenSource }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="sources" open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>Sources ({sources.length})</summary>
      {open && <Pointers sources={sources} onOpen={onOpen} />}
    </details>
  );
}

const WAIT_HINT = "You can retry after the current answer finishes.";

function Outcome({ answer, onOpen, onRetry, retryBlocked }: {
  answer: Answer; onOpen: OpenSource; onRetry: () => void; retryBlocked: boolean;
}) {
  const { final, stopped, sentences } = answer;
  // a safe refusal: the evidence was there, but no answer passed verification
  const unverifiable = final !== null && final.refused && final.verdict === "BLOCKED";
  if (answer.phase === "lost") {
    return (
      <div className="notice notice-lost" role="alert">
        <p>
          The connection dropped before the answer finished.
          {sentences.length > 0 ? " The sentences above were verified before they were sent." : ""}
        </p>
        <button type="button" className="danger" onClick={onRetry} disabled={retryBlocked}>
          Connection lost — retry?
        </button>
        {retryBlocked && <p className="hint">{WAIT_HINT}</p>}
      </div>
    );
  }
  if (answer.phase === "rejected" && answer.error) {
    return (
      <div className="notice notice-lost" role="alert">
        <p>The question was not accepted: {answer.error.message}</p>
        <button type="button" className="quiet" onClick={onRetry} disabled={retryBlocked}>
          Ask again
        </button>
        {retryBlocked && <p className="hint">{WAIT_HINT}</p>}
      </div>
    );
  }
  if (answer.phase === "cancelled") {
    return <p className="notice notice-muted">You stopped this answer.</p>;
  }
  const parts = [];
  if (stopped && !(unverifiable && sentences.length === 0)) {
    parts.push(stopped.partial || sentences.length > 0 ? (
      <p key="stopped" className="notice notice-partial">
        <strong>
          Answer stopped after {plural(sentences.length, "sentence")}:{" "}
          {reasonText(stopped.reason_class)}.
        </strong>{" "}
        Nothing unverified was shown.
      </p>
    ) : (
      <p key="stopped" className="notice notice-partial">
        <strong>Answer stopped: {reasonText(stopped.reason_class)}.</strong> No sentence could
        be verified, so none is shown.
      </p>
    ));
  }
  if (unverifiable && final) {
    if (sentences.length === 0) {
      parts.push(
        <div key="unverifiable" className="notice notice-partial">
          <p><strong>No answer could be verified against these passages.</strong></p>
          {final.message && <p>{final.message}</p>}
        </div>,
      );
    } // with sentences shown, the "Answer stopped after N sentences" notice says it all
  } else if (final?.refused) {
    parts.push(
      <div key="refused" className="notice notice-refusal">
        <p><strong>{REFUSAL_TEXT}</strong></p>
        {final.message && final.message !== REFUSAL_TEXT && <p>{final.message}</p>}
      </div>,
    );
  } else if (unavailable(answer) && final) {
    const daily = final.error_code === "daily_budget_exhausted";
    parts.push(
      <div key="unavailable" className="notice notice-unavailable">
        <p>
          <strong>
            {daily ? "Daily limit reached. Relevant passages:"
              : `${final.message ?? "No answer could be produced."} Relevant passages:`}
          </strong>
        </p>
        <Pointers sources={answer.sources} onOpen={onOpen} ended />
        {daily && final.message && <p className="hint">{final.message}</p>}
      </div>,
    );
  }
  return <>{parts}</>;
}

export function AnswerView({ answer, onOpenSource, onRetry, retryBlocked = false, id }: Props) {
  const final = answer.final;
  const bar = segments(answer);
  return (
    <article className="turn" id={id} tabIndex={-1} aria-label={`Question: ${answer.question}`}>
      <p className="question">{answer.question}</p>
      {answer.rewrittenQuestion && (
        <p className="interpreted">Interpreted as: {answer.rewrittenQuestion}</p>
      )}
      <Banner answer={answer} />
      <section className="answer" aria-label="Answer" aria-live="polite">
        {answer.sentences.map((s, i) => (
          <SentenceLine key={i} sentence={s} sources={answer.sources} onOpen={onOpenSource} />
        ))}
      </section>
      <Outcome answer={answer} onOpen={onOpenSource} onRetry={onRetry}
        retryBlocked={retryBlocked} />
      {answer.sources.length > 0 && !unavailable(answer) && (
        <SourcesList sources={answer.sources} onOpen={onOpenSource} />
      )}
      {final && (
        <footer className="answer-footer">
          {bar && <TimeBar segments={bar} cached={final.cache_status === "exact_hit"} />}
          <p className="meta">
            {final.tokens_in + final.tokens_out} tokens ({final.tokens_in} in,{" "}
            {final.tokens_out} out), {(final.total_ms / 1000).toFixed(2)} s total
            {final.cache_status === "exact_hit" ? ", from the cache" : ""}
            {final.coalesced ? ", shared with an identical question" : ""}.
          </p>
        </footer>
      )}
    </article>
  );
}
