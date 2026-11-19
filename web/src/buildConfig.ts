// Build-time settings (Vite replaces import.meta.env at build time; see web/.env.example).
// Until V2-10 the API accepts any Bearer key for the development tenant, so the default
// below is not a secret. A real key must never be baked into a public build.
export const API_KEY: string = import.meta.env.VITE_API_KEY || "relay-dev-key";
export const TENANT_ID: string = import.meta.env.VITE_TENANT_ID || "dev";
