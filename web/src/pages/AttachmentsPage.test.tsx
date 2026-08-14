import { render, screen, waitFor } from "@testing-library/react";
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

describe("AttachmentsPage", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer owner-token");
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/runs/attachments" && (!init?.method || init.method === "GET")) {
          return jsonResponse({
            items: [
              {
                id: "att_0123456789abcdef0123456789abcdef",
                filename: "broken-skill-pack.zip",
                kind: "archive",
                content_type: "application/zip",
                size_bytes: 2048,
                sha256: "a".repeat(64),
                expires_at: "2026-08-20T00:00:00Z",
              },
              {
                id: "att_fedcba9876543210fedcba9876543210",
                filename: "old-context.md",
                kind: "context",
                content_type: "text/markdown",
                size_bytes: 1024,
                sha256: "b".repeat(64),
                expires_at: "2026-08-21T00:00:00Z",
              },
            ],
          });
        }
        if (
          path === "/api/v1/runs/attachments/att_0123456789abcdef0123456789abcdef" &&
          init?.method === "DELETE"
        ) {
          return jsonResponse({ id: "att_0123456789abcdef0123456789abcdef", deleted: true });
        }
        if (path === "/api/v1/runs/attachments/bulk-delete" && init?.method === "POST") {
          const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : { ids: [] };
          return jsonResponse({ deleted: body.ids, failed: [] });
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

  it("lists uploaded attachments and deletes a selected item", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/attachments" />);

    expect(await screen.findByRole("heading", { name: "附件管理" })).not.toBeNull();
    expect(screen.getByText("broken-skill-pack.zip")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "删除附件 broken-skill-pack.zip" }));

    expect(window.confirm).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/runs/attachments/att_0123456789abcdef0123456789abcdef",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
  });

  it("filters uploaded attachments by filename, type, and checksum", async () => {
    render(<TestApp initialPath="/attachments" />);

    expect(await screen.findByRole("heading", { name: "附件管理" })).not.toBeNull();
    await userEvent.type(screen.getByRole("searchbox", { name: "快速搜索附件" }), "context");

    expect(screen.getByText("old-context.md")).not.toBeNull();
    expect(screen.queryByText("broken-skill-pack.zip")).toBeNull();
    expect(screen.getByText("显示 1 / 2")).not.toBeNull();
  });

  it("bulk deletes selected attachments through one batch request", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<TestApp initialPath="/attachments" />);

    expect(await screen.findByText("broken-skill-pack.zip")).not.toBeNull();
    await userEvent.click(screen.getByRole("checkbox", { name: "全选当前附件结果" }));
    await userEvent.click(screen.getByRole("button", { name: /批量删除已选附件/ }));

    expect(window.confirm).toHaveBeenCalledWith("确认删除当前结果中已选的 2 个附件？删除后对话里不能再引用它们。");
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/runs/attachments/bulk-delete",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            ids: ["att_0123456789abcdef0123456789abcdef", "att_fedcba9876543210fedcba9876543210"],
          }),
        }),
      );
    });
  });

  it("clears the bulk selection when select all is clicked again", async () => {
    render(<TestApp initialPath="/attachments" />);

    const selectAll = await screen.findByRole("checkbox", { name: "全选当前附件结果" });
    const bulkDelete = screen.getByRole("button", { name: /批量删除已选附件/ }) as HTMLButtonElement;

    expect(bulkDelete.disabled).toBe(true);
    await userEvent.click(selectAll);
    expect(screen.getByText("当前结果已选 2")).not.toBeNull();
    expect(bulkDelete.disabled).toBe(false);
    await userEvent.click(selectAll);

    expect(screen.getByText("当前结果已选 0")).not.toBeNull();
    expect(bulkDelete.disabled).toBe(true);
  });
});
