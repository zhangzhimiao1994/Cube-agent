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
    expect(screen.queryByText(longArtifactText)).toBeNull();
    expect(screen.queryByText(rawPayloadOutput)).toBeNull();
    expect(screen.queryByText(longUnsafeSummary)).toBeNull();
    expect(screen.queryByText(nestedSecret)).toBeNull();
    expect(screen.queryByText("输出内容")).toBeNull();
    expect(screen.queryByText("artifact_id")).toBeNull();

    const processSummary = screen.getByLabelText("Agent 集群动作");
    const processCard = within(processSummary).getByRole("button", { name: /SUMMARY_SHOULD_BE_COMPACTED/ });
    const controlsId = processCard.getAttribute("aria-controls");
    expect(controlsId).toBeTruthy();
    await user.click(processCard);

    const drawer = await screen.findByRole("dialog", { name: "Agent 动作详情" });
    const backdrop = document.querySelector(".process-drawer-backdrop");
    expect(backdrop?.parentElement).toBe(document.body);
    expect(controlsId ? document.getElementById(controlsId) : null).toBe(drawer);
    expect(document.body.style.overflow).toBe("hidden");
    expect(document.body.style.touchAction).toBe("none");
    expect(document.documentElement.style.overflow).toBe("hidden");
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
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Agent 动作详情" })).toBeNull());
    expect(document.body.style.overflow).toBe("");
    expect(document.body.style.touchAction).toBe("");
    expect(document.documentElement.style.overflow).toBe("");

    await user.click(processCard);
    expect(await screen.findByRole("dialog", { name: "Agent 动作详情" })).not.toBeNull();
    await user.click(document.querySelector(".process-drawer-backdrop") as HTMLElement);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Agent 动作详情" })).toBeNull());
    expect(document.body.style.overflow).toBe("");
    expect(document.body.style.touchAction).toBe("");
    expect(document.documentElement.style.overflow).toBe("");
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
    const processCard = within(processSummary).getByRole("button", { name: /初始动作摘要/ });
    await user.click(processCard);

    const drawer = await screen.findByRole("dialog", { name: "Agent 动作详情" });
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
    await user.click(within(processSummary).getByRole("button", { name: /writer 初始动作/ }));
    expect(await screen.findByRole("dialog", { name: "Agent 动作详情" })).not.toBeNull();

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
        expect(within(processSummary).getByRole("button", { name: /reviewer 新加入动作/ })).not.toBeNull();
      },
      { timeout: 2500 },
    );
    expect(screen.getByRole("dialog", { name: "Agent 动作详情" })).not.toBeNull();
  });
});
