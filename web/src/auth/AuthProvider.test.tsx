import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError, api, type CurrentUser } from "../api/client";
import { AuthProvider, RequireAuth, useAuth } from "./AuthProvider";

const current = { user_id: "11111111-1111-4111-8111-111111111111", username: "current-user",
  tenant_id: "22222222-2222-4222-8222-222222222222", role: "operator", permissions: ["runs:read"] } as CurrentUser;
function Probe() {
  const auth = useAuth();
  return <><span role="status">{auth.loading ? "loading" : auth.user?.username ?? "signed-out"}</span>
    <button onClick={() => void auth.login("test", "test-password")}>login</button>
    <button onClick={() => void auth.logout()}>logout</button></>;
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each([503, "offline"])("retains authorized cached content on transient read failure: %s", async (failure) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.spyOn(api, "me").mockResolvedValue(current);
  const read = vi.fn().mockResolvedValue("AUTHORIZED_CACHED_RESULT");
  function Content() {
    const query = useQuery({ queryKey: ["authorized-record"], queryFn: read });
    return <><p>{query.data}</p>{query.isError && <p role="alert">Temporary read failure</p>}</>;
  }
  const view = render(<QueryClientProvider client={client}><MemoryRouter><AuthProvider>
    <RequireAuth><Content /></RequireAuth>
  </AuthProvider></MemoryRouter></QueryClientProvider>);
  try {
    await screen.findByText("AUTHORIZED_CACHED_RESULT");
    read.mockRejectedValue(failure === 503 ? new ApiError("Unavailable", 503) : new Error("offline"));
    await act(async () => { await client.refetchQueries({ queryKey: ["authorized-record"] }); });
    await screen.findByRole("alert");
    expect(screen.getByText("AUTHORIZED_CACHED_RESULT")).toBeTruthy();
    expect(client.getQueryData(["authorized-record"])).toBe("AUTHORIZED_CACHED_RESULT");
  } finally { view.unmount(); client.clear(); }
});

it.each(["failure", "old-user"])("ignores stale startup identity after explicit login: %s", async (outcome) => {
  let resolve!: (value: CurrentUser) => void, reject!: (error: Error) => void;
  vi.spyOn(api, "me").mockReturnValue(new Promise<CurrentUser>((yes, no) => { resolve = yes; reject = no; }));
  vi.spyOn(api, "login").mockResolvedValue(current);
  render(<AuthProvider><Probe /></AuthProvider>);
  await userEvent.click(screen.getByText("login", { exact: true }));
  expect(screen.getByRole("status").textContent).toBe("current-user");
  await act(async () => {
    if (outcome === "failure") reject(new Error("stale unauthenticated request"));
    else resolve({ ...current, username: "old-user" });
  });
  expect(screen.getByRole("status").textContent).toBe("current-user");
});

it("does not restore startup identity after logout", async () => {
  let resolve!: (value: CurrentUser) => void;
  vi.spyOn(api, "me").mockReturnValue(new Promise<CurrentUser>(yes => { resolve = yes; }));
  vi.spyOn(api, "logout").mockResolvedValue();
  render(<AuthProvider><Probe /></AuthProvider>);
  await userEvent.click(screen.getByText("logout", { exact: true }));
  await act(async () => resolve(current));
  expect(screen.getByRole("status").textContent).toBe("signed-out");
});

it("remounts protected state for an immediately resolved identity switch and isolates late mutation callbacks", async () => {
  const original = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let auth!: ReturnType<typeof useAuth>;
  let activeClient = original;
  let finishOld!: (value: string) => void;
  const oldMutation = new Promise<string>((resolve) => { finishOld = resolve; });
  let finishNew!: (value: string) => void;
  const newRead = new Promise<string>((resolve) => { finishNew = resolve; });
  const next = { ...current, user_id: "new-actor", username: "new-user" };
  vi.spyOn(api, "me").mockResolvedValue(current);
  vi.spyOn(api, "login").mockResolvedValue(next);
  const read = vi.fn((id: string) => id === current.user_id ? Promise.resolve("A_PRIVATE_RESULT") : newRead);
  function Controls() {
    auth = useAuth();
    activeClient = useQueryClient();
    return null;
  }
  function PrivatePage() {
    const { user } = useAuth();
    const client = useQueryClient();
    const [draft] = useState(() => user?.user_id === current.user_id ? "A_PRIVATE_DRAFT" : "B_DRAFT");
    const query = useQuery({ queryKey: ["private-records"], queryFn: () => read(user!.user_id) });
    const mutation = useMutation({ mutationFn: () => oldMutation,
      onSuccess: (value) => client.setQueryData(["private-records"], value) });
    return <><p>{draft}</p><p>{query.data}</p><button onClick={() => mutation.mutate()}>Start old mutation</button></>;
  }
  const view = render(<QueryClientProvider client={original}><MemoryRouter><AuthProvider><Controls />
    <RequireAuth><PrivatePage /></RequireAuth>
  </AuthProvider></MemoryRouter></QueryClientProvider>);
  try {
    await screen.findByText("A_PRIVATE_RESULT");
    await userEvent.click(screen.getByText("Start old mutation"));
    await act(async () => auth.login("b", "fixture"));
    expect(document.body.textContent).not.toContain("A_PRIVATE_");
    await waitFor(() => expect(read).toHaveBeenCalledWith(next.user_id));
    await act(async () => { finishNew("B_RESULT"); finishOld("A_PRIVATE_LATE_RESULT"); });
    await screen.findByText("B_RESULT");
    expect(screen.getByText("B_DRAFT")).toBeTruthy();
    expect(document.body.textContent).not.toContain("A_PRIVATE_");
    expect(activeClient).not.toBe(original);
    expect(activeClient.getQueryData(["private-records"])).toBe("B_RESULT");
  } finally { view.unmount(); original.clear(); activeClient.clear(); }
});
