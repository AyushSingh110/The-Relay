// Status: what GET /healthz reports, refreshed every few seconds (V2-11: live quota).
import { useCallback, useEffect, useState } from "react";

import type { Health, QuotaLane, WhoAmI } from "../api";
import { useApi } from "../ApiContext";

const REFRESH_MS = 5000;

const count = (n: number) => n.toLocaleString("en-US");

function tokensText(q: QuotaLane): string {
  if (q.tpm_available === null) return "No token limit known";
  const left = Math.max(0, q.tpm_available);
  return q.tpm_limit === null ? `${count(left)} tokens left this minute`
    : `${count(left)} of ${count(q.tpm_limit)} tokens this minute`;
}

function tokensTone(q: QuotaLane): "ok" | "warn" | "bad" | undefined {
  if (q.daily_stop || q.circuit === "open") return "bad";
  if (q.tpm_available === null || q.tpm_limit === null) return undefined;
  return q.tpm_available < q.tpm_limit * 0.2 ? "warn" : "ok";
}

function Row({ name, value, tone }: { name: string; value: string; tone?: "ok" | "warn" | "bad" }) {
  return (
    <tr>
      <th scope="row">{name}</th>
      <td>{tone ? <span className={`tag tag-${tone}`}>{value}</span> : value}</td>
    </tr>
  );
}

export function StatusPage() {
  const api = useApi();
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [whoError, setWhoError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.health().then(
      (h) => { setHealth(h); setError(null); setCheckedAt(new Date()); },
      (failure: Error) => setError(failure.message),
    );
    api.whoami().then(
      (w) => { setWho(w); setWhoError(null); },
      (failure: Error) => setWhoError(failure.message),
    );
  }, [api]);

  useEffect(() => {
    document.title = "Status | The Relay";
    load();
    const timer = window.setInterval(load, REFRESH_MS); // live: queue and quota move fast
    return () => window.clearInterval(timer);
  }, [load]);

  const log = health?.trace_sink;
  return (
    <section className="page">
      <header className="page-head with-action">
        <div>
          <h1>Status</h1>
          <p className="sub">
            {checkedAt ? `Checked at ${checkedAt.toLocaleTimeString()}.` : "Checking the service…"}
          </p>
        </div>
        <button type="button" className="quiet" onClick={load}>Check again</button>
      </header>
      {error && <p role="alert" className="error">The service did not answer: {error}</p>}
      {(who || whoError) && (
        <div className="status-grid access-grid">
        <table aria-label="Access">
          <caption>Access</caption>
          <tbody>
            {who ? (
              <>
                <Row name="Tenant" value={who.tenant_name ? `${who.tenant_name} (${who.tenant_id})`
                  : who.tenant_id} />
                <Row name="API key" value={who.dev_key
                  ? "Development key, for local use only" : who.key}
                  tone={who.dev_key ? "warn" : "ok"} />
                <Row name="Role" value={who.role} />
                <Row name="Daily limit" value={who.daily_limit === null ? "None"
                  : `${who.daily_limit} questions a day`} />
              </>
            ) : (
              <Row name="API key" value={`not accepted: ${whoError}`} tone="bad" />
            )}
          </tbody>
        </table>
        </div>
      )}
      {health && (
        <div className="status-grid">
          <table aria-label="Service">
            <caption>Service</caption>
            <tbody>
              <Row name="Health" value={health.status === "ok" ? "OK" : health.status}
                tone={health.status === "ok" ? "ok" : "bad"} />
              <Row name="Cache mode" value={health.cache_mode} />
              <Row name="Metadata store" value={log ? "SQLite, request log writing"
                : "Not in use (cache mode is not sqlite)"} tone={log ? "ok" : "warn"} />
              <Row name="Cache entries" value={String(health.cache_entries)} />
            </tbody>
          </table>
          <table aria-label="Queue">
            <caption>Queue</caption>
            <tbody>
              <Row name="Waiting" value={String(health.queue_depth)} />
              <Row name="Running" value={`${health.queue_active}`} />
              <Row name="Capacity" value={String(health.queue_capacity)} />
              <Row name="Shared in flight" value={String(health.in_flight)} />
            </tbody>
          </table>
          <table aria-label="Model provider">
            <caption>Model provider</caption>
            <tbody>
              <Row name="Circuit" value={health.circuit ?? "not reported"}
                tone={health.circuit === "closed" ? "ok" : health.circuit ? "bad" : undefined} />
              <Row name="Daily stop"
                value={health.daily_stop === null ? "Not reported"
                  : health.daily_stop ? "Reached" : "Not reached"}
                tone={health.daily_stop === null ? undefined : health.daily_stop ? "bad" : "ok"} />
            </tbody>
          </table>
          {health.quota && health.quota.length > 0 && (
            <table aria-label="Quota headroom">
              <caption>Quota headroom</caption>
              <tbody>
                {health.quota.map((q) => (
                  <tr key={q.lane}>
                    <th scope="row">{q.lane}</th>
                    <td>
                      <span className={`tag tag-${tokensTone(q) ?? "plain"}`}>{tokensText(q)}</span>
                      <span className="quota-detail">
                        {q.day_requests === 1 ? "1 request today" : `${count(q.day_requests)} requests today`}
                        {q.rpd_limit !== null ? ` of ${count(q.rpd_limit)}` : ""}
                        {q.daily_stop ? ", daily stop reached" : ""}
                        {q.circuit !== "closed" ? `, circuit ${q.circuit}` : ""}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <table aria-label="Request log">
            <caption>Request log</caption>
            <tbody>
              {log ? (
                <>
                  <Row name="Written" value={String(log.written)} />
                  <Row name="Failed" value={String(log.failed)} tone={log.failed ? "warn" : undefined} />
                  <Row name="Dropped" value={String(log.dropped)} tone={log.dropped ? "bad" : undefined} />
                  <Row name="Pending" value={String(log.pending)} />
                </>
              ) : (
                <Row name="Request log" value="Not reported in this cache mode" />
              )}
              <Row name="Trace failures" value={String(health.trace_failures)} />
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
