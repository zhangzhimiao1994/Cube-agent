import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../app/router";

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const owner = {
  id: "11111111-1111-4111-8111-111111111111",
  tenant_id: "33333333-3333-4333-8333-333333333333",
  role: "super_admin",
};

const skills = [
  {
    id: "deep-research",
    name: "deep-research",
    status: "quarantined",
    scan_diff: ["manifest loaded"],
    requested_permissions: ["network:http"],
    source_filename: "deep-research.zip",
    package_version_id: "pkg-v2",
    content_sha256: "sha256-current",
    current_version_id: "version-2",
    versions: [
      {
        id: "version-1",
        source_filename: "deep-research-v1.zip",
        package_version_id: "pkg-v1",
        content_sha256: "sha256-old",
        created_at: "2026-08-14T01:00:00Z",
        is_current: false,
      },
      {
        id: "version-2",
        source_filename: "deep-research.zip",
        package_version_id: "pkg-v2",
        content_sha256: "sha256-current",
        created_at: "2026-08-15T01:00:00Z",
        is_current: true,
      },
    ],
  },
  {
    id: "docx",
    name: "docx",
    status: "quarantined",
    scan_diff: ["document tools"],
    requested_permissions: ["filesystem:workspace"],
  },
  {
    id: "pdf",
    name: "pdf",
    status: "enabled",
    scan_diff: [],
    requested_permissions: [],
  },
];

