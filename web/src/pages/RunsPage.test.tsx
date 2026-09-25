import { readFileSync } from "node:fs";

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { RunDetail } from "../api/client";
import {
  agentInlineSummary,
  conversationCheckpoints,
  conversationWorkspaceFiles,
  conversationMessages,
  MessageBody,
  mergeConversationRuns,
  requestedPermissionsForSandbox,
  runConversationId,
  runDetailVersion,
  runProcessItems,
  processDetailValuePresentation,
  workbenchFileItems,
  workbenchActionDescriptor,
  workspacePreviewPath,
} from "./RunsPage";

const baseRun: RunDetail = {
  id: "22222222-2222-4222-8222-222222222222",
  status: "running",
  mode: "dispatch",
  version: 9,
  conversation_id: "conv-top-level",
  request: "test",
  created_at: "2026-09-02T00:00:00Z",
  queue_wait_ms: 0,
  capacity_wait_ms: 0,
  cost_usd: "0.00",
  events: [],
  artifacts: [],
  explicit_details: {},
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

describe("runConversationId", () => {
  it("falls back to the top-level conversation id when explicit details omit it", () => {
    expect(runConversationId(baseRun)).toBe("conv-top-level");
  });

  it("keeps explicit detail conversation id authoritative", () => {
    expect(
      runConversationId({
        ...baseRun,
        explicit_details: { conversation_id: "conv-explicit" },
      }),
    ).toBe("conv-explicit");
  });
});

describe("runDetailVersion", () => {
  it("prefers the top-level run version", () => {
    expect(
      runDetailVersion({
        ...baseRun,
        version: 9,
        explicit_details: { version: "3" },
      }),
    ).toBe(9);
  });

  it("falls back to explicit detail version for rolling upgrades", () => {
    expect(
      runDetailVersion({
        ...baseRun,
        version: 0,
        explicit_details: { version: "3" },
      }),
    ).toBe(3);
  });
});

describe("conversation ordering", () => {
  it("renders conversation turns by run creation time even when incoming data is out of order", () => {
    const laterRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "第二轮请求",
      created_at: "2026-09-02T00:02:00Z",
    };
    const earlierRun: RunDetail = {
      ...baseRun,
      request: "第一轮请求",
      created_at: "2026-09-02T00:01:00Z",
    };

    const messages = conversationMessages([laterRun, earlierRun]);

    expect(messages.filter((message) => message.id.endsWith("-request")).map((message) => message.body)).toEqual([
      "第一轮请求",
      "第二轮请求",
    ]);
  });

  it("keeps merged cached conversation runs in chronological order", () => {
    const laterRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "第二轮请求",
      created_at: "2026-09-02T00:02:00Z",
    };
    const earlierRun: RunDetail = {
      ...baseRun,
      request: "第一轮请求",
      created_at: "2026-09-02T00:01:00Z",
    };

    const merged = mergeConversationRuns([laterRun], [earlierRun, laterRun]);

    expect(merged.map((run) => run.request)).toEqual(["第一轮请求", "第二轮请求"]);
  });

  it("prefers fresh conversation snapshots over stale cached runs with the same id", () => {
    const cachedRun: RunDetail = {
      ...baseRun,
      request: "旧请求",
      artifacts: [{ id: "artifact-old", kind: "markdown", title: "旧产物", text: "旧回复" }],
    };
    const freshRun: RunDetail = {
      ...baseRun,
      request: "新请求",
      artifacts: [{ id: "artifact-new", kind: "markdown", title: "新产物", text: "新回复" }],
    };

    const merged = mergeConversationRuns([cachedRun], [freshRun]);

    expect(merged).toHaveLength(1);
    expect(merged[0].request).toBe("新请求");
    expect(merged[0].artifacts[0]?.text).toBe("新回复");
  });

  it("does not downgrade cached conversation runs with older incoming progress", () => {
    const cachedRun: RunDetail = {
      ...baseRun,
      version: 2,
      request: "更新后的请求",
      events: [
        {
          sequence: 2,
          kind: "step.completed",
          message: "step.completed",
          created_at: "2026-09-02T00:00:02Z",
          participants: [],
          payload: {},
        },
      ],
    };
    const staleIncomingRun: RunDetail = {
      ...baseRun,
      version: 1,
      request: "旧请求",
      events: [],
    };

    const merged = mergeConversationRuns([cachedRun], [staleIncomingRun]);

    expect(merged).toHaveLength(1);
    expect(merged[0].request).toBe("更新后的请求");
    expect(merged[0].version).toBe(2);
  });

  it("keeps runs without reliable timestamps after timestamped history", () => {
    const timestampedRun: RunDetail = {
      ...baseRun,
      request: "已有历史",
      created_at: "2026-09-02T00:01:00Z",
    };
    const pendingRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "新提交待写入时间",
      created_at: null,
    };

    const messages = conversationMessages([pendingRun, timestampedRun]);

    expect(messages.filter((message) => message.id.endsWith("-request")).map((message) => message.body)).toEqual([
      "已有历史",
      "新提交待写入时间",
    ]);
  });

  it("does not render download-only main artifacts as standalone attachment replies", () => {
    const run: RunDetail = {
      ...baseRun,
      status: "completed",
      artifacts: [
        {
          id: "artifact-main",
          kind: "tool_result",
          title: "main",
          filename: "project.zip",
          mime_type: "application/zip",
          size_bytes: 1024,
          sha256: "abc",
          download_url: "/api/v1/runs/run/artifacts/artifact-main/download",
          presentation: "final_attachment",
        },
      ],
    };

    const messages = conversationMessages([run]);

    expect(messages.map((message) => message.title)).toEqual(["你"]);
  });

  it("keeps final artifacts with text as normal assistant replies", () => {
    const run: RunDetail = {
      ...baseRun,
      status: "completed",
      artifacts: [
        {
          id: "artifact-main",
          kind: "tool_result",
          title: "main",
          text: "插件已完成构建。\n\n核心设计：后台解析文档，前台搜索。",
          filename: "project.zip",
          mime_type: "application/zip",
          size_bytes: 1024,
          sha256: "abc",
          download_url: "/api/v1/runs/run/artifacts/artifact-main/download",
          presentation: "final_attachment",
        },
      ],
    };

    const messages = conversationMessages([run]);

    expect(messages.map((message) => message.title)).toEqual(["你", "回复"]);
    expect(messages[1].body).toContain("插件已完成构建");
    expect(messages[1].artifact?.filename).toBe("project.zip");
  });

  it("builds compact jump checkpoints from user questions", () => {
    const runs: RunDetail[] = [
      {
        ...baseRun,
        id: "11111111-1111-4111-8111-111111111111",
        request: "需要编写一个浏览器插件，不触发切屏读取 office 文档，并可以进行搜索查询",
        created_at: "2026-09-02T00:01:00Z",
      },
      {
        ...baseRun,
        id: "33333333-3333-4333-8333-333333333333",
        request: "继续优化 UI 交互，重点检查文件预览和配置页面",
        created_at: "2026-09-02T00:02:00Z",
      },
    ];

    expect(conversationCheckpoints(conversationMessages(runs))).toEqual([
      {
        id: "11111111-1111-4111-8111-111111111111-request",
        label: "需要编写一个浏览器插件，不触发切屏读取 office 文档，并...",
        index: 1,
      },
      {
        id: "33333333-3333-4333-8333-333333333333-request",
        label: "继续优化 UI 交互，重点检查文件预览和配置页面",
        index: 2,
      },
    ]);
  });

  it("orders process cards by event sequence when backend events arrive out of order", () => {
    const outOfOrderRun: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 2,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-09-02T00:00:02Z",
          actor: "engineer",
          participants: [],
          tool_name: "artifact_writer",
          step_id: "write-output",
          action: null,
          decision: null,
          payload: { result: "第二步产物" },
        },
        {
          sequence: 1,
          kind: "step.started",
          message: "step.started",
          created_at: "2026-09-02T00:00:01Z",
          actor: "main_agent",
          participants: [],
          tool_name: null,
          step_id: "plan",
          action: null,
          decision: null,
          payload: { task: "第一步规划" },
        },
      ],
    };

    const items = runProcessItems(outOfOrderRun, new Map());

    expect(items.flatMap((item) => (item.createdAt ? [item.createdAt] : []))).toEqual([
      "2026-09-02T00:00:01Z",
      "2026-09-02T00:00:02Z",
    ]);
  });

  it("surfaces runtime recovery without checkpoint internals in chat workbench process items", () => {
    const recoveredRun: RunDetail = {
      ...baseRun,
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
          sequence: 1,
          kind: "runtime.recovered",
          message: "runtime recovered from checkpoint",
          created_at: "2026-09-02T00:00:01Z",
          actor: "main_agent",
          participants: [],
          tool_name: null,
          step_id: "runtime-recovery",
          action: null,
          decision: null,
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
    };

    const items = runProcessItems(recoveredRun, new Map());
    const recovery = items.find((item) => item.badge === "断点续跑");

    expect(recovery).toBeTruthy();
    expect(`${recovery?.title} ${recovery?.message}`).toContain("恢复完成");
    expect(`${recovery?.title} ${recovery?.message}`).toContain("2/5 步");
    expect(`${recovery?.title} ${recovery?.message}`).toContain("模型状态：异常 1，已完成 2");
    expect(`${recovery?.title} ${recovery?.message}`).toContain("工具状态：进行中 1");
    expect(`${recovery?.title} ${recovery?.message}`).toContain("审查产物 1");
    expect(JSON.stringify(recovery?.rows)).not.toContain("checkpoint-00000000-0000-4000-8000-000000000001");
    expect(JSON.stringify(recovery?.rows)).not.toContain("checkpoint_id");
  });

  it("surfaces tool lifecycle fallback actions when tool events are absent", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [],
      tool_lifecycle: [
        {
          tool_call_id: "tool-terminal-1",
          tool_name: "run_safe_command",
          status: "completed",
          operation_kind: "terminal",
          actor: "implementer",
          step_id: "install-deps",
          started_sequence: 10,
          terminal_sequence: 12,
          sequences: [10, 11, 12],
          approval_id: null,
          replay_safe: true,
          argument_bytes: 82,
          output_bytes: 4096,
          exit_code: 0,
          artifact_id: null,
          failure_kind: null,
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const terminal = items.find((item) => item.badge === "运行终端");
    const rows = terminal?.rows.map((row) => `${row.label}:${row.value}`).join("\n") ?? "";

    expect(terminal?.message).toBe("运行终端 已完成");
    expect(terminal?.sourceActor).toBe("implementer");
    expect(terminal?.sourceStepId).toBe("install-deps");
    expect(rows).toContain("事件范围:#10-#12");
    expect(rows).toContain("退出码:0");
  });

  it("projects nested discussion traces into chat workbench process rows", () => {
    const discussionRun: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "discussion.completed",
          message: "discussion.completed",
          summary: "多角色完成方案讨论",
          created_at: "2026-09-02T00:00:01Z",
          actor: "main_agent",
          participants: ["planner", "critic"],
          payload: {
            discussion_trace: {
              participants: ["planner", "critic"],
              member_statements: {
                planner: "建议先生成计划文件和架构图谱，再进入实现。",
                critic: "担心直接实现会遗漏测试标准，需要先补验收清单。",
              },
              disagreement_summary: "是否立即实现存在分歧，风险是跳过验收。",
              verification_steps: ["核对技能规则", "读取约束"],
              final_decision: "先完成计划和验收清单，再派发实现任务。",
            },
          },
        },
      ],
    };

    const item = runProcessItems(discussionRun, new Map()).find((candidate) => candidate.badge === "讨论过程");
    const rows = item?.rows.map((row) => `${row.label}:${row.value}`).join("\n") ?? "";

    expect(rows).toContain("Planner意见:建议先生成计划文件和架构图谱");
    expect(rows).toContain("分歧与风险:是否立即实现存在分歧");
    expect(rows).toContain("求证与验证:核对技能规则、读取约束");
    expect(rows).toContain("最终决策:先完成计划和验收清单");
  });
});

