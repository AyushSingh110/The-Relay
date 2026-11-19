import type { Segments } from "../answer";

const ms = (value: number) => (value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`);

export function TimeBar({ segments, cached }: { segments: Segments; cached: boolean }) {
  const total = segments.parts.reduce((sum, p) => sum + p.ms, 0);
  return (
    <figure className="timebar">
      <div className="timebar-track" aria-hidden="true">
        {total > 0 && segments.parts.filter((p) => p.ms > 0).map((p) => (
          <span key={p.label} className={`seg seg-${p.label.toLowerCase()}`}
            style={{ flexGrow: p.ms }} />
        ))}
      </div>
      <figcaption>
        {cached ? "Served from the cache: no queue, retrieval or model time. " : ""}
        {segments.parts.map((p, i) => (
          <span key={p.label} className="seg-label">
            <span className={`swatch seg-${p.label.toLowerCase()}`} aria-hidden="true" />
            {p.label} {ms(p.ms)}{i < segments.parts.length - 1 ? ", " : ""}
          </span>
        ))}
        {segments.estimated ? " (estimated from when each stage began)" : ""}
      </figcaption>
    </figure>
  );
}
