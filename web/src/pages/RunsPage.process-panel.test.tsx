import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type Conversation, type RunDetail } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import { RunProcessDrawer, RunsPage, runProcessItems } from "./RunsPage";

const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
});

function fixture(mode = "discuss"): RunDetail {
  const event = (sequence: number, kind: string, message: string, payload: Record<string, unknown> = {}) => ({
    sequence, kind, message, payload, participants: [], actor: "project_engineer", created_at: "2026-10-24T03:00:00Z",
  });
  return {
    id: "run-panel", conversation_id: "conv-panel", request: "Build the fixture project", mode, status: "failed",
    created_at: "2026-10-24T03:00:00Z", version: 8, queue_wait_ms: 0, capacity_wait_ms: 0, cost_usd: "0",
    artifacts: [], tool_lifecycle: [], failure_diagnostics: [], orchestration_protocol_summary: null,
    explicit_details: { project_id: "fixture", workspace_session_id: "main", harness_provider: "fixture-provider" },
    model_outcome_summary: { completion_count: 0, fallback_used: false, fallback_attempt_count: 0,
      requested_logical_models: [], actual_logical_models: [], attempted_logical_models: [], provider_ids: [] },
    events: [
      event(1, "main_agent.plan", "Configured plan", { roles: [{ id: "project_engineer", name: "工程师", role: "工程师",
        purpose: "工程实现", model: "configured-model", tools: ["workspace.write_text"] }], steps: [],
        dispatch_discussion_trace: { member_statements: { project_engineer: "SYNTHETIC_NOT_A_TURN" } } }),
      event(2, "step.started", "Started implementation"),
      event(3, "message.created", "PUBLIC_ENGINEER_TURN"),
      { ...event(4, "tool.completed", "tool.completed", { summary: "文件写入已记录", arguments: { command: "SECRET_COMMAND" } }), tool_name: "workspace.write_text" },
      event(5, "discussion.completed", "discussion.completed", { summary: "LAST_UTTERANCE_NOT_MINUTES" }),
      event(6, "model.reasoning_delta", "SECRET_REASONING", { text: "SECRET_REASONING" }),
      event(7, "runtime.failed", "model transport failed (status=503)"),
    ],
  };
}

async function mount(mode?: string, empty = false) {
  const user = userEvent.setup();
  const run = fixture(mode);
  if (empty) run.events = [];
  const conversation: Conversation = {
    conversation_id: "conv-panel", title: "Panel fixture", project_id: "fixture", project_label: "Fixture",
    workspace_path: "main", archived_at: null, created_at: run.created_at, updated_at: run.created_at, runs: [run],
  };
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false, staleTime: Infinity } } });
  clients.push(client);
  client.setQueryData(["runs"], [run]);
  client.setQueryData(["conversations", false], [conversation]);
  client.setQueryData(["conversations", true], []);
  client.setQueryData(["conversation", "conv-panel"], conversation);
  client.setQueryData(["conversation-queue", "conv-panel"], []);
  client.setQueryData(["project-workspaces"], []);
  vi.spyOn(api, "me").mockResolvedValue({ tenant_id: "fixture-tenant", user_id: "fixture-user", username: "fixture",
    role: "operator", permissions: ["*"] });
  vi.spyOn(api, "conversation").mockResolvedValue(conversation);
  vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
  vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
  vi.spyOn(api, "capabilityApprovals").mockResolvedValue({ items: [], ready: false, conversation_approval_available: false });
  render(<QueryClientProvider client={client}><AuthProvider><MemoryRouter initialEntries={["/?conversation=conv-panel"]}>
    <RunsPage />
  </MemoryRouter></AuthProvider></QueryClientProvider>);
  const stream = await screen.findByRole("region", { name: "主对话内容" });
  await user.click(await within(stream).findByRole("button", { name: /Agent 工作席/ }));
  return { user, panel: screen.getByRole("dialog", { name: "Agent 工作席详情" }) };
}

