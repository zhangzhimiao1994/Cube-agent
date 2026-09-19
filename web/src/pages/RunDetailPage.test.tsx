import { readFileSync } from "node:fs";

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunDetail } from "../api/client";
import { TestApp } from "../app/router";

const runId = "55555555-5555-4555-8555-555555555555";
const longArtifactText =
  "完整产物正文：第一段包含很长的执行输出，第二段包含 payload 原始内容，第三段包含需要点击详情后才能阅读的最终脚本。";
const rawPayloadOutput =
  "RAW_PAYLOAD_OUTPUT_SHOULD_ONLY_APPEAR_IN_DETAIL_MODAL with enough length to prove it is not flattened on the page";
const longUnsafeSummary =
  "SUMMARY_SHOULD_BE_COMPACTED_BEFORE_DRAWER_RENDERING 第一段非常长，包含很多模型运行细节，不能完整平铺在一级页面或动作详情抽屉里。第二段继续追加上下文。";
const nestedSecret = "NESTED_SECRET_SHOULD_NEVER_RENDER";

it("keeps process detail values readable in narrow workbench drawers", () => {
  const stylesCss = readFileSync("src/styles.css", "utf8");

  expect(stylesCss).toMatch(
    /\.run-process-detail dd\s*{[\s\S]*overflow-wrap:\s*anywhere;[\s\S]*word-break:\s*break-word;/,
  );
  expect(stylesCss).toMatch(
    /@media \(max-width: 980px\)[\s\S]*\.run-process-detail dl\s*{[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\);/,
  );
  expect(stylesCss).toMatch(
    /@media \(max-width: 980px\)[\s\S]*\.process-intermediate-card > strong,[\s\S]*\.process-intermediate-card > small:not\(\.process-card-badge\)\s*{[\s\S]*grid-column:\s*2;/,
  );
  expect(stylesCss).toMatch(
    /@media \(max-width: 640px\)[\s\S]*\.agent-workbench-tabs\s*{[\s\S]*display:\s*flex;[\s\S]*overflow-x:\s*auto;/,
  );
});

const runDetail: RunDetail = {
  id: runId,
  status: "completed",
  mode: "dispatch",
  version: 1,
  conversation_id: "conv-run-detail",
  request: "请生成独立运行详情页回归样例。",
  created_at: "2026-08-20T00:00:00Z",
  queue_wait_ms: 10,
  capacity_wait_ms: 5,
  cost_usd: "0.0100",
  events: [
    {
      sequence: 1,
      kind: "artifact.created",
      message: rawPayloadOutput,
      summary: longUnsafeSummary,
      created_at: "2026-08-20T00:00:01Z",
      actor: "writer",
      participants: [],
      tool_name: "artifact_writer",
      step_id: "write-final",
      action: null,
      decision: null,
      payload: {
        output: rawPayloadOutput,
        output_bytes: 1234,
        artifact_id: "artifact-final",
        metadata: {
          token: nestedSecret,
          safe_note: "nested safe note",
        },
      },
      artifact: {
        id: "artifact-final",
        kind: "markdown",
        title: "最终脚本产物",
        text: longArtifactText,
        filename: "final-script.md",
        mime_type: "text/markdown",
        size_bytes: 2048,
        sha256: "a".repeat(64),
        download_url: "/api/v1/admin/artifacts/final-script.md",
      },
    },
  ],
  artifacts: [
    {
      id: "artifact-final",
      kind: "markdown",
      title: "最终脚本产物",
      text: longArtifactText,
      filename: "final-script.md",
      mime_type: "text/markdown",
      size_bytes: 2048,
      sha256: "a".repeat(64),
      download_url: "/api/v1/admin/artifacts/final-script.md",
    },
  ],
  explicit_details: {
    selected_agent_ids: "writer",
    routing_reason: "workflow selected explicitly",
  },
  failure_diagnostics: [],
  tool_lifecycle: [],
  model_outcome_summary: {
    completion_count: 0,
    fallback_used: false,
    fallback_attempt_count: 0,
    requested_logical_models: [],
    actual_logical_models: [],
    attempted_logical_models: [],
    provider_ids: [],
  },
  orchestration_protocol_summary: null,
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("RunDetailPage", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(runDetail);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it("keeps long run outputs behind categorized detail cards while preserving artifact downloads", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByRole("heading", { name: "运行详情" })).not.toBeNull();
    expect(screen.queryByRole("status", { name: "模型结果摘要" })).toBeNull();
    expect(screen.queryByText(longArtifactText)).toBeNull();
    expect(screen.queryByText(rawPayloadOutput)).toBeNull();
    expect(screen.queryByText(longUnsafeSummary)).toBeNull();
    expect(screen.queryByText(nestedSecret)).toBeNull();
    expect(screen.queryByText("输出内容")).toBeNull();
    expect(screen.queryByText("artifact_id")).toBeNull();

    const processSummary = screen.getByLabelText("Agent 集群动作");
    const processCard = within(processSummary).getByRole("button", { name: /Agent 工作席/ });
    const controlsId = processCard.getAttribute("aria-controls");
    expect(controlsId).toBeTruthy();
    await user.click(processCard);

    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    const backdrop = document.querySelector(".process-drawer-backdrop");
    expect(backdrop?.parentElement).toBe(document.body);
    expect(controlsId ? document.getElementById(controlsId) : null).toBe(drawer);
    expect(document.body.style.overflow).toBe("hidden");
    expect(document.body.style.touchAction).toBe("none");
    expect(document.documentElement.style.overflow).toBe("hidden");
    await user.click(within(drawer).getByRole("button", { name: /SUMMARY_SHOULD_BE_COMPACTED_BEFORE_DRA/ }));
    expect(within(drawer).getByRole("button", { name: /产物/ })).not.toBeNull();
    expect(within(drawer).getByRole("button", { name: /证据/ })).not.toBeNull();
    expect(within(drawer).queryByText(longArtifactText)).toBeNull();
    expect(within(drawer).queryByText(rawPayloadOutput)).toBeNull();
    expect(within(drawer).queryByText(longUnsafeSummary)).toBeNull();
    expect(within(drawer).queryByText(nestedSecret)).toBeNull();
    expect(within(drawer).getByRole("button", { name: /下载 final-script\.md/ })).not.toBeNull();

    await user.click(within(drawer).getByRole("button", { name: /产物/ }));
    const productDetail = await screen.findByRole("dialog", { name: "产物详情" });
    expect(within(productDetail).getByText(longArtifactText)).not.toBeNull();
    expect(within(productDetail).getByText("final-script.md")).not.toBeNull();
    await user.click(within(productDetail).getByRole("button", { name: "关闭" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "产物详情" })).toBeNull());
    await user.click(within(drawer).getByRole("button", { name: /证据/ }));
    const evidenceDetail = await screen.findByRole("dialog", { name: "证据详情" });
    expect(within(evidenceDetail).getByText("2026-08-20T00:00:01Z")).not.toBeNull();
    expect(within(evidenceDetail).queryByText(nestedSecret)).toBeNull();
    await user.click(document.querySelector(".process-detail-modal-backdrop") as HTMLElement);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "证据详情" })).toBeNull());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Agent 工作席详情" })).toBeNull());
    expect(document.body.style.overflow).toBe("");
    expect(document.body.style.touchAction).toBe("");
    expect(document.documentElement.style.overflow).toBe("");

    await user.click(processCard);
    expect(await screen.findByRole("dialog", { name: "Agent 工作席详情" })).not.toBeNull();
    await user.click(document.querySelector(".process-drawer-backdrop") as HTMLElement);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Agent 工作席详情" })).toBeNull());
    expect(document.body.style.overflow).toBe("");
    expect(document.body.style.touchAction).toBe("");
    expect(document.documentElement.style.overflow).toBe("");
  });

  it("keeps the agent workbench as one entry point and opens dispatch details on click", async () => {
    const user = userEvent.setup();
    const detailedRun: RunDetail = {
      ...runDetail,
      orchestration_protocol_summary: {
        protocol: "role_handoff_contract_v1",
        status: "completed",
        role_count: 2,
        handoff_count: 1,
        contract_count: 1,
        blocked_contract_count: 0,
        truncated: false,
      },
      events: [
        {
          ...runDetail.events[0],
          sequence: 1,
          summary: "主 Agent 初始判断",
          actor: "main_agent",
          step_id: "main-agent-plan",
        },
        {
          ...runDetail.events[0],
          sequence: 2,
          kind: "step.started",
          message: "reviewer scheduled",
          summary: "reviewer 子 Agent 调度",
          actor: "reviewer",
          step_id: "reviewer-dispatch",
          artifact: null,
        },
        {
          ...runDetail.events[0],
          sequence: 3,
          kind: "runtime.completed",
          message: "critic completed",
          summary: "critic 子 Agent 已下班",
          actor: "critic",
          step_id: "critic-complete",
          artifact: null,
        },
        {
          ...runDetail.events[0],
          sequence: 4,
          kind: "message.created",
          message: "critic final note",
          summary: "critic 子 Agent 追加收尾",
          actor: "critic",
          step_id: "critic-final-note",
          artifact: null,
        },
        {
          ...runDetail.events[0],
          sequence: 5,
          kind: "runtime.completed",
          message: "unassigned cleanup completed",
          summary: "未标记 actor 的收尾",
          actor: null,
          step_id: "unassigned-cleanup",
          artifact: null,
        },
        {
          ...runDetail.events[0],
          sequence: 6,
          kind: "step.failed",
          message: "qa failed",
          summary: "qa 子 Agent 检查失败",
          actor: "qa",
          step_id: "qa-review",
          artifact: null,
        },
        {
          ...runDetail.events[0],
          sequence: 7,
          kind: "dispatch.queued",
          message: "planner queued",
          summary: "planner 子 Agent 已安排",
          actor: "planner",
          step_id: "planner-queued",
          artifact: null,
        },
      ],
      artifacts: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    const workbenchButton = within(processSummary).getByRole("button", { name: /Agent 工作席/ });
    expect(within(processSummary).getAllByRole("button")).toHaveLength(1);
    expect(workbenchButton.textContent).toContain("6 个 Agent");
    expect(workbenchButton.textContent).toContain("1 异常");
    expect(workbenchButton.textContent).toContain("1 工作中");
    expect(workbenchButton.textContent).toContain("3 已完成");
    expect(workbenchButton.textContent).toContain("1 已安排");
    expect(workbenchButton.textContent).not.toContain("critic 子 Agent 已下班");
    expect(within(processSummary).queryByText("reviewer 子 Agent 调度")).toBeNull();
    expect(within(processSummary).queryByText("critic 子 Agent 已下班")).toBeNull();

    await user.click(workbenchButton);

    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    expect(within(drawer).getByRole("button", { name: "助手总览" }).getAttribute("aria-pressed")).toBe("true");
    expect(within(drawer).getByRole("button", { name: "调度讨论" }).textContent).toContain("1 条");
    expect(within(drawer).getByRole("button", { name: "实际动作" }).textContent).toContain("5 条");
    expect(within(drawer).getByRole("button", { name: "修复异常" }).textContent).toContain("1 条");
    const workbenchOverview = within(drawer).getByLabelText("Agent 工作席概览");
    expect(within(drawer).getByText("主 Agent 初始判断")).not.toBeNull();
    expect(within(drawer).getByText("reviewer 子 Agent 调度")).not.toBeNull();
    expect(within(workbenchOverview).queryByText("critic 子 Agent 已下班")).toBeNull();
    expect(drawer.querySelector(".run-process-detail")).toBeNull();
    await user.click(within(drawer).getByRole("button", { name: "调度讨论" }));
    expect(within(drawer).getByRole("button", { name: "调度讨论" }).getAttribute("aria-pressed")).toBe("true");
    const coordinationActions = within(drawer).getByLabelText("Agent 工作席动作");
    expect(within(coordinationActions).getByRole("button", { name: /planner 子 Agent 已安排/ })).not.toBeNull();
    expect(within(coordinationActions).queryByRole("button", { name: /critic 子 Agent 已下班/ })).toBeNull();

    await user.click(within(drawer).getByRole("button", { name: "实际动作" }));
    const workbenchActions = within(drawer).getByLabelText("Agent 工作席动作");
    expect(within(workbenchActions).getByRole("button", { name: /reviewer 子 Agent 调度/ })).not.toBeNull();
    expect(within(workbenchActions).getByRole("button", { name: /critic 子 Agent 已下班/ })).not.toBeNull();
    await user.click(within(workbenchActions).getByRole("button", { name: /reviewer 子 Agent 调度/ }));
    expect((drawer.querySelector(".run-process-detail") as HTMLElement).textContent).toContain("reviewer 子 Agent 调度");
    expect((drawer.querySelector(".run-process-detail") as HTMLElement).textContent).not.toContain("critic 子 Agent 已下班");
  });

  it("splits run detail workbench files terminals and results into separate inspectable windows", async () => {
    const user = userEvent.setup();
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        {
          ...runDetail.events[0],
          sequence: 1,
          kind: "tool.completed",
          message: "npm test completed",
          summary: "运行终端 npm test",
          actor: "runner",
          tool_name: "terminal",
          step_id: "terminal-test",
          artifact: null,
          payload: {
            operation_kind: "terminal",
            command: "npm test",
            exit_code: 0,
            output: "1 test passed",
          },
        },
        {
          ...runDetail.events[0],
          sequence: 2,
          kind: "artifact.created",
          message: "created final script",
          summary: "创建文件 final-script.md",
          actor: "writer",
          step_id: "write-final",
          payload: {
            operation_kind: "file_create",
            artifact_id: "artifact-final",
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
          artifact: runDetail.artifacts[0],
        },
      ],
      artifacts: [runDetail.artifacts[0]],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    await user.click(within(processSummary).getByRole("button", { name: /Agent 工作席/ }));
    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });

    expect(within(drawer).getByRole("button", { name: "文件" }).textContent).toContain("2 个");
    expect(within(drawer).getByRole("button", { name: "终端" }).textContent).toContain("1 条");
    expect(within(drawer).getByRole("button", { name: "结果" }).textContent).toContain("1 条");

    await user.click(within(drawer).getByRole("button", { name: "文件" }));
    const filesWindow = within(drawer).getByLabelText("文件窗口");
    const fileList = within(filesWindow).getByLabelText("文件操作列表");
    const finalScriptButton = within(fileList).getByRole("button", { name: /final-script\.md/ });
    expect(finalScriptButton).not.toBeNull();
    expect(within(fileList).getByRole("button", { name: /src\/app\.ts/ })).not.toBeNull();
    await user.click(finalScriptButton);
    expect(within(filesWindow).getByText(longArtifactText)).not.toBeNull();

    await user.click(within(drawer).getByRole("button", { name: "终端" }));
    const terminalWindow = within(drawer).getByLabelText("终端窗口");
    expect(within(terminalWindow).getByRole("button", { name: /运行终端 npm test/ })).not.toBeNull();

    await user.click(within(drawer).getByRole("button", { name: "结果" }));
    const resultWindow = within(drawer).getByLabelText("结果窗口");
    expect(within(resultWindow).getByRole("button", { name: /创建文件 final-script\.md/ })).not.toBeNull();
  });

  it("shows checkpoint recovery as a clear workbench action without leaking checkpoint internals", async () => {
    const user = userEvent.setup();
    const detailedRun: RunDetail = {
      ...runDetail,
      runtime_recovery_summary: {
        recovery_count: 1,
        last_completed_steps: 2,
        last_total_steps: 5,
        model_status_counts: { failed: 1, succeeded: 2 },
        tool_status_counts: { running: 1 },
        review_artifacts: 1,
      },
      events: [
        {
          ...runDetail.events[0],
          sequence: 1,
          kind: "runtime.recovered",
          message: "runtime recovered from checkpoint",
          summary: null,
          actor: "main_agent",
          step_id: "runtime-recovery",
          artifact: null,
          payload: {
            recovery_count: 1,
            completed_steps: 2,
            total_steps: 5,
            model_status_counts: { failed: 1, succeeded: 2 },
            tool_status_counts: { running: 1 },
            review_artifacts: 1,
            checkpoint_id: "checkpoint-00000000-0000-4000-8000-000000000001",
          },
        },
      ],
      artifacts: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    await user.click(within(processSummary).getByRole("button", { name: /Agent 工作席/ }));
    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    await user.click(within(drawer).getByRole("button", { name: "修复异常" }));
    const workbenchActions = within(drawer).getByLabelText("Agent 工作席动作");
    const recoveryAction = within(workbenchActions).getByRole("button", { name: /断点续跑/ });

    expect(recoveryAction.textContent).toContain("恢复完成");
    expect(recoveryAction.textContent).toContain("2/5 步");
    expect(within(drawer).queryByText("checkpoint-00000000-0000-4000-8000-000000000001")).toBeNull();

    await user.click(recoveryAction);

    const detail = drawer.querySelector(".run-process-detail") as HTMLElement;
    expect(detail.textContent).toContain("断点续跑");
    expect(detail.textContent).toContain("模型状态：异常 1，已完成 2");
    expect(detail.textContent).toContain("工具状态：进行中 1");
    expect(detail.textContent).toContain("审查产物 1");
    expect(detail.textContent).not.toContain("checkpoint-00000000-0000-4000-8000-000000000001");
  });

  it("keeps action detail fields readable in the mobile workbench drawer", async () => {
    const user = userEvent.setup();
    window.innerWidth = 390;
    window.innerHeight = 844;
    window.dispatchEvent(new Event("resize"));
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        {
          ...runDetail.events[0],
          sequence: 1,
          kind: "decision.completed",
          message: "decision.completed",
          summary: "主 Agent 选择运行模式与角色",
          actor: "main_agent",
          step_id: "mode-decision",
          artifact: null,
          payload: {
            capability_execution_plan:
              "Agent selected the runtime mode, roles, and models. The value should wrap by words instead of becoming a one-letter vertical column.",
          },
        },
      ],
      artifacts: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    await user.click(within(processSummary).getByRole("button", { name: /Agent 工作席/ }));
    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    await user.click(within(drawer).getByRole("button", { name: "调度讨论" }));
    const workbenchActions = within(drawer).getByLabelText("Agent 工作席动作");
    await user.click(within(workbenchActions).getByRole("button", { name: /主 Agent 选择运行模式与角色/ }));

    const detailRegion = drawer.querySelector(".run-process-detail") as HTMLElement;
    expect(detailRegion.textContent).toContain("主 Agent 选择运行模式与角色");
    await user.click(within(detailRegion).getByRole("button", { name: /(活动|证据|决策)：Agent selected the runtime mode/ }));
    const detailModal = await screen.findByRole("dialog", { name: /(活动|证据|决策)详情/ });
    const detailValue = within(detailModal).getByText(/Agent selected the runtime mode/);
    expect(detailValue.closest("dd")?.textContent).toContain("wrap by words instead of becoming a one-letter vertical column");
  });

  it("compresses long run detail workbench action lists until expanded", async () => {
    const user = userEvent.setup();
    const longActionRun: RunDetail = {
      ...runDetail,
      events: Array.from({ length: 16 }, (_, index) => {
        const sequence = index + 1;
        return {
          ...runDetail.events[0],
          sequence,
          kind: "step.started",
          message: `worker step ${sequence}`,
          summary: `worker 第 ${sequence.toString().padStart(2, "0")} 步处理`,
          actor: "worker",
          step_id: `worker-step-${sequence}`,
          artifact: null,
          payload: {},
        };
      }),
      artifacts: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(longActionRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    await user.click(within(processSummary).getByRole("button", { name: /Agent 工作席/ }));

    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    await user.click(within(drawer).getByRole("button", { name: "实际动作" }));
    const workbenchActions = within(drawer).getByLabelText("Agent 工作席动作");
    expect(within(drawer).getByText("已折叠 4 个较早实际动作")).not.toBeNull();
    expect(within(workbenchActions).queryByRole("button", { name: /worker 第 01 步处理/ })).toBeNull();
    expect(within(workbenchActions).getByRole("button", { name: /worker 第 05 步处理/ })).not.toBeNull();
    expect(within(workbenchActions).getByRole("button", { name: /worker 第 16 步处理/ })).not.toBeNull();

    await user.click(within(drawer).getByRole("button", { name: "显示全部实际动作" }));

    expect(within(drawer).queryByText("已折叠 4 个较早实际动作")).toBeNull();
    expect(within(workbenchActions).getByRole("button", { name: /worker 第 01 步处理/ })).not.toBeNull();
    expect(within(drawer).getByRole("button", { name: "收起实际动作" })).not.toBeNull();
  });

  it("renders run detail events in sequence order when backend payload arrives out of order", async () => {
    const outOfOrderDetail: RunDetail = {
      ...runDetail,
      events: [
        {
          ...runDetail.events[0],
          sequence: 2,
          summary: "第二步产物",
          created_at: "2026-08-20T00:00:02Z",
          actor: "writer",
          step_id: "write-final",
        },
        {
          ...runDetail.events[0],
          sequence: 1,
          kind: "step.started",
          message: "step.started",
          summary: "第一步规划",
          created_at: "2026-08-20T00:00:01Z",
          actor: "main_agent",
          tool_name: null,
          step_id: "plan",
          payload: { task: "第一步规划" },
          artifact: null,
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(outOfOrderDetail);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByRole("heading", { name: "运行详情" })).not.toBeNull();
    const eventRows = Array.from(document.querySelectorAll(".event-log-list li")).map((row) => row.textContent ?? "");
    expect(eventRows).toEqual([
      expect.stringContaining("第一步规划"),
      expect.stringContaining("第二步产物"),
    ]);
  });

  it("shows safe observer recommendations from payload metadata", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        ...runDetail.events,
        {
          sequence: 8,
          kind: "observer.notice",
          message: "observer.notice",
          created_at: "2026-08-20T00:00:08Z",
          actor: null,
          participants: [],
          step_id: null,
          payload: {
            trigger: "model_capacity_pressure",
            action: "reschedule_or_reassign_model",
            severity: "warning",
            recommendation: "switch_to_available_model_and_retry",
            source_kind: "step.failed",
            source_sequence: 4,
            failure_events: 1,
            retry_events: 0,
            message_events: 3,
            artifact_events: 1,
            actor: "planner",
          },
        },
        {
          sequence: 9,
          kind: "observer.notice",
          message: "observer.notice",
          created_at: "2026-08-20T00:00:09Z",
          actor: null,
          participants: [],
          step_id: null,
          payload: {
            trigger: "model_capability_routing_unavailable",
            action: "reassign_tool_role_to_capable_model",
            severity: "warning",
            recommendation: "reassign_tool_role_to_capable_model_and_retry",
            source_kind: "runtime.failed",
            source_sequence: 5,
            failure_events: 2,
            retry_events: 0,
            message_events: 3,
            artifact_events: 1,
            actor: "main_agent",
          },
        },
        {
          sequence: 10,
          kind: "observer.notice",
          message: "observer.notice",
          created_at: "2026-08-20T00:00:10Z",
          actor: null,
          participants: [],
          step_id: null,
          payload: {
            trigger: "runtime_failure",
            action: "preserve_partial_outputs",
            severity: "info",
            recommendation: "raw_unknown_recommendation_should_not_render",
            source_kind: "runtime.failed",
            source_sequence: 5,
            failure_events: 2,
            retry_events: 0,
            message_events: 3,
            artifact_events: 1,
            actor: "main_agent",
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const observerHeading = await screen.findByRole("heading", { name: "调度观察" });
    const observerArticle = observerHeading.closest("article") as HTMLElement;
    expect(within(observerArticle).getByText("恢复建议：切换到有容量的同类模型，保留已有产物后重试。")).not.toBeNull();
    expect(within(observerArticle).getByText("恢复建议：将工具角色改派给支持工具调用的模型后重试。")).not.toBeNull();
    expect(within(observerArticle).getByText("角色：planner")).not.toBeNull();
    expect(within(observerArticle).getAllByText("角色：主 Agent").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/raw_unknown_recommendation_should_not_render/)).toBeNull();
  });

  it("renders model outcome summary without capacity internals", async () => {
    const user = userEvent.setup();
    const detailedRun: RunDetail = {
      ...runDetail,
      model_outcome_summary: {
        completion_count: 2,
        fallback_used: true,
        fallback_attempt_count: 1,
        requested_logical_models: ["planner", "main"],
        actual_logical_models: ["planner", "backup"],
        attempted_logical_models: ["planner", "main", "backup"],
        provider_ids: ["deepseek", "openai"],
        last_requested_logical_model: "main",
        last_logical_model: "backup",
        last_provider_id: "openai",
      },
      events: [
        ...runDetail.events,
        {
          sequence: 9,
          kind: "model.completed",
          message: "model.completed",
          created_at: "2026-08-20T00:00:09Z",
          actor: "main_agent",
          participants: [],
          payload: {
            lease_id: "lease-private",
            quota_scope_id: "tenant-private-quota",
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("模型结果")).not.toBeNull();
    expect(within(summary).getByText("2 次完成")).not.toBeNull();
    expect(within(summary).getByText("1 次回退")).not.toBeNull();
    expect(within(summary).getByText("planner, main")).not.toBeNull();
    expect(within(summary).getByText("planner, backup")).not.toBeNull();
    expect(within(summary).getByText("deepseek, openai")).not.toBeNull();
    expect(within(summary).getByText("main -> backup")).not.toBeNull();
    expect(screen.queryByText("lease-private")).toBeNull();
    expect(screen.queryByText("tenant-private-quota")).toBeNull();

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    await user.click(within(processSummary).getByRole("button", { name: /Agent 工作席/ }));
    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    await user.click(within(drawer).getByRole("button", { name: /模型过程/ }));
    expect(within(drawer).queryByText("lease-private")).toBeNull();
    expect(within(drawer).queryByText("tenant-private-quota")).toBeNull();
  });

  it("hides orchestration handoff summary when the run has no plan handoffs", async () => {
    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByRole("heading", { name: "运行详情" })).not.toBeNull();
    expect(screen.queryByRole("status", { name: "编排交接摘要" })).toBeNull();
    expect(screen.queryByRole("status", { name: "模型结果摘要" })).toBeNull();
  });

  it("renders orchestration handoff summary as one compact row without internals", async () => {
    const user = userEvent.setup();
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        ...runDetail.events,
        {
          sequence: 8,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:08Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              api_base: "https://internal.example.invalid",
              orchestration_handoffs: {
                schema_version: 1,
                items: [
                  {
                    source_step_id: "copywriter_step",
                    target_step_id: "final_response_step",
                    source_role_id: "copywriter",
                    target_role_id: "final_synthesizer",
                    source_purpose: "execute",
                    target_purpose: "synthesize",
                    source_logical_model: "creative",
                    target_logical_model: "main",
                    handoff_kind: "step_dependency",
                    lease_id: "lease-private",
                  },
                ],
                truncated: false,
              },
              orchestration_contracts: {
                schema_version: 1,
                items: [
                  {
                    contract_id: "copywriter_step-to-final_response_step",
                    source_step_id: "copywriter_step",
                    target_step_id: "final_response_step",
                    source_role_id: "copywriter",
                    target_role_id: "final_synthesizer",
                    handoff_kind: "step_dependency",
                    status: "planned",
                    required_output_fields: ["status", "summary", "evidence"],
                    ready_status: "done",
                    blocking_statuses: ["blocked", "needs_user"],
                    quota_scope_id: "tenant-private-quota",
                  },
                ],
                truncated: false,
              },
              orchestration_protocol: {
                schema_version: 1,
                protocol: "role_handoff_contract_v1",
                mode: "dispatch",
                role_count: 2,
                handoff_count: 1,
                contract_count: 1,
                structured_output_schema: "dispatch_output_v1",
                required_output_fields: ["status", "summary", "evidence"],
                ready_status: "done",
                blocking_statuses: ["blocked", "needs_user"],
                recovery_hints: ["retry_blocked_contract_chain"],
                truncated: false,
                lease_id: "lease-private",
              },
            },
          },
        },
        {
          sequence: 9,
          kind: "step.failed",
          message: "copywriter retryable failure",
          created_at: "2026-08-20T00:00:09Z",
          actor: "copywriter",
          participants: [],
          step_id: "copywriter_step",
          payload: {
            error_code: "temporary_failure",
          },
        },
        {
          sequence: 10,
          kind: "step.completed",
          message: "copywriter finished",
          created_at: "2026-08-20T00:00:10Z",
          actor: "copywriter",
          participants: [],
          step_id: "copywriter_step",
          payload: {
            status: "done",
          },
        },
        {
          sequence: 11,
          kind: "step.completed",
          message: "final synthesizer finished",
          created_at: "2026-08-20T00:00:11Z",
          actor: "final_synthesizer",
          participants: [],
          step_id: "final_response_step",
          payload: {
            status: "done",
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(summary.classList.contains("run-model-outcome-summary")).toBe(true);
    expect(within(summary).getByText("已记录交接")).not.toBeNull();
    expect(within(summary).getByText("角色交接协议")).not.toBeNull();
    expect(within(summary).getByText("2 个角色，1 个契约")).not.toBeNull();
    expect(within(summary).getByText("1 次交接")).not.toBeNull();
    expect(within(summary).getByText("1 个契约，已完成 1")).not.toBeNull();
    expect(within(summary).getByText("copywriter -> final_synthesizer")).not.toBeNull();
    expect(within(summary).getByText("creative -> main")).not.toBeNull();
    expect(within(summary).getByText("step_dependency")).not.toBeNull();
    expect(screen.queryByText("copywriter_step")).toBeNull();
    expect(screen.queryByText("final_response_step")).toBeNull();
    expect(screen.queryByText("copywriter_step-to-final_response_step")).toBeNull();
    expect(screen.queryByText("lease-private")).toBeNull();
    expect(screen.queryByText("role_handoff_contract_v1")).toBeNull();
    expect(screen.queryByText("tenant-private-quota")).toBeNull();
    expect(screen.queryByText("internal.example.invalid")).toBeNull();

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    await user.click(within(processSummary).getByRole("button", { name: /Agent 工作席/ }));
    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    await user.click(within(drawer).getByRole("button", { name: /执行步骤/ }));
    expect(within(drawer).queryByText("lease-private")).toBeNull();
    expect(within(drawer).queryByText("internal.example.invalid")).toBeNull();
  });

  it("renders model capability negotiation as a compact model outcome metric without internals", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      model_capability_negotiation_summary: {
        role_count: 3,
        satisfied_count: 1,
        missing_count: 1,
        unknown_count: 1,
        missing_capability_counts: {
          tool_calling: 1,
        },
        truncated: true,
      },
      capability_execution_summary: {
        permission_boundary: "runtime_capability_gateway",
        role_count: 2,
        capability_count: 3,
        inventory_count: 4,
        failure_code_count: 3,
        failure_code_counts: {
          "mcp.server_failed": 1,
          "plugin.invalid_arguments": 1,
          "plugin.timeout": 2,
          "plugin.secret_token": 1,
        },
        truncated: true,
      },
      events: [
        ...runDetail.events,
        {
          sequence: 8,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:08Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              model_capability_negotiation: {
                schema_version: 1,
                items: [
                  {
                    role_id: "token_leak",
                    logical_model: "sk_secret",
                    required_capabilities: ["tool_calling"],
                    matched_capabilities: [],
                    missing_capabilities: ["tool_calling"],
                    status: "missing_capability",
                    api_base: "https://model-internal.example.invalid",
                    quota_scope_id: "tenant-private-quota",
                  },
                ],
                role_count: 3,
                satisfied_count: 1,
                missing_count: 1,
                unknown_count: 1,
                truncated: true,
              },
            },
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("能力协商")).not.toBeNull();
    expect(within(summary).getByText("3 个角色，满足 1，缺口 1，缺 工具调用 1，未知 1，已截断")).not.toBeNull();
    expect(within(summary).queryByText(/tool_calling/)).toBeNull();
    expect(within(summary).getByText("能力执行边界")).not.toBeNull();
    expect(
      within(summary).getByText(
        "2 个角色，3 项能力，库存 4，失败码 3，MCP 服务失败 1，插件参数无效 1，插件执行超时 2，已截断",
      ),
    ).not.toBeNull();
    expect(within(summary).getByText("已记录能力协商")).not.toBeNull();
    expect(within(summary).queryByText(/mcp\.server_failed/)).toBeNull();
    expect(within(summary).queryByText(/plugin\.invalid_arguments/)).toBeNull();
    expect(within(summary).queryByText(/plugin\.timeout/)).toBeNull();
    expect(screen.queryByText("sk_secret")).toBeNull();
    expect(screen.queryByText("plugin.secret_token")).toBeNull();
    expect(screen.queryByText("token_leak")).toBeNull();
    expect(screen.queryByText("model-internal.example.invalid")).toBeNull();
    expect(screen.queryByText("tenant-private-quota")).toBeNull();
    expect(screen.queryByText("credential-private")).toBeNull();
  });

  it("keeps model outcome fallback posture when outcome and handoff summaries both exist", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      model_outcome_summary: {
        completion_count: 1,
        fallback_used: false,
        fallback_attempt_count: 0,
        requested_logical_models: ["main"],
        actual_logical_models: ["main"],
        attempted_logical_models: ["main"],
        provider_ids: ["deepseek"],
      },
      events: [
        ...runDetail.events,
        {
          sequence: 8,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:08Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              orchestration_handoffs: {
                schema_version: 1,
                items: [
                  {
                    source_role_id: "copywriter",
                    target_role_id: "final_synthesizer",
                    source_logical_model: "creative",
                    target_logical_model: "main",
                    handoff_kind: "step_dependency",
                  },
                ],
                truncated: false,
              },
              orchestration_contracts: {
                schema_version: 1,
                items: [
                  {
                    contract_id: "copywriter_step-to-final_response_step",
                    source_step_id: "copywriter_step",
                    target_step_id: "final_response_step",
                    source_role_id: "copywriter",
                    target_role_id: "final_synthesizer",
                    handoff_kind: "step_dependency",
                    status: "planned",
                    required_output_fields: ["status", "summary"],
                    ready_status: "done",
                    blocking_statuses: ["blocked", "needs_user"],
                    recovery_hint: "retry_blocked_contract_chain",
                  },
                ],
                truncated: false,
              },
            },
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("未发生回退")).not.toBeNull();
    expect(within(summary).getByText("1 次完成")).not.toBeNull();
    expect(within(summary).getByText("1 次交接")).not.toBeNull();
    expect(within(summary).getByText("1 个契约")).not.toBeNull();
    expect(within(summary).queryByText("已记录交接")).toBeNull();
  });

  it("shows runtime recovery summary as a compact outcome metric", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      runtime_recovery_summary: {
        recovery_count: 1,
        last_completed_steps: 2,
        last_total_steps: 5,
        model_status_counts: { failed: 1, succeeded: 2 },
        tool_status_counts: { running: 1 },
        review_artifacts: 1,
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("已恢复续跑")).not.toBeNull();
    expect(within(summary).getByText("1 次续跑，2/5 步")).not.toBeNull();
    expect(within(summary).getByText("模型状态：异常 1，已完成 2；工具状态：进行中 1；审查产物 1")).not.toBeNull();
    expect(screen.queryByText("00000000-0000-4000-8000-000000000001")).toBeNull();
  });

  it("hides zero-count runtime recovery summaries", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      runtime_recovery_summary: {
        recovery_count: 0,
        last_completed_steps: 0,
        last_total_steps: 0,
        model_status_counts: {},
        tool_status_counts: {},
        review_artifacts: 0,
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    await screen.findByText("请生成独立运行详情页回归样例。");
    expect(screen.queryByRole("status", { name: "模型结果摘要" })).toBeNull();
  });

  it("shows self-repair recovery summary as a compact outcome metric", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      self_repair_recovery_summary: {
        status: "active",
        recovery_strategy: "retry_blocked_contract_chain_after_replanning",
        orchestration_recovery_hint: "retry_blocked_contract_chain",
        replan_scope: "blocked_contract_chain",
        reuse_completed_artifacts: true,
        retry_blocked_contracts_only: true,
        automatic_execution: false,
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("已记录自修复")).not.toBeNull();
    expect(within(summary).getByText("自修复")).not.toBeNull();
    expect(within(summary).getByText("契约链重规划，范围 阻塞链路，只重试阻塞链路，复用已完成产物")).not.toBeNull();
    expect(screen.queryByText("retry_blocked_contract_chain_after_replanning")).toBeNull();
    expect(screen.queryByText("retry_blocked_contract_chain")).toBeNull();
  });

  it("shows plugin runtime self-repair recovery summaries without failing run detail parsing", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      self_repair_recovery_summary: {
        status: "active",
        recovery_strategy: "repair_plugin_endpoint_or_adapter_and_retry",
        replan_scope: "plugin_runtime",
        reuse_completed_artifacts: false,
        retry_blocked_contracts_only: false,
        automatic_execution: true,
      },
      artifacts: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("已记录自修复")).not.toBeNull();
    expect(within(summary).getByText("修复插件运行时并重试，范围 插件运行时，自动执行")).not.toBeNull();
    expect(screen.queryByText("repair_plugin_endpoint_or_adapter_and_retry")).toBeNull();
    expect(screen.queryByText("plugin_runtime")).toBeNull();
  });

  it("allows accepting a controlled self-repair proposal from run detail", async () => {
    const user = userEvent.setup();
    const requests: Array<{ path: string; method: string; body: unknown }> = [];
    const failedRun: RunDetail = {
      ...runDetail,
      status: "failed",
      version: 7,
      decision_token: "repair-token",
      repair_proposal: {
        kind: "self_repair",
        title: "重试失败的插件调用",
        summary: "插件端点临时不可用，修复后只重试失败步骤。",
        repair_action: "repair_plugin_endpoint_or_adapter",
        failure_kind: "plugin_runtime_unavailable",
        source_run_id: runId,
        source_event_sequence: 3,
        attempt: 1,
        max_attempts: 2,
        instruction: "只重试失败的插件工具调用。",
        requires_approval: true,
        replay_safe: true,
        automatic_execution: false,
        fingerprint: "repair-fingerprint",
        recovery_strategy: "repair_plugin_endpoint_or_adapter_and_retry",
      },
      artifacts: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "https://agent-hub.test");
        const method = init?.method ?? "GET";
        if (url.pathname === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (url.pathname === `/api/v1/admin/runs/${runId}`) return jsonResponse(failedRun);
        if (url.pathname === `/api/v1/runs/${runId}/accept-repair` && method === "POST") {
          requests.push({
            path: url.pathname,
            method,
            body: JSON.parse(String(init?.body)),
          });
          return jsonResponse({
            ...failedRun,
            status: "queued",
            decision_token: null,
            repair_proposal: null,
          });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const approval = await screen.findByRole("status", { name: "自修复确认" });
    expect(within(approval).getByText("自修复待确认")).not.toBeNull();
    expect(within(approval).getByText("重试失败的插件调用")).not.toBeNull();
    expect(within(approval).getByText(/插件运行时不可用/)).not.toBeNull();
    expect(within(approval).getByText(/修复插件端点或适配器/)).not.toBeNull();

    await user.click(within(approval).getByRole("button", { name: "接受修复" }));

    await waitFor(() => {
      expect(requests).toEqual([
        {
          path: `/api/v1/runs/${runId}/accept-repair`,
          method: "POST",
          body: {
            decision_token: "repair-token",
            version: 7,
          },
        },
      ]);
    });
  });

  it("labels repair intent metadata without exposing raw repair codes", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        {
          sequence: 2,
          kind: "self_repair.proposed",
          message: "self_repair.proposed",
          created_at: "2026-08-20T00:00:02Z",
          actor: "main_agent",
          participants: [],
          tool_name: null,
          step_id: "repair",
          action: "retry_with_fallback",
          decision: null,
          payload: {
            repair_action: "switch_model",
            failure_kind: "model_timeout",
            requires_approval: true,
            replay_safe: false,
          },
        },
        {
          sequence: 3,
          kind: "repair.started",
          message: "repair.started",
          created_at: "2026-08-20T00:00:03Z",
          actor: null,
          participants: [],
          tool_name: null,
          step_id: "repair",
          action: null,
          decision: null,
          payload: {
            repair_action: "draft_repair_proposal",
            failure_kind: "runtime_failure",
            status: "running",
            attempt: 1,
            max_attempts: 1,
            requires_approval: true,
          },
        },
      ],
      artifacts: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const intents = await screen.findByLabelText("执行意图");
    expect(within(intents).getByText("切换模型后重试")).not.toBeNull();
    expect(within(intents).getByText("生成受控修复提案")).not.toBeNull();
    expect(within(intents).getByText("模型调用超时")).not.toBeNull();
    expect(within(intents).getByText("运行阶段失败")).not.toBeNull();
    expect(intents.textContent).not.toContain("switch_model");
    expect(intents.textContent).not.toContain("model_timeout");
    expect(intents.textContent).not.toContain("runtime_failure");
    expect(screen.queryByText("switch_model")).toBeNull();
    expect(screen.queryByText("model_timeout")).toBeNull();
  });

  it("summarizes blocked orchestration contracts without expanding contract details", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        ...runDetail.events,
        {
          sequence: 8,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:08Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              orchestration_contracts: {
                schema_version: 1,
                items: [
                  {
                    contract_id: "research_step-to-final_response_step",
                    source_step_id: "research_step",
                    target_step_id: "final_response_step",
                    source_role_id: "researcher",
                    target_role_id: "final_synthesizer",
                    handoff_kind: "step_dependency",
                    status: "planned",
                    required_output_fields: ["status", "summary", "evidence"],
                    ready_status: "done",
                    blocking_statuses: ["blocked", "needs_user"],
                    recovery_hint: "retry_blocked_contract_chain",
                  },
                ],
                truncated: false,
              },
            },
          },
        },
        {
          sequence: 9,
          kind: "step.failed",
          message: "research failed",
          created_at: "2026-08-20T00:00:09Z",
          actor: "researcher",
          participants: [],
          step_id: "research_step",
          payload: {
            error_code: "runtime.failed",
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("已记录契约")).not.toBeNull();
    expect(within(summary).getByText("1 个契约，阻塞 1")).not.toBeNull();
    expect(screen.queryByText(/research_step/)).toBeNull();
    expect(screen.queryByText(/research_step-to-final_response_step/)).toBeNull();
  });

  it("shows a compact recovery hint when an orchestration contract is blocked", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        ...runDetail.events,
        {
          sequence: 8,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:08Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              orchestration_contracts: {
                schema_version: 1,
                items: [
                  {
                    contract_id: "research_step-to-final_response_step",
                    source_step_id: "research_step",
                    target_step_id: "final_response_step",
                    source_role_id: "researcher",
                    target_role_id: "final_synthesizer",
                    handoff_kind: "step_dependency",
                    status: "blocked",
                    required_output_fields: ["status", "summary", "evidence"],
                    ready_status: "done",
                    blocking_statuses: ["blocked", "needs_user"],
                    recovery_hint: "retry_blocked_contract_chain",
                  },
                ],
                truncated: false,
              },
            },
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const diagnostics = await screen.findByLabelText("故障诊断");
    expect(within(diagnostics).getByText("契约恢复提示")).not.toBeNull();
    expect(within(diagnostics).getByText("1 个契约阻塞")).not.toBeNull();
    expect(within(diagnostics).getByText("按契约提示只重试阻塞角色链路，保留已完成产物和步骤。")).not.toBeNull();
    expect(screen.queryByText(/research_step/)).toBeNull();
    expect(screen.queryByText(/research_step-to-final_response_step/)).toBeNull();
  });

  it("uses the latest orchestration contract snapshot before showing recovery hints", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        ...runDetail.events,
        {
          sequence: 8,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:08Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              orchestration_contracts: {
                schema_version: 1,
                items: [
                  {
                    contract_id: "research_step-to-final_response_step",
                    source_step_id: "research_step",
                    target_step_id: "final_response_step",
                    source_role_id: "researcher",
                    target_role_id: "final_synthesizer",
                    handoff_kind: "step_dependency",
                    status: "blocked",
                    required_output_fields: ["status", "summary", "evidence"],
                    ready_status: "done",
                    blocking_statuses: ["blocked", "needs_user"],
                    recovery_hint: "retry_blocked_contract_chain",
                  },
                ],
                truncated: false,
              },
            },
          },
        },
        {
          sequence: 9,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:09Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              orchestration_contracts: {
                schema_version: 1,
                items: [
                  {
                    contract_id: "research_step-to-final_response_step",
                    source_step_id: "research_step",
                    target_step_id: "final_response_step",
                    source_role_id: "researcher",
                    target_role_id: "final_synthesizer",
                    handoff_kind: "step_dependency",
                    status: "done",
                    required_output_fields: ["status", "summary", "evidence"],
                    ready_status: "done",
                    blocking_statuses: ["blocked", "needs_user"],
                    recovery_hint: "retry_blocked_contract_chain",
                  },
                ],
                truncated: false,
              },
            },
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("1 个契约")).not.toBeNull();
    expect(within(summary).queryByText(/阻塞/)).toBeNull();
    expect(screen.queryByLabelText("故障诊断")).toBeNull();
    expect(screen.queryByText(/retry_blocked_contract_chain/)).toBeNull();
  });

  it("uses the latest orchestration contract snapshot for truncation state", async () => {
    const detailedRun: RunDetail = {
      ...runDetail,
      events: [
        ...runDetail.events,
        {
          sequence: 8,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:08Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              orchestration_contracts: {
                schema_version: 1,
                items: [
                  {
                    contract_id: "research_step-to-final_response_step",
                    source_step_id: "research_step",
                    target_step_id: "final_response_step",
                    source_role_id: "researcher",
                    target_role_id: "final_synthesizer",
                    handoff_kind: "step_dependency",
                    status: "planned",
                    required_output_fields: ["status", "summary", "evidence"],
                    ready_status: "done",
                    blocking_statuses: ["blocked", "needs_user"],
                  },
                ],
                truncated: true,
              },
            },
          },
        },
        {
          sequence: 9,
          kind: "step.started",
          message: "main_agent_plan",
          created_at: "2026-08-20T00:00:09Z",
          actor: "main_agent",
          participants: [],
          step_id: "main_agent_plan",
          payload: {
            model_execution_plan: {
              schema_version: 1,
              orchestration_contracts: {
                schema_version: 1,
                items: [
                  {
                    contract_id: "research_step-to-final_response_step",
                    source_step_id: "research_step",
                    target_step_id: "final_response_step",
                    source_role_id: "researcher",
                    target_role_id: "final_synthesizer",
                    handoff_kind: "step_dependency",
                    status: "planned",
                    required_output_fields: ["status", "summary", "evidence"],
                    ready_status: "done",
                    blocking_statuses: ["blocked", "needs_user"],
                  },
                ],
                truncated: false,
              },
            },
          },
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(detailedRun);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const summary = await screen.findByRole("status", { name: "模型结果摘要" });
    expect(within(summary).getByText("1 个契约")).not.toBeNull();
    expect(within(summary).queryByText(/已截断/)).toBeNull();
  });

  it("deduplicates generated downloads that reuse the same file URL", async () => {
    const duplicatedDownload = {
      id: "artifact-final-wrapper-2",
      kind: "tool_result",
      title: "最终脚本产物",
      text: null,
      filename: "final-script.md",
      mime_type: "text/markdown",
      size_bytes: 2048,
      sha256: "a".repeat(64),
      download_url: "/api/v1/admin/artifacts/final-script.md",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) {
          return jsonResponse({
            ...runDetail,
            artifacts: [...runDetail.artifacts, duplicatedDownload],
          });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByRole("heading", { name: "运行详情" })).not.toBeNull();
    const archive = screen.getByRole("heading", { name: "产物" }).closest("article");
    expect(archive).not.toBeNull();
    expect(within(archive as HTMLElement).getAllByRole("button", { name: /下载 final-script\.md/ })).toHaveLength(1);
  });

  it("allows sandbox capability approvals from the run detail page", async () => {
    const user = userEvent.setup();
    const approvalRun = {
      ...runDetail,
      status: "waiting_approval",
      version: 0,
      explicit_details: {
        ...runDetail.explicit_details,
        approval_kind: "capability_tool",
        approval_id: "approval-sandbox-1",
        version: "7",
      },
      failure_diagnostics: [
        {
          category: "approval",
          stage: "approval.requested",
          reason: "project.generate_zip 需要沙箱权限。",
          recommendation: "确认本次受限工具调用后继续。",
          sequence: 2,
          actor: "implementer",
          step_id: "build-zip",
          tool_name: "project.generate_zip",
          approval_id: "approval-sandbox-1",
        },
      ],
    } satisfies RunDetail;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "https://agent-hub.test").pathname;
      if (path === "/api/v1/auth/me") {
        return jsonResponse({
          user_id: "11111111-1111-4111-8111-111111111111",
          tenant_id: "33333333-3333-4333-8333-333333333333",
          username: "admin",
          role: "super_admin",
          permissions: ["*"],
        });
      }
      if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(approvalRun);
      if (path === `/api/v1/admin/runs/${runId}/approve-capability`) {
        expect(JSON.parse(String(init?.body))).toEqual({
          approval_id: "approval-sandbox-1",
          version: 7,
        });
        return jsonResponse({ ...approvalRun, status: "queued", version: 8 });
      }
      return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const card = await screen.findByRole("status", { name: "沙箱权限确认" });
    expect(within(card).getByText("project.generate_zip 需要沙箱权限。")).not.toBeNull();
    await user.click(within(card).getByRole("button", { name: "允许一次" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/approve-capability"), expect.any(Object)));
  });

  it("rejects sandbox capability approvals from the run detail page", async () => {
    const user = userEvent.setup();
    const approvalRun = {
      ...runDetail,
      status: "waiting_approval",
      explicit_details: {
        ...runDetail.explicit_details,
        approval_kind: "capability_tool",
        approval_id: "approval-sandbox-2",
      },
      events: [
        ...runDetail.events,
        {
          sequence: 2,
          kind: "approval.requested",
          message: "approval.requested",
          summary: "当前工具调用需要授权。",
          created_at: "2026-08-20T00:00:02Z",
          actor: "implementer",
          participants: [],
          tool_name: "project.generate_zip",
          step_id: "build-zip",
          action: "生成可下载 zip",
          decision: null,
          approval_id: "approval-sandbox-2",
          payload: {},
        },
      ],
    } satisfies RunDetail;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "https://agent-hub.test").pathname;
      if (path === "/api/v1/auth/me") {
        return jsonResponse({
          user_id: "11111111-1111-4111-8111-111111111111",
          tenant_id: "33333333-3333-4333-8333-333333333333",
          username: "admin",
          role: "super_admin",
          permissions: ["*"],
        });
      }
      if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(approvalRun);
      if (path === `/api/v1/admin/runs/${runId}/reject-capability`) {
        expect(JSON.parse(String(init?.body))).toEqual({
          approval_id: "approval-sandbox-2",
          version: 1,
        });
        return jsonResponse({ ...approvalRun, status: "cancelled", version: 2 });
      }
      return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const card = await screen.findByRole("status", { name: "沙箱权限确认" });
    expect(within(card).getByText("生成可下载 zip")).not.toBeNull();
    await user.click(within(card).getByRole("button", { name: "拒绝" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/reject-capability"), expect.any(Object)));
  });

  it("does not show sandbox controls for non-capability waiting approvals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) {
          return jsonResponse({
            ...runDetail,
            status: "waiting_approval",
            explicit_details: {
              ...runDetail.explicit_details,
              approval_kind: "temporary_agent",
              approval_id: "approval-temporary-agent",
            },
          });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    expect(await screen.findByRole("heading", { name: "运行详情" })).not.toBeNull();
    expect(screen.queryByRole("status", { name: "沙箱权限确认" })).toBeNull();
  });

  it("refreshes the open Agent action drawer when a newer event arrives for the same source", async () => {
    const user = userEvent.setup();
    const initialSummary = "初始动作摘要";
    const refreshedSummary = "刷新后的动作摘要";
    let currentDetail: RunDetail = {
      ...runDetail,
      status: "running",
      events: [
        {
          ...runDetail.events[0],
          sequence: 1,
          summary: initialSummary,
          created_at: "2026-08-20T00:00:01Z",
          actor: "writer",
          step_id: "write-final",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(currentDetail);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    const processCard = within(processSummary).getByRole("button", { name: /Agent 工作席/ });
    await user.click(processCard);

    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    await user.click(within(drawer).getByRole("button", { name: /初始动作摘要/ }));
    expect(within(drawer).getAllByText(initialSummary).length).toBeGreaterThan(0);
    expect(within(drawer).getAllByText("2026-08-20T00:00:01Z").length).toBeGreaterThan(0);

    currentDetail = {
      ...currentDetail,
      events: [
        ...currentDetail.events,
        {
          ...currentDetail.events[0],
          sequence: 2,
          summary: refreshedSummary,
          created_at: "2026-08-20T00:00:05Z",
        },
      ],
    };

    await waitFor(
      () => {
        expect(within(drawer).getAllByText(refreshedSummary).length).toBeGreaterThan(0);
        expect(within(drawer).getAllByText("2026-08-20T00:00:05Z").length).toBeGreaterThan(0);
      },
      { timeout: 2500 },
    );
  });

  it("shows newly arrived Agent action cards without closing an open drawer", async () => {
    const user = userEvent.setup();
    let currentDetail: RunDetail = {
      ...runDetail,
      status: "running",
      events: [
        {
          ...runDetail.events[0],
          sequence: 1,
          summary: "writer 初始动作",
          created_at: "2026-08-20T00:00:01Z",
          actor: "writer",
          step_id: "write-final",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            username: "admin",
            role: "super_admin",
            permissions: ["*"],
          });
        }
        if (path === `/api/v1/admin/runs/${runId}`) return jsonResponse(currentDetail);
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );

    render(<TestApp initialPath={`/runs/${runId}`} />);

    const processSummary = await screen.findByLabelText("Agent 集群动作");
    await user.click(within(processSummary).getByRole("button", { name: /Agent 工作席/ }));
    const drawer = await screen.findByRole("dialog", { name: "Agent 工作席详情" });
    await user.click(within(drawer).getByRole("button", { name: "实际动作" }));
    const workbenchActions = within(drawer).getByLabelText("Agent 工作席动作");
    expect(within(workbenchActions).getByRole("button", { name: /writer 初始动作/ })).not.toBeNull();

    currentDetail = {
      ...currentDetail,
      events: [
        ...currentDetail.events,
        {
          ...currentDetail.events[0],
          sequence: 2,
          summary: "reviewer 新加入动作",
          created_at: "2026-08-20T00:00:05Z",
          actor: "reviewer",
          step_id: "review-final",
        },
      ],
    };

    await waitFor(
      () => {
        expect(within(processSummary).queryByText("reviewer 新加入动作")).toBeNull();
        expect(within(workbenchActions).getByRole("button", { name: /reviewer 新加入动作/ })).not.toBeNull();
      },
      { timeout: 2500 },
    );
    expect(screen.getByRole("dialog", { name: "Agent 工作席详情" })).not.toBeNull();
  });
});
