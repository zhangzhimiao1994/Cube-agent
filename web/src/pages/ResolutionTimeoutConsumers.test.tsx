import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import { RunDetailPage } from "./RunDetailPage";
import { RunsPage } from "./RunsPage";

const id = "55555555-5555-4555-8555-555555555555";
const observation = { resolution_phase: "freeze", resolution_progress: "freeze_pending",
  resolution_progress_evidence: "validated_root_status", original_deadline_expired: null };
const rawRun = {
  id, status: "failed", mode: "dispatch", version: 2, conversation_id: "existing", request: "修复 Python 项目",
  created_at: "2026-11-15T00:00:00Z", queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0",
  events: [], artifacts: [], explicit_details: {}, tool_lifecycle: [],
  failure_diagnostics: [{ category: "runtime", stage: "runtime.failed", sequence: 3,
    reason: "runtime_failed", recommendation: "保留原意图与期限；结果未知时仅检查，禁止重放。",
    error_stage: "project_verification", error_category: "timeout", retryable: false,
    error_code: "project_verification.resolution.resolution_timeout", ...observation }],
  model_outcome_summary: { completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
    requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] },
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("retains timeout facts through the real API reader including unknown original deadline", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(rawRun), { status: 200 })));
  const run = await api.run(id);
  expect(run.failure_diagnostics[0]).toMatchObject(observation);
});

it.each([
  { original_deadline_expired: 1 }, { resolution_phase: "PRIVATE password/raw/path" },
  { resolution_progress: null }, { resolution_progress_evidence: "none" },
])("rejects malformed observation through the real API reader: %j", async (change) => {
  const malformed = { ...rawRun, failure_diagnostics: [{ ...rawRun.failure_diagnostics[0], ...change }] };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(malformed), { status: 200 })));
  await expect(api.run(id)).rejects.toThrow();
});

it.each([true, false])("keeps the original observed deadline boolean %s without creating repair permission", async (expired) => {
  const payload = { ...rawRun, failure_diagnostics: [{ ...rawRun.failure_diagnostics[0], original_deadline_expired: expired }] };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 })));
  const run = await api.run(id);
  expect(run.failure_diagnostics[0].original_deadline_expired).toBe(expired);
  expect(run.failure_diagnostics[0].retryable).toBe(false);
  expect(run.repair_proposal).toBeFalsy();
});

it.each(["conversation", "technical"])("renders finite Chinese timeout facts on the %s page", async (page) => {
  // Parse the real transport schema before either page consumes the response.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(rawRun), { status: 200 })));
  const run = await api.run(id);
  const conversation = { conversation_id: "existing", title: "现有项目", project_id: "project",
    workspace_path: "session", created_at: rawRun.created_at, runs: [run] };
  vi.spyOn(api, "me").mockResolvedValue({ tenant_id: "tenant", user_id: "operator", username: "operator",
    role: "operator", permissions: ["run:create", "run:read", "config:read"] });
  vi.spyOn(api, "runs").mockResolvedValue([run]);
  vi.spyOn(api, "run").mockResolvedValue(run);
  vi.spyOn(api, "conversation").mockResolvedValue(conversation);
  vi.spyOn(api, "conversations").mockResolvedValue([conversation]);
  vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
  vi.spyOn(api, "models").mockResolvedValue([]);
  vi.spyOn(api, "projectWorkspaces").mockResolvedValue([]);
  vi.spyOn(api, "executionBackends").mockResolvedValue([]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const path = page === "conversation" ? "/runs?conversation=existing" : `/runs/${id}`;
  render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter initialEntries={[path]}>
    <Routes><Route path="/runs" element={<RunsPage />} /><Route path="/runs/:runId" element={<RunDetailPage />} /></Routes>
  </MemoryRouter></AuthProvider></QueryClientProvider>);
  if (page === "conversation") {
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /Agent 工作席/ }));
    await user.click(screen.getByRole("tab", { name: "真实调度记录" }));
  }
  expect((await screen.findAllByText("解析阶段：冻结依赖")).length).toBeGreaterThan(0);
  expect(screen.getAllByText("解析进度：等待冻结结果").length).toBeGreaterThan(0);
  expect(screen.getAllByText("原期限：是否已到期未知").length).toBeGreaterThan(0);
  expect(screen.getAllByText("进度依据：已校验的执行状态").length).toBeGreaterThan(0);
  expect(screen.queryByText("仅表示观察，不代表成功或允许重放。")).toBeNull();
});
