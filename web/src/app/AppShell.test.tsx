import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "./router";

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("AppShell presentation", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/me") {
          return jsonResponse({
            username: "owner",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === "/api/v1/admin/runs") {
          return jsonResponse([]);
        }
        return jsonResponse({ error: "not_found" }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders a polished operations console shell", async () => {
    render(<TestApp initialPath="/" />);

    expect(await screen.findByText("Agent orchestration control plane")).not.toBeNull();
    expect(screen.getByText("Control console")).not.toBeNull();
    expect(screen.getByText("Live routing")).not.toBeNull();
    expect(screen.getByText("Guarded tools")).not.toBeNull();
    expect(screen.getByText("Hermes learning")).not.toBeNull();
  });
});
