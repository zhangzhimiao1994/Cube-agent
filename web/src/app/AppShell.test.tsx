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
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
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
