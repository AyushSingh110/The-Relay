// Evaluation review (V2-12): one run's answers, with the passages each was given, its citation
// chips, and the three graders side by side; the owner grades each answer in one click.
// Only the owner's grades count as reviewed (ARCHITECTURE_V2 §10).
import { useCallback, useEffect, useState } from "react";

import type {
  EvalAnswer, EvalPassage, EvalRunDetail, EvalRunInfo, EvalSet, EvalSummary, HumanVerdict,
} from "../api";
import { useApi } from "../ApiContext";

const TAGS = /\[\s*P\s*\d+(?:\s*[,;]\s*P?\s*\d+)*\s*\]/gi;
const VERDICT_TEXT: Record<string, string> = {
  CORRECT: "Correct", PARTIAL: "Partial", INCORRECT: "Incorrect",
  UNANSWERABLE_CORRECT: "Correct refusal",
};

const pct = (value: number | null, n: number, of: number) =>
  value === null ? "n/a" : `${(value * 100).toFixed(1)}% (${n} of ${of})`;

function passageLabel(p: EvalPassage): string {
  const pages = p.page_start === p.page_end ? `${p.page_start}` : `${p.page_start}-${p.page_end}`;
  return `${p.short_name ?? p.paper_id} p. ${pages}`;
}

function rubricReasons(notes: string | null): string | null {
  if (!notes) return null;
  try {
    const parsed = JSON.parse(notes) as { reasons?: string[] };
    return parsed.reasons?.join("; ") ?? null;
  } catch {
    return null;
  }
}

function AnswerText({ answer, open, onOpen }: {
  answer: EvalAnswer; open: number | null; onOpen: (index: number) => void;
}) {
  if (answer.refused) return <p className="eval-answer muted">The answer was a refusal.</p>;
  const parts: (string | number)[] = [];
  let last = 0;
  for (const tag of answer.answer_text.matchAll(TAGS)) {
    parts.push(answer.answer_text.slice(last, tag.index));
    for (const number of tag[0].match(/\d+/g) ?? []) parts.push(Number(number));
    last = (tag.index ?? 0) + tag[0].length;
  }
  parts.push(answer.answer_text.slice(last));
  return (
    <p className="eval-answer">
      {parts.map((part, i) => {
        if (typeof part === "string") return <span key={i}>{part}</span>;
        const passage = answer.passages[part - 1];
        return (
          <button key={i} type="button" className="chip" aria-expanded={open === part - 1}
            aria-label={`Source P${part}${passage ? `: ${passageLabel(passage)}` : ""}`}
            onClick={() => onOpen(part - 1)}>
            [{passage ? passageLabel(passage) : `P${part}`}]
          </button>
        );
      })}
    </p>
  );
}

