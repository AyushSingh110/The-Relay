// Upload: add PDFs (file or arXiv ID) to a collection and follow their ingestion.
import { useEffect, useRef, useState, type DragEvent, type SyntheticEvent } from "react";

import type { Added, DocumentState, ExtractionQuality, IngestState } from "../api";
import { useApi } from "../ApiContext";
import { chatPath, Link } from "../router";
import { useCollection } from "../useCollection";

const STEPS: IngestState[] = ["queued", "parsing", "embedding", "indexing", "ready"];
const LABELS: Record<IngestState, string> = {
  queued: "Queued", parsing: "Parsing", embedding: "Embedding", indexing: "Indexing",
  ready: "Ready", failed: "Failed",
};
const ARXIV_ID = /^\d{4}\.\d{4,5}v[1-9]\d*$/;
const DEFAULT_POLL_MS = 2000;

type Source = { kind: "file"; file: File } | { kind: "arxiv"; id: string };

interface Item {
  key: number;
  label: string;
  source: Source;
  documentId: string | null;
  state: IngestState | "sending" | "rejected";
  errorClass: string | null;
  message: string | null; // why the upload itself was refused
  note: string | null;
  quality: ExtractionQuality | null;
  pollError: boolean;
}

const isPdf = (file: File) =>
  file.name.toLowerCase().endsWith(".pdf") && (file.type === "" || file.type === "application/pdf");

function stateOf(doc: DocumentState): IngestState {
  if (doc.job) return doc.job.state;
  return doc.status === "ready" ? "ready" : doc.status === "failed" ? "failed" : "queued";
}

function Progress({ item, collectionId, onRetry }: {
  item: Item; collectionId: string; onRetry: () => void;
}) {
  const card = useRef<HTMLElement>(null);
  const step = item.state === "sending" || item.state === "rejected" || item.state === "failed"
    ? -1 : STEPS.indexOf(item.state);
  return (
    <li>
      <section ref={card} tabIndex={-1} className={`progress progress-${item.state}`}
        role="status" aria-label={item.label}>
        <p className="progress-name">{item.label}</p>
        {item.state === "sending" && <p className="state">Sending…</p>}
        {step >= 0 && (
          <>
            <p className="state">
              Step {step + 1} of {STEPS.length}: <strong>{LABELS[item.state as IngestState]}</strong>
              {item.state !== "ready" ? " (in progress)" : ""}
            </p>
            <div className="steps" aria-hidden="true">
              {STEPS.map((s, i) => <span key={s} className={i <= step ? "done" : ""} />)}
            </div>
          </>
        )}
        {item.note && <p className="hint">{item.note}</p>}
        {item.pollError && <p className="hint">The latest progress could not be loaded.</p>}
        {item.state === "ready" && item.quality && (
          <p>
            Extracted {item.quality.total_pages} pages, {item.quality.empty_pages} empty.{" "}
            <Link to={chatPath(collectionId)}>Ask questions about it</Link>
          </p>
        )}
        {item.state === "ready" && item.quality?.suspected_scan && (
          <p className="notice notice-scan">
            <strong>This PDF may be a scan — answers may be incomplete</strong>
          </p>
        )}
        {(item.state === "failed" || item.state === "rejected") && (
          <div className="notice notice-lost">
            <p>
              <strong>{item.state === "failed" ? "Ingestion failed" : "Not added"}</strong>
              {item.errorClass ? `: ${item.errorClass}` : ""}
              {item.message ? `: ${item.message}` : ""}
            </p>
            <button type="button" className="danger"
              onClick={() => { card.current?.focus(); onRetry(); }}
              aria-label={`Retry upload of ${item.label}`}>Retry upload</button>
          </div>
        )}
      </section>
    </li>
  );
}

