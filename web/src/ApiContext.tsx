import { createContext, useContext, type ReactNode } from "react";

import type { RelayApi } from "./api";

const ApiContext = createContext<RelayApi | null>(null);

export function ApiProvider({ api, children }: { api: RelayApi; children: ReactNode }) {
  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>;
}

export function useApi(): RelayApi {
  const api = useContext(ApiContext);
  if (api === null) throw new Error("useApi needs an <ApiProvider>");
  return api;
}
