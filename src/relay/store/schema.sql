-- The Relay v2 metadata store, schema version 1 (docs/ARCHITECTURE_V2.md §5).
--
-- Applied once by relay.store.migrations, inside one transaction; the applied version is
-- recorded in _schema_version. Table and column names are part of the contract: later
-- migrations key on them. Timestamps are ISO-8601 UTC text. Foreign keys are enforced
-- (PRAGMA foreign_keys = ON is set on every connection).

CREATE TABLE tenant (
    id          TEXT PRIMARY KEY NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE api_key (
    id           TEXT PRIMARY KEY NOT NULL,
    tenant_id    TEXT NOT NULL REFERENCES tenant(id),
    key_hash     TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL,
    daily_limit  INTEGER
);

CREATE TABLE collection (
    id                TEXT PRIMARY KEY NOT NULL,
    tenant_id         TEXT NOT NULL REFERENCES tenant(id),
    name              TEXT NOT NULL,
    retrieval_preset  TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE TABLE document (
    id             TEXT PRIMARY KEY NOT NULL,
    collection_id  TEXT NOT NULL REFERENCES collection(id),
    source         TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    title          TEXT,
    pages          INTEGER,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE chunk (
    id           TEXT PRIMARY KEY NOT NULL,
    document_id  TEXT NOT NULL REFERENCES document(id),
    page_start   INTEGER NOT NULL,
    page_end     INTEGER NOT NULL,
    section      TEXT,
    text_hash    TEXT NOT NULL
);

CREATE TABLE ingest_job (
    id           TEXT PRIMARY KEY NOT NULL,
    document_id  TEXT NOT NULL REFERENCES document(id),
    state        TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error_class  TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE conversation (
    id             TEXT PRIMARY KEY NOT NULL,
    collection_id  TEXT NOT NULL REFERENCES collection(id),
    created_at     TEXT NOT NULL
);

CREATE TABLE message (
    id               TEXT PRIMARY KEY NOT NULL,
    conversation_id  TEXT NOT NULL REFERENCES conversation(id),
    role             TEXT NOT NULL,
    content          TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- No foreign keys on purpose: the log must accept a request even when the collection or
-- tenant is unknown or later deleted.
CREATE TABLE request_log (
    id             TEXT PRIMARY KEY NOT NULL,
    collection_id  TEXT,
    tenant_id      TEXT,
    question_hash  TEXT,
    status         TEXT NOT NULL,
    ttft_ms        REAL,
    total_ms       REAL,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cache_hit      INTEGER NOT NULL DEFAULT 0,
    coalesced      INTEGER NOT NULL DEFAULT 0,
    verdict        TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE eval_set (
    id             TEXT PRIMARY KEY NOT NULL,
    collection_id  TEXT NOT NULL REFERENCES collection(id),
    name           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE eval_question (
    id                TEXT PRIMARY KEY NOT NULL,
    eval_set_id       TEXT NOT NULL REFERENCES eval_set(id),
    question          TEXT NOT NULL,
    required_facts    TEXT,
    gold_citations    TEXT,
    forbidden_claims  TEXT,
    trap_type         TEXT
);

CREATE TABLE grade (
    id                TEXT PRIMARY KEY NOT NULL,
    eval_question_id  TEXT NOT NULL REFERENCES eval_question(id),
    run_id            TEXT,
    grader            TEXT NOT NULL,
    score             REAL,
    notes             TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX idx_api_key_tenant ON api_key(tenant_id);
CREATE INDEX idx_collection_tenant ON collection(tenant_id);
CREATE INDEX idx_document_collection ON document(collection_id);
CREATE INDEX idx_chunk_document ON chunk(document_id);
CREATE INDEX idx_ingest_job_document ON ingest_job(document_id);
CREATE INDEX idx_ingest_job_state ON ingest_job(state);
CREATE INDEX idx_conversation_collection ON conversation(collection_id);
CREATE INDEX idx_message_conversation ON message(conversation_id);
CREATE INDEX idx_request_log_created ON request_log(created_at);
CREATE INDEX idx_eval_set_collection ON eval_set(collection_id);
CREATE INDEX idx_eval_question_set ON eval_question(eval_set_id);
CREATE INDEX idx_grade_question ON grade(eval_question_id);
