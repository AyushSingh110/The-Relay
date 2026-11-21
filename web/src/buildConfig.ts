// Build-time settings (Vite replaces import.meta.env at build time; see web/.env.example).
// The default is the development key, accepted only by a server listening on loopback
// (V2-10); a real key comes from `relay keys create`. A real key must never be baked into a
// public build.
export const API_KEY: string = import.meta.env.VITE_API_KEY || "relay-dev-key";
export const TENANT_ID: string = import.meta.env.VITE_TENANT_ID || "dev";
