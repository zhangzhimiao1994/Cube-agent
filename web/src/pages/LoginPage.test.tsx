import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const owner = {
  username: "owner",
  role: "super_admin",
  permissions: ["*"],
};

const runs = [
  {
    id: "22222222-2222-4222-8222-222222222222",
    status: "running",
    mode: "dispatch",
    queue_wait_ms: 120,
    capacity_wait_ms: 40,
    cost_usd: "0.0132",
  },
];

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/v1/me") {
          return jsonResponse({ error: "unauthenticated" }, { status: 401 });
        }
        if (path === "/api/v1/auth/login") {
          const body = JSON.parse(String(init?.body));
          if (body.username === "owner" && body.password === "correct horse battery staple") {
            return jsonResponse(owner);
          }
          return jsonResponse({ error: "invalid_credentials" }, { status: 401 });
        }
        if (path === "/api/v1/setup") {
          return jsonResponse(owner);
        }
        if (path === "/api/v1/admin/runs") {
          return jsonResponse(runs);
        }
        if (path === "/api/v1/auth/logout") {
          return new Response(null, { status: 204 });
        }
        return jsonResponse({ error: "not_found" }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("logs in and opens the run dashboard", async () => {
    render(<TestApp initialPath="/login" />);
    await userEvent.type(screen.getByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await userEvent.click(screen.getByRole("button", { name: "Login" }));

    expect(await screen.findByRole("heading", { name: "Run operations" })).not.toBeNull();
  });

  it("shows invalid credentials without storing browser tokens", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    render(<TestApp initialPath="/login" />);
    await userEvent.type(screen.getByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Login" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Invalid username or password",
    );
    expect(setItem).not.toHaveBeenCalled();
  });

  it("creates the first admin through setup", async () => {
    render(<TestApp initialPath="/setup" />);
    expect(await screen.findByText("Secure first-run setup")).not.toBeNull();
    expect(screen.getByText("Use the one-time code printed by the installer.")).not.toBeNull();
    expect(screen.getByText("Install packages")).not.toBeNull();
    expect(screen.getByText("Deploy release")).not.toBeNull();
    expect(screen.getByText("Run migrations")).not.toBeNull();
    expect(screen.getByText("Start services")).not.toBeNull();
    expect(screen.getByText("Create administrator account")).not.toBeNull();
    await userEvent.type(screen.getByLabelText("Setup code"), "setup-code");
    await userEvent.type(screen.getByLabelText("Username"), "owner");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await userEvent.click(screen.getByRole("button", { name: "Create admin" }));

    expect(await screen.findByRole("heading", { name: "Run operations" })).not.toBeNull();
  });

  it("protects the dashboard while session is missing", async () => {
    render(<TestApp initialPath="/" />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "Login" })).not.toBeNull());
  });

  it("shows navigation only for returned permissions", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/v1/me") {
          return jsonResponse({
            username: "viewer",
            role: "viewer",
            permissions: ["run:read", "config:read"],
          });
        }
        if (String(input) === "/api/v1/admin/runs") {
          return jsonResponse(runs);
        }
        return new Response(null, { status: 204 });
      }),
    );

    render(<TestApp initialPath="/" />);

    expect(await screen.findByRole("link", { name: "Runs" })).not.toBeNull();
    expect(screen.getByRole("link", { name: "Config" })).not.toBeNull();
    expect(screen.queryByRole("link", { name: "Skills" })).toBeNull();
  });
});
