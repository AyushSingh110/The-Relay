// A small history-API router: four routes do not need a routing library.
import { useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from "react";

const CHANGE = "relay:navigate";

export function navigate(to: string): void {
  window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(CHANGE));
}

export function usePath(): string {
  const [path, setPath] = useState(() => window.location.pathname);
  useEffect(() => {
    const update = () => setPath(window.location.pathname);
    window.addEventListener("popstate", update);
    window.addEventListener(CHANGE, update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener(CHANGE, update);
    };
  }, []);
  return path;
}

export type Route =
  | { page: "collections" }
  | { page: "upload"; collectionId: string }
  | { page: "chat"; collectionId: string }
  | { page: "status" }
  | { page: "missing" };

export function match(path: string): Route {
  const trimmed = path.replace(/\/+$/, "") || "/";
  if (trimmed === "/") return { page: "collections" };
  if (trimmed === "/status") return { page: "status" };
  const found = /^\/collections\/([^/]+)\/(upload|chat)$/.exec(trimmed);
  if (found) {
    let collectionId: string;
    try {
      collectionId = decodeURIComponent(found[1]!);
    } catch {
      return { page: "missing" }; // a malformed escape such as "%E0"
    }
    return found[2] === "upload" ? { page: "upload", collectionId } : { page: "chat", collectionId };
  }
  return { page: "missing" };
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { to: string };

export function Link({ to, onClick, children, ...rest }: LinkProps) {
  const go = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey) return; // let the browser open a new tab
    event.preventDefault();
    navigate(to);
  };
  return <a href={to} onClick={go} {...rest}>{children}</a>;
}

export const chatPath = (id: string) => `/collections/${encodeURIComponent(id)}/chat`;
export const uploadPath = (id: string) => `/collections/${encodeURIComponent(id)}/upload`;
