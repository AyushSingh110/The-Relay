import { useEffect, useRef } from "react";

import { ChatPage } from "./pages/ChatPage";
import { CollectionsPage } from "./pages/CollectionsPage";
import { EvalReviewPage } from "./pages/EvalReviewPage";
import { StatusPage } from "./pages/StatusPage";
import { UploadPage } from "./pages/UploadPage";
import { Link, match, usePath } from "./router";

export function App() {
  const path = usePath();
  const route = match(path);
  const main = useRef<HTMLElement>(null);
  const first = useRef(true);
  useEffect(() => {
    // after a navigation, move focus to the new page (as a full page load would)
    if (first.current) first.current = false;
    else main.current?.focus();
  }, [path]);
  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      <header className="masthead">
        <Link to="/" className="wordmark">The Relay</Link>
        <nav aria-label="Main">
          <Link to="/" aria-current={route.page === "collections" ? "page" : undefined}>
            Collections
          </Link>
          <Link to="/eval" aria-current={route.page === "eval" ? "page" : undefined}>
            Evaluation
          </Link>
          <Link to="/status" aria-current={route.page === "status" ? "page" : undefined}>
            Status
          </Link>
        </nav>
      </header>
      <main id="main" ref={main} tabIndex={-1}>
        {route.page === "collections" && <CollectionsPage />}
        {route.page === "upload" && <UploadPage collectionId={route.collectionId} />}
        {route.page === "chat" && <ChatPage key={route.collectionId} collectionId={route.collectionId} />}
        {route.page === "status" && <StatusPage />}
        {route.page === "eval" && <EvalReviewPage />}
        {route.page === "missing" && (
          <section className="page">
            <h1>Page not found</h1>
            <p>There is nothing at {path}. <Link to="/">Go to your collections</Link>.</p>
          </section>
        )}
      </main>
    </>
  );
}
