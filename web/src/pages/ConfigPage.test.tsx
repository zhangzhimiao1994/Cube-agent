import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

const principal = {
  user_id: "11111111-1111-4111-8111-111111111111",
  tenant_id: "33333333-3333-4333-8333-333333333333",
  role: "super_admin",
};

const settings = {
  default_mode: "auto",
  default_workflow_id: null,
  default_agent_ids: [],
  log_level: "warning",
  hermes_enabled: true,
  safe_tools_enabled: true,
  require_approval_for_tools: true,
  allow_main_agent_override: false,
  allow_temporary_agents: false,
  multimedia_generation_enabled: false,
  openclaw_enabled: false,
  openclaw_mode: "ask",
  temporary_agent_policy:
    "主 Agent 发现角色池缺少必要能力时，必须先说明原因并取得用户确认，再临时加入子 Agent。",
  channel_entry: "web",
  attachment_retention_days: 7,
  attachment_max_mb: 25,
};

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("ConfigPage", () => {
  const requests: Array<{ body: unknown; method: string; path: string }> = [];

  beforeEach(() => {
    requests.length = 0;
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? "GET";
        if (init?.body) {
          requests.push({ path, method, body: JSON.parse(String(init.body)) });
        }
        if (path === "/api/v1/auth/me") {
          return jsonResponse(principal);
        }
        if (path === "/api/v1/admin/settings") {
          if (method === "PUT") return jsonResponse(JSON.parse(String(init?.body)));
          return jsonResponse(settings);
        }
        if (path === "/api/v1/admin/agents") {
          return jsonResponse([
            {
              id: "director",
              name: "导演",
              enabled: true,
              role: "导演",
              prompt: "负责选题、分镜和最终把关。",
              model: "main",
              skills: [],
            },
          ]);
        }
        if (path === "/api/v1/admin/workflows") {
          return jsonResponse([
            {
              id: "short-video-dispatch",
              name: "短视频派单",
              enabled: true,
              mode: "dispatch",
              task_type: "短视频",
              role_selection_policy: "按任务类型选择导演、文案、剪辑师。",
              agent_ids: ["director"],
              objective: "生产短视频方案",
              steps: ["拆解需求", "分派角色", "汇总产物"],
              deliverables: ["脚本", "分镜"],
              decision_policy: "主 Agent 汇总裁决",
            },
          ]);
        }
        if (path === "/api/v1/admin/models") {
          return jsonResponse([]);
        }
        if (path === "/api/v1/config/current") {
          return jsonResponse({
            id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            version: 3,
            status: "published",
            document: {
              models: {
                main: {
                  deployments: [{ provider: "deepseek", model: "deepseek-v4-flash" }],
                },
              },
              agents: [],
            },
            created_by: principal.user_id,
            created_at: "2026-08-08T00:00:00Z",
          });
        }
        if (path === "/api/v1/config/drafts" && method === "POST") {
          return jsonResponse(
            {
              id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
              version: 4,
              status: "draft",
              document: JSON.parse(String(init?.body)),
              created_by: principal.user_id,
              created_at: "2026-08-08T00:01:00Z",
            },
            { status: 201 },
          );
        }
        if (path === "/api/v1/config/drafts/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/publish") {
          return jsonResponse({
            id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            version: 4,
            status: "published",
            document: requests.at(-1)?.body,
            created_by: principal.user_id,
            created_at: "2026-08-08T00:01:00Z",
            notification_status: "sent",
          });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("loads system settings and saves production defaults through dedicated controls", async () => {
    const user = userEvent.setup();
    const view = render(<TestApp initialPath="/config" />);

    expect(await screen.findByRole("heading", { name: "系统设置" })).not.toBeNull();
    expect(screen.getByText("版本 3")).not.toBeNull();
    expect(view.container.querySelectorAll(".settings-shortcut-card")).toHaveLength(5);

    await user.selectOptions(screen.getByLabelText("默认运行模式"), "dispatch");
    await user.selectOptions(screen.getByLabelText("默认工作流"), "short-video-dispatch");
    await user.click(screen.getByLabelText(/导演/));
    await user.click(screen.getByLabelText("允许主 Agent 提出临场调整，执行前必须向用户核对"));
    await user.click(screen.getByLabelText("允许主 Agent 在能力不足时申请临时子 Agent"));
    await user.click(screen.getByTestId("multimedia-generation-toggle"));
    await user.click(screen.getByTestId("openclaw-toggle"));
    await user.selectOptions(screen.getByLabelText("OpenClaw 权限模式"), "read_only");
    fireEvent.change(screen.getByLabelText(/临时 Agent 补位规则/), {
      target: { value: "缺少专业能力时先申请临时 Agent，任务结束后询问是否永久保存。" },
    });
    await user.click(screen.getByRole("button", { name: "保存系统设置" }));

    expect((await screen.findByRole("status")).textContent).toContain("系统设置已保存");
    expect(requests.find((request) => request.path === "/api/v1/admin/settings")).toMatchObject({
      method: "PUT",
      body: {
        ...settings,
        default_mode: "dispatch",
        default_workflow_id: "short-video-dispatch",
        default_agent_ids: ["director"],
        allow_main_agent_override: true,
        allow_temporary_agents: true,
        multimedia_generation_enabled: true,
        openclaw_enabled: true,
        openclaw_mode: "read_only",
        temporary_agent_policy: "缺少专业能力时先申请临时 Agent，任务结束后询问是否永久保存。",
      },
    });
  });

  it("keeps advanced JSON publishing available with detailed parse errors", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/config" />);

    await user.click(await screen.findByText("高级：直接编辑生产配置 JSON"));
    const editor = screen.getByLabelText("配置 JSON") as HTMLTextAreaElement;
    expect(editor.value).toContain('"models"');

    fireEvent.change(editor, { target: { value: "{broken" } });
    await user.click(screen.getByRole("button", { name: "创建草稿并发布" }));

    expect((await screen.findByRole("alert")).textContent).toContain("JSON 解析失败");
  });
});
