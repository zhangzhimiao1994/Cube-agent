import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const runId = "22222222-2222-4222-8222-222222222222";

const runListItem = {
  id: runId,
  status: "running",
  mode: "dispatch",
  queue_wait_ms: 120,
  capacity_wait_ms: 40,
  cost_usd: "0.0132",
};

const runDetail = {
  ...runListItem,
  request: "Summarize current deployment readiness.",
  events: [
    {
      sequence: 1,
      kind: "queued",
      message: "Run accepted and queued.",
      created_at: "2026-08-07T00:00:00Z",
    },
  ],
  artifacts: [{ id: "artifact-1", kind: "markdown", title: "Readiness report" }],
  explicit_details: { routing: "dispatch mode selected explicitly" },
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("operational management pages", () => {
  beforeEach(() => {
    let skillStatus: "missing" | "quarantined" | "enabled" = "missing";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/admin/runs") {
          return jsonResponse([runListItem]);
        }
        if (path === `/api/v1/admin/runs/${runId}`) {
          return jsonResponse(runDetail);
        }
        if (path === `/api/v1/admin/runs/${runId}/pause`) {
          return jsonResponse({ ...runDetail, status: "paused" });
        }
        if (path === "/api/v1/admin/skills" && init?.method === "GET") {
          return jsonResponse(
            skillStatus === "missing"
              ? []
              : [
                  {
                    id: "safe-skill",
                    name: "safe-skill",
                    status: skillStatus,
                    scan_diff: ["added SKILL.md"],
                    requested_permissions: ["filesystem:read"],
                  },
                ],
          );
        }
        if (path === "/api/v1/admin/skills" && init?.method === "POST") {
          skillStatus = "quarantined";
          return jsonResponse({
            id: "safe-skill",
            name: "safe-skill",
            status: "quarantined",
            scan_diff: ["added SKILL.md"],
            requested_permissions: ["filesystem:read"],
          });
        }
        if (path === "/api/v1/admin/skills/safe-skill/approve") {
          skillStatus = "enabled";
          return jsonResponse({
            id: "safe-skill",
            name: "safe-skill",
            status: "enabled",
            scan_diff: ["added SKILL.md"],
            requested_permissions: ["filesystem:read"],
          });
        }
        if (path === "/api/v1/admin/mcp") {
          return jsonResponse([
            {
              id: "filesystem",
              name: "Filesystem MCP",
              health: "healthy",
              allowed_tools: ["read_file"],
            },
          ]);
        }
        if (path === "/api/v1/admin/memory") {
          return jsonResponse([
            {
              id: "project-policy",
              scope: "tenant",
              value: "Only non-dangerous operations may run without approval.",
            },
          ]);
        }
        if (path === "/api/v1/admin/memory/project-policy") {
          return jsonResponse({
            id: "project-policy",
            scope: "tenant",
            value: "Updated policy.",
          });
        }
        if (path.startsWith("/api/v1/admin/audit")) {
          return jsonResponse([
            {
              id: "audit-1",
              actor: "system",
              action: "config.publish",
              resource: "configuration",
              created_at: "2026-08-07T00:00:00Z",
            },
          ]);
        }
        if (path === "/api/v1/admin/hermes" && init?.method === "GET") {
          return jsonResponse([
            {
              id: "hermes-1",
              outcome: "success",
              lesson: "Use dispatch mode when the request has clear deliverables.",
              tags: ["dispatch"],
              weight: 3,
              created_at: "2026-08-07T00:00:00Z",
            },
          ]);
        }
        if (path === "/api/v1/admin/hermes/recommend") {
          return jsonResponse({
            recommended_mode: "group_chat",
            recommended_model: "deepseek-chat",
            recommended_skills: ["architecture-review"],
            confidence: 0.7,
            reasons: ["Matched prior Hermes lesson."],
            requires_approval: false,
          });
        }
        if (path === "/api/v1/admin/hermes/feedback") {
          return jsonResponse({
            id: "hermes-2",
            outcome: "success",
            lesson: "Use group chat when debate review is required.",
            tags: ["debate", "review"],
            weight: 5,
            created_at: "2026-08-07T00:00:00Z",
          });
        }
        return jsonResponse({ error: "not_found" }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows run operations and supports pause control", async () => {
    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByText("Status: running")).not.toBeNull();
    expect(screen.getByText(/Readiness report/)).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));

    await waitFor(() => expect(screen.getByText("Status: paused")).not.toBeNull());
  });

  it("uploads and approves a skill", async () => {
    render(<TestApp initialPath="/skills" />);

    await screen.findByRole("heading", { name: "Skills governance" });
    const file = new File(["safe"], "safe-skill.zip", { type: "application/zip" });
    await userEvent.upload(screen.getByLabelText("Skill ZIP"), file);
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));

    expect(await screen.findByText("Status: quarantined")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Approve and enable" }));
    expect(await screen.findByText("Status: enabled")).not.toBeNull();
  });

  it("shows MCP, memory, and audit governance pages", async () => {
    render(<TestApp initialPath="/mcp" />);
    expect(await screen.findByText("Health: healthy")).not.toBeNull();

    render(<TestApp initialPath="/memory" />);
    expect(await screen.findByText("Scope: tenant")).not.toBeNull();

    render(<TestApp initialPath="/audit" />);
    expect(await screen.findByText("config.publish")).not.toBeNull();
    expect(screen.queryByText(/api_key|hidden_reasoning|fingerprint/i)).toBeNull();
  });

  it("shows Hermes recommendations and records safe feedback", async () => {
    render(<TestApp initialPath="/hermes" />);

    expect(await screen.findByRole("heading", { name: "Hermes learning" })).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Ask Hermes" }));
    expect(await screen.findByText("Mode: group_chat")).not.toBeNull();
    expect(screen.getByText("Model: deepseek-chat")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Record feedback" }));
    expect(await screen.findByText("Use dispatch mode when the request has clear deliverables.")).not.toBeNull();
  });

  it("shows detailed API errors on data loading failures", async () => {
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
          return jsonResponse(
            { error: { code: "service_unavailable", message: "database is not ready" } },
            { status: 503, headers: { "X-Error-ID": "err_123" } },
          );
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath="/" />);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Failed to load runs: database is not ready (service_unavailable, HTTP 503, error err_123)",
    );
  });
});
