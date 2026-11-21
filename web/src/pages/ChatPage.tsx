// Chat: questions to one collection over POST /v1/ask, rendered as the events arrive.
import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { ApiError } from "../api";
import { initialAnswer, reduce, type Action, type Answer } from "../answer";
import { useApi } from "../ApiContext";
import { AnswerView } from "../components/AnswerView";
import { SourcePanel } from "../components/SourcePanel";
import { useCollection } from "../useCollection";
import { Link, uploadPath } from "../router";

interface Open {
  turn: number;
  passageId: string;
  sentence: string | null;
}

export function ChatPage({ collectionId }: { collectionId: string }) {
  const api = useApi();
  const collection = useCollection(collectionId);
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Answer[]>([]);
  const [open, setOpen] = useState<Open | null>(null);
  const running = useRef(new Map<number, AbortController>());
  const [busy, setBusy] = useState(false);
  const focusTurn = useRef<number | null>(null);
  const questionBox = useRef<HTMLTextAreaElement>(null);
  // one conversation per page visit (V2-08): follow-ups are read in its context
  const conversation = useRef<string | null>(null);

  useEffect(() => {
    // after a retry the button that was pressed is gone: keep the user on that answer
    if (focusTurn.current === null) return;
    window.document.getElementById(`turn-${focusTurn.current}`)?.focus();
    focusTurn.current = null;
  }, [turns]);

  useEffect(() => {
    document.title = `${collection.data?.name ?? "Chat"} | The Relay`;
  }, [collection.data?.name]);

  useEffect(() => {
    const controllers = running.current;
    return () => controllers.forEach((c) => c.abort()); // leaving the page stops its answers
  }, []);

  const dispatch = useCallback((index: number, action: Action) => {
    setTurns((all) => all.map((turn, i) => (i === index ? reduce(turn, action) : turn)));
  }, []);

  const conversationId = useCallback(async (): Promise<string | null> => {
    if (conversation.current === null) {
      try {
        conversation.current = (await api.createConversation(collectionId)).id;
      } catch {
        return null; // asked without context; the next question tries again
      }
    }
    return conversation.current;
  }, [api, collectionId]);

  const run = useCallback(async (text: string, index: number) => {
    const controller = new AbortController();
    running.current.set(index, controller);
    setBusy(true);
    try {
      const id = await conversationId();
      await api.ask(id === null ? { question: text, collection_id: collectionId }
        : { question: text, collection_id: collectionId, conversation_id: id },
        (event) => dispatch(index, { type: "event", event, at: performance.now() }),
        controller.signal);
      dispatch(index, { type: "ended", at: performance.now() });
    } catch (error) {
      if (controller.signal.aborted) dispatch(index, { type: "cancelled" });
      else if (error instanceof ApiError) {
        if (error.code === "conversation_not_found") conversation.current = null; // start anew
        dispatch(index, { type: "rejected",
          error: { status: error.status, code: error.code, message: error.message } });
      } else dispatch(index, { type: "lost", at: performance.now() });
    } finally {
      running.current.delete(index);
      setBusy(running.current.size > 0);
    }
  }, [api, collectionId, conversationId, dispatch]);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const text = question.trim();
    if (text === "" || busy) return;
    const index = turns.length;
    setTurns((all) => [...all, initialAnswer(text, performance.now())]);
    setQuestion("");
    void run(text, index);
  };

  const retry = (index: number) => {
    const text = turns[index]?.question;
    if (text === undefined || busy) return;
    setTurns((all) => all.map((t, i) => (i === index ? initialAnswer(text, performance.now()) : t)));
    focusTurn.current = index;
    void run(text, index);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  };

  const shown = open === null ? undefined : turns[open.turn];
  return (
    <section className="page chat">
      <header className="page-head">
        <h1>{collection.data?.name ?? "Chat"}</h1>
        <p className="sub">
          Answers use only this collection's documents. Every sentence shown passed the
          citation check. <Link to={uploadPath(collectionId)}>Add documents</Link>
        </p>
        {collection.error && <p role="alert" className="error">{collection.error}</p>}
      </header>

      <div className="turns">
        {turns.length === 0 && (
          <p className="empty">Ask a question about the documents in this collection.</p>
        )}
        {turns.map((turn, index) => (
          <AnswerView key={index} id={`turn-${index}`} answer={turn} retryBlocked={busy}
            onRetry={() => retry(index)}
            onOpenSource={(passageId, sentence) => setOpen({ turn: index, passageId, sentence })} />
        ))}
      </div>

      <form className="ask" onSubmit={submit}>
        <label htmlFor="question">Your question</label>
        <textarea id="question" ref={questionBox} value={question} rows={3} maxLength={4000}
          onChange={(e) => setQuestion(e.target.value)} onKeyDown={onKeyDown}
          aria-describedby="question-hint" />
        <p id="question-hint" className="hint">Enter sends; Shift+Enter starts a new line.</p>
        <div className="actions">
          <button type="submit" disabled={busy || question.trim() === ""}>Ask</button>
          {busy && (
            <button type="button" className="quiet"
              onClick={() => {
                running.current.forEach((c) => c.abort());
                questionBox.current?.focus(); // the Stop button disappears
              }}>
              Stop answering
            </button>
          )}
        </div>
      </form>

      {open && shown && (
        <SourcePanel passageId={open.passageId} sentence={open.sentence}
          passage={shown.sources.find((p) => p.id === open.passageId)}
          onClose={() => setOpen(null)} />
      )}
    </section>
  );
}