describe("MessageBody", () => {
  it("keeps long content compact while allowing expand and full copy", async () => {
    const longText = Array.from({ length: 22 }, (_item, index) => `line-${index + 1}: long architecture detail`)
      .join("\n");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<MessageBody text={longText} title="回复" />);

    expect(screen.queryByText(/line-22/)).toBeNull();
    expect(screen.getByRole("button", { name: "展开全文" })).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "复制全文" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(longText));
    expect(screen.getByRole("button", { name: "已复制" })).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "展开全文" }));
    expect(screen.getByText(/line-22: long architecture detail/)).not.toBeNull();
  });

  it("renders fenced code blocks as categorized copyable blocks", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<MessageBody title="回复" text={"快速定位\n```bash\nssh -vvv user@host\nnc -v host 22\n```\n完成"} />);

    expect(screen.getByText("bash")).not.toBeNull();
    expect(screen.getByText(/ssh -vvv user@host/)).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "复制 bash 代码" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("ssh -vvv user@host\nnc -v host 22"));
    expect(screen.getByRole("button", { name: "已复制 bash 代码" })).not.toBeNull();
  });
});

describe("agentInlineSummary", () => {
  it("does not disable touch scrolling while the workbench owns nested mobile drawers", () => {
    const source = readFileSync("src/pages/RunsPage.tsx", "utf8");

    expect(source).not.toContain('document.body.style.touchAction = "none"');
  });

  it("keeps the closed conversation history drawer out of the visible interaction tree", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");

    expect(stylesCss).toMatch(
      /\.conversation-list\s*{[\s\S]*pointer-events:\s*none;[\s\S]*transform:\s*translateX\(calc\(100% \+ 1\.2rem\)\);[\s\S]*visibility:\s*hidden;/,
    );
    expect(stylesCss).toMatch(
      /\.history-drawer-open \.conversation-list\s*{[\s\S]*pointer-events:\s*auto;[\s\S]*transform:\s*translateX\(0\);[\s\S]*visibility:\s*visible;/,
    );
  });

  it("keeps conversation file previews scrollable when mobile drawers lock page scrolling", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");

    expect(stylesCss).toMatch(
      /\.conversation-file-preview-drawer\s*{[\s\S]*display:\s*grid;[\s\S]*grid-template-rows:\s*auto auto minmax\(0,\s*1fr\);[\s\S]*height:\s*min\(98dvh,\s*1040px\);[\s\S]*overflow:\s*hidden;/,
    );
    expect(stylesCss).toMatch(
      /\.conversation-file-preview-drawer \.agent-workbench-file-preview\s*{[\s\S]*min-height:\s*0;[\s\S]*overflow-y:\s*auto;[\s\S]*overscroll-behavior:\s*contain;/,
    );
    expect(stylesCss).toMatch(
      /\.conversation-file-preview-drawer \.agent-workbench-file-code\s*{[\s\S]*max-height:\s*none;[\s\S]*min-height:\s*0;/,
    );
  });

  it("opens process detail expansions as full reading sheets instead of cramped cards", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");
    const source = readFileSync("src/pages/RunsPage.tsx", "utf8");

    expect(source).toContain('shouldCollapse && expanded ? " is-expanded"');
    expect(stylesCss).toMatch(
      /\.process-detail-modal\s*{[\s\S]*height:\s*min\(96dvh,\s*1040px\);[\s\S]*width:\s*min\(98vw,\s*1280px\);/,
    );
    expect(stylesCss).toMatch(
      /\.process-detail-modal:has\(\.bounded-text-block\.is-expanded\)\s*{[\s\S]*height:\s*min\(98dvh,\s*1120px\);[\s\S]*width:\s*min\(99vw,\s*1360px\);/,
    );
    expect(stylesCss).toMatch(
      /\.bounded-text-block\.is-expanded pre\s*{[\s\S]*max-height:\s*none;/,
    );
    expect(stylesCss).toMatch(
      /@media \(max-width: 640px\)[\s\S]*\.process-detail-modal-backdrop\s*{[\s\S]*align-items:\s*stretch;[\s\S]*padding:\s*env\(safe-area-inset-top\) 0 0;/,
    );
    expect(stylesCss).toMatch(
      /@media \(max-width: 640px\)[\s\S]*\.process-detail-modal\s*{[\s\S]*height:\s*calc\(100dvh - env\(safe-area-inset-top\)\);[\s\S]*width:\s*100vw;/,
    );
  });

  it("closes the history drawer when starting a new blank conversation", () => {
    const source = readFileSync("src/pages/RunsPage.tsx", "utf8");
    const startNewConversationMatch = source.match(/function startNewConversation\(\) \{[\s\S]*?\n  \}/);

    expect(startNewConversationMatch?.[0]).toContain("setHistoryOpen(false)");
  });

  it("keeps approval cards cancellable when an action would otherwise proceed", () => {
    const source = readFileSync("src/pages/RunsPage.tsx", "utf8");

    expect(source).toContain("cancelProjectPreflightApproval");
    expect(source).toContain("取消预检");
    expect(source).toContain("cancelOpenClawApproval");
    expect(source).toContain("取消操作建议");
  });

  it("explains scheduled context-loading agents with a compact action summary", () => {
    expect(
      agentInlineSummary({
        id: "context_loader",
        name: "Context Loader",
        role: "Context Loader",
        model: "默认模型",
        summary: "等待执行分配任务",
        purpose: "",
        taskInputs: [],
        dependencyInputs: [],
        toolInputs: [],
        status: "已安排",
      }),
    ).toBe("加载上下文与约束");
  });
});

describe("workspace and sandbox submission helpers", () => {
  it("groups current conversation downloadable files by final and intermediate artifacts", () => {
    const firstRun: RunDetail = {
      ...baseRun,
      id: "11111111-1111-4111-8111-111111111111",
      created_at: "2026-09-02T00:01:00Z",
      artifacts: [
        {
          id: "final-zip",
          kind: "tool_result",
          title: "源码包",
          text: null,
          filename: "demo.zip",
          mime_type: "application/zip",
          size_bytes: 128,
          sha256: "a".repeat(64),
          download_url: "/api/v1/workspaces/projects/project/sessions/session/bundle/download",
          presentation: "final_attachment",
        },
      ],
      events: [
        {
          sequence: 1,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-09-02T00:01:30Z",
          participants: [],
          payload: {},
          artifact: {
            id: "draft-md",
            kind: "tool_result",
            title: "草稿",
            text: null,
            filename: "draft.md",
            mime_type: "text/markdown",
            size_bytes: 16,
            sha256: "b".repeat(64),
            download_url:
              "/api/v1/workspaces/projects/project/sessions/session/files/download?path=draft.md",
            presentation: "step_detail",
          },
        },
      ],
    };
    const secondRun: RunDetail = {
      ...baseRun,
      id: "22222222-2222-4222-8222-222222222223",
      created_at: "2026-09-02T00:02:00Z",
      artifacts: [
        {
          id: "final-zip-copy",
          kind: "tool_result",
          title: "源码包副本",
          text: null,
          filename: "demo.zip",
          mime_type: "application/zip",
          size_bytes: 128,
          sha256: "a".repeat(64),
          download_url: "/api/v1/workspaces/projects/project/sessions/session/bundle/download",
          presentation: "final_attachment",
        },
      ],
    };

    const files = conversationWorkspaceFiles([secondRun, firstRun]);

    expect(files.final.map((artifact) => artifact.filename)).toEqual(["demo.zip"]);
    expect(files.intermediate.map((artifact) => artifact.filename)).toEqual(["draft.md"]);
  });

  it("projects event workspace files into the workbench file list", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "创建文件 src/app.ts",
          created_at: "2026-09-02T00:01:30Z",
          participants: [],
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
                text: "raw file text should not be used",
              },
            ],
          },
        },
      ],
    };

    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, []);

    expect(files.map((file) => file.path)).toEqual(["src/app.ts"]);
    expect(files[0]?.operation).toBe("创建文件");
    expect(files[0]?.text).toBe("");
  });

  it("links workspace file previews back to the agent action that produced them", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "implementer 创建文件 src/app.ts",
          created_at: "2026-09-02T00:01:30Z",
          actor: "implementer",
          participants: [],
          tool_name: "workspace.write_file",
          step_id: "write-app",
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                operation_kind: "file_create",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, items);

    expect(files[0]?.source?.sourceActor).toBe("implementer");
    expect(files[0]?.source?.sourceStepId).toBe("write-app");
    expect(files[0]?.source?.message).toContain("src/app.ts");
  });

  it("keeps repeated workspace file operations on the same path tied to each producing action", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "implementer 创建文件 src/app.ts",
          created_at: "2026-09-02T00:01:30Z",
          actor: "implementer",
          participants: [],
          tool_name: "workspace.write_file",
          step_id: "write-app",
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                operation_kind: "file_create",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
        },
        {
          sequence: 2,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "tester 编辑文件 src/app.ts",
          created_at: "2026-09-02T00:02:30Z",
          actor: "tester",
          participants: [],
          tool_name: "workspace.patch_file",
          step_id: "patch-app",
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                operation_kind: "file_edit",
                mime_type: "text/typescript",
                size_bytes: 768,
                sha256: "c".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, items);

    expect(files.map((file) => `${file.operation}:${file.source?.sourceActor}:${file.source?.sourceStepId}`)).toEqual([
      "创建文件:implementer:write-app",
      "编辑文件:tester:patch-app",
    ]);
  });

  it("uses workspace file operation metadata before event text fallbacks", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "创建文件 src/app.ts",
          created_at: "2026-09-02T00:01:30Z",
          participants: [],
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                operation_kind: "file_edit",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
        },
      ],
    };

    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, []);

    expect(files.map((file) => `${file.filename}:${file.operation}`)).toEqual(["app.ts:编辑文件"]);
  });

  it("summarizes agent actions by concrete file operation and target path", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "implementer 写入源码文件",
          created_at: "2026-09-02T00:01:30Z",
          actor: "implementer",
          participants: [],
          tool_name: "workspace.write_file",
          step_id: "write-storage",
          payload: {
            workspace_files: [
              {
                path: "src/storage/cloud-drive.ts",
                filename: "cloud-drive.ts",
                operation_kind: "file_create",
                mime_type: "text/typescript",
                size_bytes: 2048,
                sha256: "d".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/storage/cloud-drive.ts",
              },
            ],
          },
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const action = items.find((item) => item.sourceActor === "implementer" && item.sourceStepId === "write-storage");
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, items);
    const descriptor = workbenchActionDescriptor(action!, files.filter((file) => file.source?.id === action?.id));

    expect(descriptor.operation).toBe("创建文件");
    expect(descriptor.target).toBe("src/storage/cloud-drive.ts");
    expect(descriptor.summary).toContain("写入源码文件");
    expect(descriptor.meta).toEqual(expect.arrayContaining(["implementer", "创建文件", "src/storage/cloud-drive.ts"]));
  });

  it("localizes backend English fallback summaries for workbench action rows", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "step.started",
          message: "architect recorded step.started",
          summary: "architect recorded step.started",
          created_at: "2026-09-02T00:01:30Z",
          actor: "architect",
          participants: [],
          step_id: "architect-step",
          payload: {},
        },
        {
          sequence: 2,
          kind: "harness.started",
          message: "Main Agent selected the runtime mode, roles, and models.",
          summary: "Main Agent selected the runtime mode, roles, and models.",
          created_at: "2026-09-02T00:02:30Z",
          actor: "main_agent",
          participants: [],
          payload: {},
        },
      ],
    };

    const items = runProcessItems(run, new Map([["architect", "林衡 · 审查"]]));
    const architect = items.find((item) => item.sourceActor === "architect");
    const mainAgent = items.find((item) => item.sourceActor === "main_agent");

    expect(workbenchActionDescriptor(architect!).target).toBe("林衡 · 审查 开始执行步骤");
    expect(workbenchActionDescriptor(mainAgent!).target).toBe("主 Agent 已选择运行模式、角色和模型");
  });

  it("classifies long detail values into bounded JSON and code blocks", () => {
    expect(processDetailValuePresentation({ label: "事件内容", value: '{"items":[{"id":"calculator.evaluate"}]}' })).toEqual({
      kind: "json",
      label: "json",
      copyLabel: "复制 json 内容",
      text: '{\n  "items": [\n    {\n      "id": "calculator.evaluate"\n    }\n  ]\n}',
      shouldCollapse: false,
    });
    expect(
      processDetailValuePresentation({
        label: "命令",
        value: "npm run build\nnpm test",
      }),
    ).toMatchObject({
      kind: "code",
      label: "bash",
      copyLabel: "复制 bash 内容",
      shouldCollapse: false,
    });
  });

  it("keeps multi-file action summaries tied to the clicked action instead of only global artifacts", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "tester 更新测试与配置",
          created_at: "2026-09-02T00:01:30Z",
          actor: "tester",
          participants: [],
          tool_name: "workspace.patch_file",
          step_id: "patch-tests",
          payload: {
            workspace_files: [
              {
                path: "tests/storage.test.ts",
                filename: "storage.test.ts",
                operation_kind: "file_edit",
                mime_type: "text/typescript",
                size_bytes: 1024,
                sha256: "e".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=tests/storage.test.ts",
              },
              {
                path: "vitest.config.ts",
                filename: "vitest.config.ts",
                operation_kind: "file_read",
                mime_type: "text/typescript",
                size_bytes: 640,
                sha256: "f".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=vitest.config.ts",
              },
            ],
          },
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const action = items.find((item) => item.sourceActor === "tester" && item.sourceStepId === "patch-tests");
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, items);
    const actionFiles = files.filter((file) => file.source?.id === action?.id);
    const descriptor = workbenchActionDescriptor(action!, actionFiles);

    expect(actionFiles.map((file) => `${file.operation}:${file.path}`)).toEqual([
      "编辑文件:tests/storage.test.ts",
      "读取文件:vitest.config.ts",
    ]);
    expect(descriptor.operation).toBe("编辑文件");
    expect(descriptor.target).toBe("tests/storage.test.ts");
  });

  it("maps sandbox profiles to bounded requested permissions", () => {
    expect(requestedPermissionsForSandbox("none")).toEqual([]);
    expect(requestedPermissionsForSandbox("read_only")).toEqual(["workspace.read"]);
    expect(requestedPermissionsForSandbox("restricted")).toEqual(["workspace.read", "command.run"]);
    expect(requestedPermissionsForSandbox("workspace_write")).toEqual([
      "workspace.read",
      "workspace.write",
      "command.run",
    ]);
  });

  it("previews the logical project session workspace path", () => {
    expect(workspacePreviewPath("Mofang Agent", "Conv Main 01")).toBe(
      "projects/mofang-agent/sessions/conv-main-01",
    );
  });
});