describe("SkillsPage", () => {
  let duplicateUploadAttempts = 0;

  beforeEach(() => {
    duplicateUploadAttempts = 0;
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "https://agent-hub.test");
        const path = url.pathname;
        const pathWithSearch = `${url.pathname}${url.search}`;
        expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer owner-token");
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: owner.id,
            tenant_id: owner.tenant_id,
            role: owner.role,
          });
        }
        if (path === "/api/v1/admin/skills" && (!init?.method || init.method === "GET")) {
          return jsonResponse(skills);
        }
        if (pathWithSearch === "/api/v1/admin/skills/upload" && init?.method === "POST") {
          const filename = (init.headers as Record<string, string>)["X-Agent-Hub-Skill-Filename"];
          if (filename === "duplicate-skill.zip") {
            duplicateUploadAttempts += 1;
            return jsonResponse(
              {
                error: {
                  code: "skill_version_choice_required",
                  message: "Skill already exists with different content",
                  details: { skill_id: "deep-research", name: "deep-research" },
                },
              },
              { status: 409 },
            );
          }
          return jsonResponse({
            filename: "all-skills.tar.gz",
            bundle: true,
            items: [
              {
                id: "research-writer",
                name: "research-writer",
                status: "scanned",
                scan_diff: ["SKILL.md detected"],
                requested_permissions: [],
              },
            ],
            skipped: [{ path: "invalid-skill", reason: "instruction skill contains nested archives" }],
          });
        }
        if (pathWithSearch === "/api/v1/admin/skills/upload?strategy=overwrite" && init?.method === "POST") {
          duplicateUploadAttempts += 1;
          return jsonResponse({
            filename: "duplicate-skill.zip",
            bundle: false,
            items: [
              {
                id: "deep-research",
                name: "deep-research",
                status: "scanned",
                scan_diff: ["overwrote current version"],
                requested_permissions: ["network:http"],
                source_filename: "duplicate-skill.zip",
                package_version_id: "pkg-v3",
                content_sha256: "sha256-overwrite",
                current_version_id: "version-3",
                versions: [
                  {
                    id: "version-3",
                    source_filename: "duplicate-skill.zip",
                    package_version_id: "pkg-v3",
                    content_sha256: "sha256-overwrite",
                    created_at: "2026-08-16T01:00:00Z",
                    is_current: true,
                  },
                ],
              },
            ],
            skipped: [],
          });
        }
        if (pathWithSearch === "/api/v1/admin/skills/upload?strategy=new_version" && init?.method === "POST") {
          duplicateUploadAttempts += 1;
          return jsonResponse({
            filename: "duplicate-skill.zip",
            bundle: false,
            items: [
              {
                id: "deep-research",
                name: "deep-research",
                status: "scanned",
                scan_diff: ["saved as new version"],
                requested_permissions: ["network:http"],
                source_filename: "duplicate-skill.zip",
                package_version_id: "pkg-v3",
                content_sha256: "sha256-new",
                current_version_id: "version-3",
                versions: [
                  {
                    id: "version-2",
                    source_filename: "deep-research.zip",
                    package_version_id: "pkg-v2",
                    content_sha256: "sha256-current",
                    created_at: "2026-08-15T01:00:00Z",
                    is_current: false,
                  },
                  {
                    id: "version-3",
                    source_filename: "duplicate-skill.zip",
                    package_version_id: "pkg-v3",
                    content_sha256: "sha256-new",
                    created_at: "2026-08-16T01:00:00Z",
                    is_current: true,
                  },
                ],
              },
            ],
            skipped: [],
          });
        }
        if (path === "/api/v1/admin/skills/deep-research/versions/version-1/activate" && init?.method === "POST") {
          const versionedSkill = skills[0];
          return jsonResponse({
            ...versionedSkill,
            current_version_id: "version-1",
            versions: (versionedSkill.versions ?? []).map((version) => ({
              ...version,
              is_current: version.id === "version-1",
            })),
          });
        }
        if (path === "/api/v1/admin/evolution-runs" && init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          return jsonResponse({
            id: "evolution_skill_creator_1",
            kind: body.kind,
            title: body.title,
            objective: body.objective,
            mode: body.mode,
            source_skill_ids: body.source_skill_ids,
            source_conversation_id: null,
            source_run_id: null,
            target_artifact_type: body.target_artifact_type,
            baseline_agent_id: body.baseline_agent_id,
            candidate_agent_ids: body.candidate_agent_ids,
            evaluator_agent_id: body.evaluator_agent_id,
            approval_policy: body.approval_policy,
            approval_status: "pending",
            approved_by: null,
            approved_at: null,
            approval_note: "",
            iteration_policy: body.iteration_policy,
            memory_policy: body.memory_policy,
            next_action: "request_approval",
            status: "waiting_approval",
            max_rounds: body.max_rounds,
            min_delta: body.min_delta,
            budget_tokens: body.budget_tokens,
            budget_minutes: body.budget_minutes,
            rubric: body.rubric,
            rounds: [],
            created_by: owner.id,
            created_at: "2026-08-15T01:00:00Z",
            updated_at: "2026-08-15T01:00:00Z",
            stop_reason: null,
          });
        }
        if (path.endsWith("/approve") && init?.method === "POST") {
          const id = path.split("/").at(-2) ?? "";
          return jsonResponse({ ...skills.find((skill) => skill.id === id), status: "enabled" });
        }
        if (path === "/api/v1/admin/skills/bulk-delete" && init?.method === "POST") {
          return jsonResponse({ deleted: JSON.parse(String(init.body)).ids, failed: [] });
        }
        if (path.includes("/api/v1/admin/skills/") && init?.method === "DELETE") {
          return jsonResponse({ status: "deleted" });
        }
        return jsonResponse({ error: { code: "not_found", message: "not found" } }, { status: 404 });
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("creates a grounded Skill Creator evolution task from the skills page", async () => {
    const fetchMock = vi.mocked(fetch);
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills" />);

    await screen.findByRole("heading", { name: "技能管理" });
    expect(screen.getByRole("region", { name: "创建 Skill 任务" })).not.toBeNull();
    await user.clear(screen.getByLabelText("Skill 方向"));
    await user.type(screen.getByLabelText("Skill 方向"), "自媒体视频 Skill");
    await user.clear(screen.getByLabelText("目标"));
    await user.type(screen.getByLabelText("目标"), "生成短视频选题、脚本、分镜和发布复盘流程。");
    await user.clear(screen.getByLabelText("资料来源"));
    await user.type(screen.getByLabelText("资料来源"), "检索平台规则、账号案例、爆款脚本和用户提供的历史素材。");
    await user.clear(screen.getByLabelText("验收任务"));
    await user.type(screen.getByLabelText("验收任务"), "用 2 个真实选题输出脚本和复盘指标，失败则继续迭代。");
    await user.click(screen.getByRole("button", { name: "创建并进入进化" }));

    await waitFor(() => {
      const request = fetchMock.mock.calls.find(([path, init]) => path === "/api/v1/admin/evolution-runs" && init?.method === "POST");
      expect(request).toBeTruthy();
      const body = JSON.parse(String(request?.[1]?.body));
      expect(body).toMatchObject({
        kind: "skill_distillation",
        title: "创建 自媒体视频 Skill",
        target_artifact_type: "skill",
        mode: "hybrid",
        baseline_agent_id: "main-agent",
        evaluator_agent_id: "evaluator-agent",
        approval_policy: "ask",
        iteration_policy: "score_gated",
        memory_policy: "summarize_between_rounds",
      });
      expect(body.objective).toContain("真实选题输出脚本");
      expect(body.objective).toContain("生成可安装的 SKILL.md");
      expect(body.rubric).toEqual(["资料真实性", "可执行 Skill 结构", "真实任务验收", "权限边界"]);
    });
    expect(await screen.findByText(/已创建进化任务：创建 自媒体视频 Skill/)).not.toBeNull();
  });
  it("supports selecting multiple skills and approving them in one action", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/skills" />);

    await screen.findByRole("heading", { name: "技能管理" });
    await userEvent.click(screen.getByLabelText("全选当前待审批 Skill"));
    await userEvent.click(screen.getByRole("button", { name: /批量审批待审批 Skill/ }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/deep-research/approve",
        expect.objectContaining({ method: "POST" }),
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/docx/approve",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("supports selecting multiple skills and deleting them after one confirmation", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/skills" />);

    await screen.findByRole("heading", { name: "技能管理" });
    const table = screen.getByRole("table", { name: "已上传 Skill" });
    await userEvent.click(within(table).getByLabelText("选择 Skill deep-research"));
    await userEvent.click(within(table).getByLabelText("选择 Skill pdf"));
    await userEvent.click(screen.getByRole("button", { name: /批量删除已选 Skill/ }));

    expect(window.confirm).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/bulk-delete",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ ids: ["deep-research", "pdf"] }),
        }),
      );
    });
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/admin/skills/deep-research",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("bulk actions only operate on selected visible skills", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/skills" />);

    await screen.findByRole("heading", { name: "技能管理" });
    await userEvent.type(screen.getByRole("textbox", { name: "按 Skill 请求权限筛选" }), "filesystem");
    await userEvent.click(screen.getByLabelText("全选当前待审批 Skill"));
    await userEvent.click(screen.getByRole("button", { name: /批量删除已选 Skill/ }));

    expect(window.confirm).toHaveBeenCalledWith("确认删除当前结果中已选的 1 个 Skill？删除后不会再分发给主 Agent 或子 Agent。");
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/bulk-delete",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ ids: ["docx"] }),
        }),
      );
    });
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/admin/skills/deep-research",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("filters uploaded skills by name, permissions, and scan details", async () => {
    render(<TestApp initialPath="/skills" />);

    await screen.findByRole("heading", { name: "技能管理" });
    await userEvent.type(screen.getByRole("textbox", { name: "按 Skill 请求权限筛选" }), "filesystem");

    expect(screen.getByText("docx")).not.toBeNull();
    expect(screen.queryByText("deep-research")).toBeNull();
    expect(screen.queryByText("pdf")).toBeNull();
    expect(screen.getByText("显示 1 / 3")).not.toBeNull();
  });

  it("sorts skills by column header", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills" />);

    const table = await screen.findByRole("table", { name: "已上传 Skill" });
    await user.click(screen.getByRole("button", { name: "状态排序" }));
    const dataRows = within(table).getAllByRole("row").slice(2).map((row) => row.textContent ?? "");
    const joinedRows = dataRows.join("\n");

    expect(joinedRows.indexOf("enabled")).toBeLessThan(joinedRows.indexOf("quarantined"));
  });

  it("uploads tar.gz skill archives with the matching archive content type", async () => {
    const fetchMock = vi.mocked(fetch);
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills" />);

    await screen.findByRole("heading", { name: "技能管理" });
    const file = new File(["skill-bytes"], "all-skills.tar.gz", { type: "application/gzip" });
    await user.upload(screen.getByLabelText("Skill 压缩包"), file);
    await user.click(screen.getByRole("button", { name: "上传并扫描" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/upload",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({
            "Content-Type": "application/gzip",
            "X-Agent-Hub-Skill-Filename": "all-skills.tar.gz",
          }),
        }),
      );
    });
    expect(await screen.findByText(/跳过 1 项/)).not.toBeNull();
    expect(screen.getByText(/invalid-skill/)).not.toBeNull();
  });

  it("asks how to handle same-name skill uploads before retrying with a version strategy", async () => {
    const fetchMock = vi.mocked(fetch);
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills" />);

    await screen.findByRole("heading", { name: "技能管理" });
    const file = new File(["new bytes"], "duplicate-skill.zip", { type: "application/zip" });
    await user.upload(screen.getByLabelText("Skill 压缩包"), file);
    await user.click(screen.getByRole("button", { name: "上传并扫描" }));

    expect(await screen.findByText(/deep-research 已存在且内容不同/)).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "保存为新版本" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/upload?strategy=new_version",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({
            "X-Agent-Hub-Skill-Filename": "duplicate-skill.zip",
          }),
        }),
      );
    });
    expect(duplicateUploadAttempts).toBe(2);
    expect(await screen.findByText(/已扫描 1 个 Skill/)).not.toBeNull();
  });

  it("shows skill version metadata and can activate a previous version", async () => {
    const fetchMock = vi.mocked(fetch);
    const user = userEvent.setup();
    render(<TestApp initialPath="/skills" />);

    const table = await screen.findByRole("table", { name: "已上传 Skill" });
    const deepResearchRow = within(table).getByText("deep-research").closest("tr");
    expect(deepResearchRow).not.toBeNull();
    expect(within(deepResearchRow as HTMLTableRowElement).getByText(/当前版本：version-2/)).not.toBeNull();
    expect(within(deepResearchRow as HTMLTableRowElement).getByText(/版本数：2/)).not.toBeNull();
    expect(within(deepResearchRow as HTMLTableRowElement).getByText(/deep-research.zip/)).not.toBeNull();

    await user.click(within(deepResearchRow as HTMLTableRowElement).getByRole("button", { name: "激活 version-1" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/deep-research/versions/version-1/activate",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });
});