export function UploadPage({ collectionId, pollMs = DEFAULT_POLL_MS }: {
  collectionId: string; pollMs?: number;
}) {
  const api = useApi();
  const collection = useCollection(collectionId);
  const [items, setItems] = useState<Item[]>([]);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [arxivId, setArxivId] = useState("");
  const [arxivError, setArxivError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const nextKey = useRef(0);
  const polling = useRef(false);

  useEffect(() => {
    document.title = `Add documents | The Relay`;
  }, []);

  const update = (key: number, change: Partial<Item>) =>
    setItems((all) => all.map((item) => (item.key === key ? { ...item, ...change } : item)));

  const send = async (key: number, source: Source) => {
    update(key, { state: "sending", errorClass: null, message: null, note: null, quality: null });
    try {
      const added: Added = source.kind === "file"
        ? await api.uploadPdf(collectionId, source.file)
        : await api.addArxiv(collectionId, source.id);
      const state: IngestState = added.status === "ready" ? "ready"
        : added.status === "failed" ? "failed" : "queued";
      update(key, { documentId: added.id, state,
        note: added.deduplicated ? "This PDF is already in the collection." : null });
      if (state === "ready" || state === "failed") {
        // finished already (a duplicate): polling will not run, so fetch its report once
        try {
          const doc = await api.getDocument(added.id);
          update(key, { state: stateOf(doc), errorClass: doc.job?.error_class ?? null,
            quality: doc.extraction_quality });
        } catch {
          update(key, { pollError: true }); // it was added; only its report is missing
        }
      }
    } catch (error) {
      update(key, { state: "rejected", message: (error as Error).message });
    }
  };

  const add = (source: Source) => {
    const key = nextKey.current++;
    const label = source.kind === "file" ? source.file.name : `arXiv ${source.id}`;
    setItems((all) => [{ key, label, source, documentId: null, state: "sending", errorClass: null,
      message: null, note: null, quality: null, pollError: false }, ...all]);
    void send(key, source);
  };

  const addFiles = (files: FileList | File[]) => {
    const list = [...files];
    const rejected = list.filter((f) => !isPdf(f));
    setRefusal(rejected.length
      ? `Only PDF files can be added. Not added: ${rejected.map((f) => f.name).join(", ")}.`
      : null);
    list.filter(isPdf).forEach((file) => add({ kind: "file", file }));
  };

  const submitArxiv = (event: SyntheticEvent) => {
    event.preventDefault();
    const id = arxivId.trim();
    if (!ARXIV_ID.test(id)) {
      setArxivError("Use an exact arXiv version, like 2410.00037v1.");
      return;
    }
    setArxivError(null);
    setArxivId("");
    add({ kind: "arxiv", id });
  };

  // Poll every pending document every pollMs (one round at a time).
  const pending = items.filter((i) => i.documentId && !["ready", "failed", "rejected", "sending"]
    .includes(i.state)).map((i) => `${i.key}:${i.documentId}`).join(",");
  useEffect(() => {
    if (pending === "") return;
    const targets = pending.split(",").map((p) => {
      const [key, id] = p.split(":");
      return { key: Number(key), id: id! };
    });
    const timer = setInterval(async () => {
      if (polling.current) return;
      polling.current = true;
      try {
        await Promise.all(targets.map(async ({ key, id }) => {
          try {
            const doc = await api.getDocument(id);
            update(key, { state: stateOf(doc), errorClass: doc.job?.error_class ?? null,
              quality: doc.extraction_quality, pollError: false });
          } catch {
            update(key, { pollError: true });
          }
        }));
      } finally {
        polling.current = false;
      }
    }, pollMs);
    return () => clearInterval(timer);
  }, [api, pending, pollMs]);

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    addFiles(event.dataTransfer.files);
  };

  return (
    <section className="page">
      <header className="page-head">
        <h1>Add documents{collection.data ? ` to ${collection.data.name}` : ""}</h1>
        <p className="sub">
          PDFs are parsed, embedded and indexed in the background; the collection can answer
          from a document once it is ready. <Link to={chatPath(collectionId)}>Go to chat</Link>
        </p>
        {collection.error && <p role="alert" className="error">{collection.error}</p>}
      </header>

      {/* Dropping is a pointer shortcut; the keyboard path is the file input inside. */}
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div className={`dropzone${dragging ? " dragging" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)} onDrop={onDrop}>
        <p>Drop PDF files here, or</p>
        <input id="pdf-file" className="file-input" type="file" accept="application/pdf,.pdf"
          multiple onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
        <label htmlFor="pdf-file" className="button">Choose a PDF</label>
      </div>
      {refusal && <p role="alert" className="error">{refusal}</p>}

      <form className="arxiv" onSubmit={submitArxiv} noValidate>
        <label htmlFor="arxiv-id">arXiv ID</label>
        <div className="inline">
          <input id="arxiv-id" value={arxivId} placeholder="2410.00037v1" autoComplete="off"
            onChange={(e) => setArxivId(e.target.value)}
            aria-describedby={arxivError ? "arxiv-error" : undefined}
            aria-invalid={arxivError ? true : undefined} />
          <button type="submit">Add from arXiv</button>
        </div>
        {arxivError && <p id="arxiv-error" role="alert" className="error">{arxivError}</p>}
      </form>

      {items.length > 0 && (
        <>
          <h2>This session's documents</h2>
          <ul className="uploads">
            {items.map((item) => (
              <Progress key={item.key} item={item} collectionId={collectionId}
                onRetry={() => void send(item.key, item.source)} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
