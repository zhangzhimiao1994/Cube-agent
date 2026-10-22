import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { api, type CurrentUser } from "../api/client";
import { AuthProvider, useAuth } from "./AuthProvider";

const current = { user_id: "11111111-1111-4111-8111-111111111111", username: "current-user",
  tenant_id: "22222222-2222-4222-8222-222222222222", role: "operator", permissions: ["runs:read"] } as CurrentUser;
function Probe() {
  const auth = useAuth();
  return <><span role="status">{auth.loading ? "loading" : auth.user?.username ?? "signed-out"}</span>
    <button onClick={() => void auth.login("test", "test-password")}>login</button>
    <button onClick={() => void auth.logout()}>logout</button></>;
}
afterEach(() => vi.restoreAllMocks());

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
