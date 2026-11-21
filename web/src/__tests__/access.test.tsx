// V2-10: the Status page says which tenant and which API key the UI is using.
import { render, screen, within } from "@testing-library/react";

import { ApiError } from "../api";
import { ApiProvider } from "../ApiContext";
import { StatusPage } from "../pages/StatusPage";
import { fakeApi } from "../test/fakeApi";

test("the status page names the tenant, the key and its role", async () => {
  const api = fakeApi({
    whoami: jest.fn(async () => ({ tenant_id: "acme", tenant_name: "Acme", key_id: "k1",
      role: "member", dev_key: false, key: "rel_live_… (id k1)", daily_limit: 1000 })),
  });
  render(<ApiProvider api={api}><StatusPage /></ApiProvider>);
  const access = await screen.findByRole("table", { name: "Access" });
  expect(within(access).getByText("Acme (acme)")).toBeInTheDocument();
  expect(within(access).getByText("rel_live_… (id k1)")).toBeInTheDocument();
  expect(within(access).getByText("member")).toBeInTheDocument();
  expect(within(access).getByText("1000 questions a day")).toBeInTheDocument();
});

test("the development key is called out as local only", async () => {
  render(<ApiProvider api={fakeApi()}><StatusPage /></ApiProvider>);
  const access = await screen.findByRole("table", { name: "Access" });
  expect(within(access).getByText(/Development key, for local use only/)).toBeInTheDocument();
});

test("a key the server rejects is reported, not hidden", async () => {
  const api = fakeApi({
    whoami: jest.fn(async () => { throw new ApiError(401, "unauthorized", "the API key is not valid"); }),
  });
  render(<ApiProvider api={api}><StatusPage /></ApiProvider>);
  const access = await screen.findByRole("table", { name: "Access" });
  expect(within(access).getByText(/not accepted: the API key is not valid/)).toBeInTheDocument();
});
