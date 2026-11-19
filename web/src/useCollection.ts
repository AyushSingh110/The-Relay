import { useEffect, useState } from "react";

import type { Collection } from "./api";
import { useApi } from "./ApiContext";

/** One collection of the caller's tenant, looked up in the collection list. */
export function useCollection(id: string): { data: Collection | null; error: string | null } {
  const api = useApi();
  const [state, setState] = useState<{ data: Collection | null; error: string | null }>({
    data: null, error: null,
  });
  useEffect(() => {
    let live = true;
    api.listCollections().then(
      (all) => {
        if (!live) return;
        const found = all.find((c) => c.id === id) ?? null;
        setState({ data: found, error: found ? null : "There is no collection with this ID." });
      },
      (error: Error) => live && setState({ data: null, error: error.message }),
    );
    return () => { live = false; };
  }, [api, id]);
  return state;
}
