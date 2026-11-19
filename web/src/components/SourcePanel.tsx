import type { Passage } from "../api";
import { chipLabel, highlight } from "../answer";
import { Dialog } from "./Dialog";

interface Props {
  passageId: string;
  passage: Passage | undefined;
  sentence: string | null; // the sentence whose chip opened the panel
  onClose: () => void;
}

export function SourcePanel({ passageId, passage, sentence, onClose }: Props) {
  return (
    <Dialog title={`Source ${passageId}`} onClose={onClose} className="sheet" modal={false}
      closeLabel="Close source">
      {passage === undefined ? (
        <p>This passage was not sent with the answer, so it cannot be shown.</p>
      ) : (
        <>
          <p className="source-name">{chipLabel(passage)}</p>
          <dl className="facts">
            <div><dt>Paper</dt><dd>
              {passage.short_name ? `${passage.short_name} (${passage.paper_id})` : passage.paper_id}
            </dd></div>
            <div><dt>Location</dt><dd>pages {passage.page_range}</dd></div>
            {passage.section && <div><dt>Section</dt><dd>{passage.section}</dd></div>}
          </dl>
          {passage.text ? (
            <blockquote className="passage">
              {(sentence ? highlight(passage.text, sentence) : [{ text: passage.text, match: false }])
                .map((part, i) => (part.match ? <mark key={i}>{part.text}</mark> : <span key={i}>{part.text}</span>))}
            </blockquote>
          ) : (
            <p>The passage text is not kept with cached answers; it is on the pages above.</p>
          )}
        </>
      )}
    </Dialog>
  );
}
