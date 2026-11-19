import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createHttpApi } from "./api";
import { ApiProvider } from "./ApiContext";
import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) throw new Error("index.html has no #root element");
createRoot(root).render(
  <StrictMode>
    <ApiProvider api={createHttpApi()}>
      <App />
    </ApiProvider>
  </StrictMode>,
);
