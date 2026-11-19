// Status: what GET /healthz reports. Quota headroom arrives with V2-11.
import { useCallback, useEffect, useState } from "react";

import type { Health } from "../api";
import { useApi } from "../ApiContext";

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

  const load = useCallback(() => {
    api.health().then(
      (h) => { setHealth(h); setError(null); setCheckedAt(new Date()); },
      (failure: Error) => setError(failure.message),
    );
  }, [api]);

  useEffect(() => {
    document.title = "Status | The Relay";
    load();
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
              <Row name="Quota headroom" value="Not yet reported by the API (coming in V2-11)" />
            </tbody>
          </table>
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