describe("one process panel with summary and genuine records", () => {
  it.each(["direct", "dispatch", "discuss", "hybrid"])("keeps the same two tabs for %s and an individual agent", async (mode) => {
    const { user, panel } = await mount(mode);
    expect(within(panel).getByRole("tab", { name: "摘要" }).getAttribute("aria-selected")).toBe("true");
    expect(within(panel).getAllByRole("tab")).toHaveLength(2);
    expect(within(panel).getByText("未生成会议纪要")).toBeTruthy();
    await user.click(within(panel).getByRole("button", { name: /打开.*工作调度/ }));
    expect(within(panel).getAllByRole("tab")).toHaveLength(2);
    await user.click(within(panel).getByRole("tab", { name: "真实调度记录" }));
    expect(within(panel).getByRole("tabpanel").textContent).toContain("PUBLIC_ENGINEER_TURN");
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });

  it("labels public excerpts honestly and never converts a last turn into meeting consensus", async () => {
    const { panel } = await mount();
    const summary = within(panel).getByRole("tabpanel");
    expect(summary.textContent).toContain("PUBLIC_ENGINEER_TURN");
    expect(summary.textContent).toContain("阻塞");
    expect(summary.textContent).not.toMatch(/共识|讨论纪要：|SYNTHETIC_NOT_A_TURN|SECRET_REASONING|SECRET_COMMAND/);
  });

  it("supports arrow and Home/End tab navigation with matched panel relationships", async () => {
    const { user, panel } = await mount();
    const summary = within(panel).getByRole("tab", { name: "摘要" });
    const records = within(panel).getByRole("tab", { name: "真实调度记录" });
    summary.focus();
    await user.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(records);
    expect(records.getAttribute("aria-controls")).toBe(within(panel).getByRole("tabpanel").id);
    await user.keyboard("{Home}");
    expect(document.activeElement).toBe(summary);
    await user.keyboard("{End}");
    expect(document.activeElement).toBe(records);
  });

  it("expands actual safe event details inside the same panel instead of stacked evidence dialogs", async () => {
    const { user, panel } = await mount();
    await user.click(within(panel).getByRole("tab", { name: "真实调度记录" }));
    const records = within(panel).getByRole("tabpanel");
    expect(records.textContent).toContain("PUBLIC_ENGINEER_TURN");
    expect(records.textContent).toContain("工具反馈");
    expect(records.textContent).not.toMatch(/SECRET_COMMAND|SECRET_REASONING|SYNTHETIC_NOT_A_TURN|讨论纪要：/);
    await user.click(within(records).getByRole("button", { name: /展开记录.*PUBLIC_ENGINEER_TURN/ }));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(within(records).getByRole("region", { name: "记录详情" })).toBeTruthy();
  });

  it("shows honest empty states without inventing records or minutes", async () => {
    const { user, panel } = await mount("direct", true);
    expect(within(panel).getByText("未生成会议纪要")).toBeTruthy();
    expect(within(panel).getByText("暂无公开发言记录")).toBeTruthy();
    await user.click(within(panel).getByRole("tab", { name: "真实调度记录" }));
    expect(within(within(panel).getByRole("tabpanel")).getByText("暂无真实调度记录")).toBeTruthy();
  });

  it("uses the same two-tab surface for direct process details without nested group dialogs", async () => {
    const user = userEvent.setup();
    const detail = fixture("hybrid");
    const target = runProcessItems(detail, new Map()).find((item) => item.sourceKind === "discussion.completed")!;
    render(<RunProcessDrawer target={target} detail={detail} onClose={() => {}} />);
    const panel = screen.getByRole("dialog", { name: "运行过程详情" });
    expect(within(panel).getByRole("tab", { name: "摘要" }).getAttribute("aria-selected")).toBe("true");
    await user.click(within(panel).getByRole("tab", { name: "真实调度记录" }));
    const records = within(panel).getByRole("tabpanel");
    expect(records.textContent).toContain("LAST_UTTERANCE_NOT_MINUTES");
    expect(records.textContent).not.toMatch(/会议纪要|共识/);
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(within(records).getByRole("group", { name: "运行详情摘要" })).toBeTruthy();
  });

  it("retains routing and execution configuration as labelled planning data, not actual feedback", async () => {
    const { user, panel } = await mount("hybrid");
    await user.click(within(panel).getByText("计划与配置"));
    expect(within(panel).getByText("fixture-provider")).toBeTruthy();
    await user.click(within(panel).getByRole("tab", { name: "真实调度记录" }));
    expect(within(panel).getByRole("tabpanel").textContent).not.toContain("fixture-provider");
  });
});
