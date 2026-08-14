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
  beforeEach(() => {
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
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
        if (path === "/api/v1/admin/skills/upload" && init?.method === "POST") {
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
});
