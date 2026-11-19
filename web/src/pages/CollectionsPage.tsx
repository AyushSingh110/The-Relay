// Collections: the dev tenant's collections, and a dialog to create one.
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError, type Collection, type Preset } from "../api";
import { useApi } from "../ApiContext";
import { Dialog } from "../components/Dialog";
import { chatPath, Link, uploadPath } from "../router";

const PRESETS: { value: Preset; label: string }[] = [
  { value: "small_fast", label: "small_fast (4 short passages, fastest)" },
  { value: "large_wide", label: "large_wide (5 long passages)" },
  { value: "balanced_rerank", label: "rerank (balanced_rerank: 12 candidates, reranked)" },
];

function NewCollection({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const api = useApi();
  const [name, setName] = useState("");
  const [preset, setPreset] = useState<Preset>("small_fast");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const nameField = useRef<HTMLInputElement>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (name.trim() === "") {
      setError("Give the collection a name.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.createCollection({ name: name.trim(), retrieval_preset: preset });
      onCreated();
    } catch (failure) {
      setError(failure instanceof ApiError && failure.code === "collection_exists"
        ? "You already have a collection with this name."
        : (failure as Error).message);
      setSaving(false);
    }
  };

  return (
    <Dialog title="New collection" onClose={onClose} initialFocus={nameField}>
      <form onSubmit={submit} className="form">
        <label htmlFor="collection-name">Name</label>
        <input id="collection-name" ref={nameField} value={name} maxLength={128} required
          onChange={(e) => setName(e.target.value)} autoComplete="off" />
        <label htmlFor="collection-preset">Retrieval preset</label>
        <select id="collection-preset" value={preset}
          onChange={(e) => setPreset(e.target.value as Preset)}>
          {PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
        </select>
        {error && <p role="alert" className="error">{error}</p>}
        <div className="actions">
          <button type="submit" disabled={saving}>Create collection</button>
          <button type="button" className="quiet" onClick={onClose}>Cancel</button>
        </div>
      </form>
    </Dialog>
  );
}

function Card({ collection }: { collection: Collection }) {
  const heading = `collection-${collection.id}`;
  const docs = collection.document_count;
  return (
    <li>
      <article className="card" aria-labelledby={heading}>
        <h2 id={heading}><Link to={chatPath(collection.id)}>{collection.name}</Link></h2>
        <dl className="facts">
          <div><dt>Documents</dt><dd>{docs === 1 ? "1 document" : `${docs} documents`}</dd></div>
          <div><dt>Index</dt><dd>Version {collection.version}</dd></div>
          <div><dt>Retrieval</dt><dd><code>{collection.retrieval_preset}</code>
            {collection.rerank ? " with reranking" : ""}</dd></div>
        </dl>
        <p className="card-links">
          <Link to={uploadPath(collection.id)}>Add documents</Link>
        </p>
      </article>
    </li>
  );
}

export function CollectionsPage() {
  const api = useApi();
  const [collections, setCollections] = useState<Collection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(() => {
    api.listCollections().then(
      (all) => { setCollections(all); setError(null); },
      (failure: Error) => setError(failure.message),
    );
  }, [api]);

  useEffect(() => {
    document.title = "Collections | The Relay";
    load();
  }, [load]);

  return (
    <section className="page">
      <header className="page-head with-action">
        <div>
          <h1>Collections</h1>
          <p className="sub">Each collection is a set of PDFs that answers are drawn from.</p>
        </div>
        <button type="button" onClick={() => setCreating(true)}>New collection</button>
      </header>
      {error && (
        <div role="alert" className="error">
          <p>The collections could not be loaded: {error}</p>
          <button type="button" className="quiet" onClick={load}>Try again</button>
        </div>
      )}
      {collections === null && !error && <p role="status">Loading collections…</p>}
      {collections?.length === 0 && (
        <p className="empty">No collections yet. Create one, then add PDFs to it.</p>
      )}
      {collections && collections.length > 0 && (
        <ul className="cards">{collections.map((c) => <Card key={c.id} collection={c} />)}</ul>
      )}
      {creating && (
        <NewCollection onClose={() => setCreating(false)}
          onCreated={() => { setCreating(false); load(); }} />
      )}
    </section>
  );
}
