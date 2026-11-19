// An accessible dialog: focus moves in on open (to ``initialFocus`` or the first control),
// Tab stays inside, Escape closes, and focus returns to what had it before.
import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, ' +
  'summary, [tabindex]:not([tabindex="-1"])';

interface Props {
  title: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  modal?: boolean;
  initialFocus?: RefObject<HTMLElement | null>;
  closeLabel?: string; // shown as a close button beside the title
}

export function Dialog({
  title, onClose, children, className, modal = true, initialFocus, closeLabel,
}: Props) {
  const box = useRef<HTMLDivElement>(null);
  const heading = useId();

  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    // a modal form starts at its first control; a reading panel starts at its top
    const target = initialFocus?.current ??
      (modal ? box.current?.querySelector<HTMLElement>(FOCUSABLE) : null);
    (target ?? box.current)?.focus();
    return () => before?.focus?.();
  }, [initialFocus, modal]);

  useEffect(() => {
    const element = box.current;
    if (element === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = [...element.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) return;
      const first = items[0]!;
      const last = items[items.length - 1]!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    element.addEventListener("keydown", onKeyDown);
    return () => element.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className={modal ? "backdrop" : undefined}>
      <div ref={box} role="dialog" aria-modal={modal} aria-labelledby={heading} tabIndex={-1}
        className={className ?? "dialog"}>
        <div className="dialog-head">
          <h2 id={heading}>{title}</h2>
          {closeLabel && <button type="button" className="quiet" onClick={onClose}>{closeLabel}</button>}
        </div>
        {children}
      </div>
    </div>
  );
}