function AnswerCard({ answer, onGrade }: {
  answer: EvalAnswer; onGrade: (answer: EvalAnswer, verdict: HumanVerdict, notes: string) => Promise<void>;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const [notes, setNotes] = useState(answer.grades.human?.notes ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = answer.source_id ?? answer.id;
  const { rubric, entailment, human } = answer.grades;
  const verdicts: HumanVerdict[] = answer.answerable
    ? ["CORRECT", "PARTIAL", "INCORRECT"] : ["UNANSWERABLE_CORRECT", "INCORRECT"];
  const grade = async (verdict: HumanVerdict) => {
    setSaving(true);
    setError(null);
    try {
      await onGrade(answer, verdict, notes);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "the grade was not saved");
    } finally {
      setSaving(false);
    }
  };
  const shown = open === null ? undefined : answer.passages[open];
  return (
    <article className="eval-card" aria-label={`${label}: ${answer.question ?? ""}`}>
      <header>
        <span className="eval-id">{label}</span>
        <span className="tag tag-plain">{answer.answerable ? "answerable" : "unanswerable"}</span>
      </header>
      <p className="question">{answer.question}</p>
      <AnswerText answer={answer} open={open} onOpen={(i) => setOpen(open === i ? null : i)} />
      {shown && (
        <blockquote className="eval-passage">
          <p className="meta">{passageLabel(shown)}{shown.section ? `, ${shown.section}` : ""}</p>
          {shown.text}
        </blockquote>
      )}
      {answer.passages.length > 0 && (
        <details className="sources">
          <summary>Retrieved passages ({answer.passages.length})</summary>
          <ol>{answer.passages.map((p, i) => (
            <li key={i}><strong>{passageLabel(p)}</strong> {p.text}</li>
          ))}</ol>
        </details>
      )}
      <dl className="eval-grades">
        <div><dt>Rubric</dt><dd>{rubric?.verdict ?? "Not graded"}</dd></div>
        <div><dt>Entailment</dt>
          <dd>{entailment?.score === null || entailment?.score === undefined
            ? "Not scored" : entailment.score.toFixed(3)}</dd></div>
        <div><dt>Owner</dt>
          <dd>{human?.verdict
            ? <span className="human-verdict">{VERDICT_TEXT[human.verdict] ?? human.verdict}</span>
            : "Not reviewed"}</dd></div>
      </dl>
      {rubricReasons(rubric?.notes ?? null) && (
        <p className="hint">Rubric: {rubricReasons(rubric?.notes ?? null)}</p>
      )}
      <div className="eval-actions">
        <label htmlFor={`notes-${answer.id}`} className="visually-hidden">Notes for {label}</label>
        <textarea id={`notes-${answer.id}`} rows={2} value={notes} placeholder="Notes (optional)"
          onChange={(e) => setNotes(e.target.value)} />
        <div className="actions">
          {verdicts.map((v) => (
            <button key={v} type="button" disabled={saving}
              className={human?.verdict === v ? "" : "quiet"} onClick={() => void grade(v)}>
              {VERDICT_TEXT[v]}
            </button>
          ))}
        </div>
        {error && <p role="alert" className="error">The grade was not saved: {error}</p>}
      </div>
    </article>
  );
}

function SideBySide({ summary }: { summary: EvalSummary }) {
  const { rubric, entailment, human, rubric_vs_human: agreement } = summary;
  return (
    <table aria-label="Graders side by side" className="eval-summary">
      <caption>Graders side by side</caption>
      <tbody>
        <tr><th scope="row">Rubric correctness</th>
          <td>{pct(rubric.correctness, rubric.correct, rubric.answerable)}</td></tr>
        <tr><th scope="row">Grounded answers</th>
          <td>{pct(rubric.grounded_rate, rubric.grounded, rubric.answerable)}</td></tr>
        <tr><th scope="row">Refusal precision</th>
          <td>{pct(rubric.refusal_precision, rubric.refused_correctly, rubric.unanswerable)}</td></tr>
        <tr><th scope="row">Entailment (advisory)</th>
          <td>{entailment.mean === null ? "Not scored"
            : `${entailment.mean.toFixed(3)} over ${entailment.scored} answer${entailment.scored === 1 ? "" : "s"}`}</td></tr>
        <tr><th scope="row">Owner reviewed</th>
          <td>{human.graded} of {summary.answers}
            {human.correct_share === null ? "" : `, ${(human.correct_share * 100).toFixed(1)}% correct`}</td></tr>
        <tr><th scope="row">Rubric agrees with owner</th>
          <td>{agreement.both === 0 ? "No reviewed answers yet" : `${agreement.agree} of ${agreement.both}`}</td></tr>
      </tbody>
    </table>
  );
}

export function EvalReviewPage() {
  const api = useApi();
  const [sets, setSets] = useState<EvalSet[] | null>(null);
  const [setId, setSetId] = useState<string | null>(null);
  const [runs, setRuns] = useState<EvalRunInfo[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [summary, setSummary] = useState<EvalSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Evaluation | The Relay";
    api.evalSets().then((found) => {
      setSets(found);
      setSetId(found[0]?.id ?? null);
    }, (failure: Error) => setError(failure.message));
  }, [api]);

  useEffect(() => {
    if (setId === null) return;
    api.evalRuns(setId).then((found) => {
      setRuns(found);
      setRunId(found[found.length - 1]?.id ?? null); // the newest run
    }, (failure: Error) => setError(failure.message));
  }, [api, setId]);

  const refreshSummary = useCallback((id: string) => {
    api.evalSummary(id).then(setSummary, (failure: Error) => setError(failure.message));
  }, [api]);

  useEffect(() => {
    if (runId === null) return;
    api.evalRun(runId).then(setDetail, (failure: Error) => setError(failure.message));
    refreshSummary(runId);
  }, [api, runId, refreshSummary]);

  const onGrade = async (answer: EvalAnswer, verdict: HumanVerdict, notes: string) => {
    const saved = await api.gradeAnswer({ answer_id: answer.id, verdict,
      notes: notes.trim() === "" ? null : notes.trim() });
    setDetail((current) => current && { ...current, answers: current.answers.map((a) =>
      a.id === answer.id ? { ...a, grades: { ...a.grades,
        human: { verdict: saved.verdict, score: saved.score, notes: notes.trim() || null } } } : a) });
    if (runId !== null) refreshSummary(runId);
  };

  return (
    <section className="page eval">
      <header className="page-head">
        <h1>Evaluation</h1>
        <p className="sub">
          Each answer with the passages it was given. The rubric and entailment scores are
          proposals; only your grade counts as reviewed.
        </p>
      </header>
      {error && <p role="alert" className="error">{error}</p>}
      {sets !== null && sets.length === 0 && (
        <p className="empty">No evaluation sets yet. Import the v1 set with
          {" "}<code>relay eval import-v1</code>.</p>
      )}
      {sets !== null && sets.length > 0 && (
        <div className="eval-pickers">
          <label>Evaluation set
            <select value={setId ?? ""} onChange={(e) => setSetId(e.target.value)}>
              {sets.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.collection_name}, {s.questions} questions)</option>)}
            </select>
          </label>
          <label>Run
            <select value={runId ?? ""} onChange={(e) => setRunId(e.target.value)}>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.kind}: {r.model_id ?? r.lane ?? "?"}, {r.retrieval_setting ?? "?"} ({r.status})
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      {summary && <SideBySide summary={summary} />}
      {detail && (
        <div className="eval-answers">
          {detail.answers.map((a) => <AnswerCard key={a.id} answer={a} onGrade={onGrade} />)}
        </div>
      )}
    </section>
  );
}
