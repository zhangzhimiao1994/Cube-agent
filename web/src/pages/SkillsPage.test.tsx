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
          });
        }
        if (path.endsWith("/approve") && init?.method === "POST") {
          const id = path.split("/").at(-2) ?? "";
          return jsonResponse({ ...skills.find((skill) => skill.id === id), status: "enabled" });
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
    await userEvent.click(screen.getByLabelText("全选待审批 Skill"));
    await userEvent.click(screen.getByRole("button", { name: "批量审批启用已选 Skill" }));

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
    await userEvent.click(screen.getByRole("button", { name: "批量删除已选 Skill" }));

    expect(window.confirm).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/deep-research",
        expect.objectContaining({ method: "DELETE" }),
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/skills/pdf",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
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
  });
});
