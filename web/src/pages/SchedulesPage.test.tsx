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

const existingSchedule = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "daily-report",
  status: "active",
  kind: "cron",
  mode: "dispatch",
  workflow_id: "daily_report",
  message: "fill daily report",
  timezone: "Asia/Shanghai",
  next_fire_at: "2026-08-13T01:00:00Z",
  run_at: null,
  cron: "0 9 * * *",
  misfire_policy: "fire_once",
  budget: 4096,
  metadata: { openclaw: "windows_desktop_report" },
};

describe("SchedulesPage", () => {
  const requests: Array<{ path: string; method: string; body: unknown }> = [];

  beforeEach(() => {
    requests.length = 0;
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "https://agent-hub.test").pathname;
        const method = init?.method ?? "GET";
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        if (method !== "GET") requests.push({ path, method, body });
        if (path === "/api/v1/auth/me") {
          return jsonResponse({
            user_id: "11111111-1111-4111-8111-111111111111",
            tenant_id: "33333333-3333-4333-8333-333333333333",
            role: "super_admin",
          });
        }
        if (path === "/api/v1/admin/schedules" && method === "GET") {
          return jsonResponse([existingSchedule]);
        }
        if (path === "/api/v1/admin/schedules" && method === "POST") {
          return jsonResponse(
            { ...existingSchedule, id: "22222222-2222-4222-8222-222222222222", ...body },
            { status: 201 },
          );
        }
        if (path === "/api/v1/admin/schedules/tick" && method === "POST") {
          return jsonResponse({ fired: ["22222222-2222-4222-8222-222222222222"] });
        }
        if (
          path === "/api/v1/admin/schedules/11111111-1111-4111-8111-111111111111" &&
          method === "DELETE"
        ) {
          return jsonResponse({ id: "11111111-1111-4111-8111-111111111111", deleted: true });
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

  it("creates weekly alarm-style scheduled tasks and ticks due work", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/schedules" />);

    await screen.findByRole("heading", { name: "计划任务" });
    expect(await screen.findByText("daily-report")).not.toBeNull();
    expect(screen.getByText("每天 09:00")).not.toBeNull();

    await user.clear(screen.getByLabelText("名称"));
    await user.type(screen.getByLabelText("名称"), "report-fill");
    await user.clear(screen.getByLabelText("执行指令"));
    await user.type(screen.getByLabelText("执行指令"), "Open browser and fill the report");
    await user.selectOptions(screen.getByLabelText("重复类型"), "weekly");
    await user.selectOptions(screen.getByLabelText("星期"), "4");
    await user.clear(screen.getByLabelText("执行时间"));
    await user.type(screen.getByLabelText("执行时间"), "09:00");
    await user.click(screen.getByRole("button", { name: "保存计划任务" }));

    await waitFor(() => {
      expect(requests).toContainEqual({
        path: "/api/v1/admin/schedules",
        method: "POST",
        body: {
          name: "report-fill",
          message: "Open browser and fill the report",
          mode: "dispatch",
          workflow_id: "scheduled_task",
          kind: "cron",
          cron: "0 9 * * 4",
          timezone: "Asia/Shanghai",
          misfire_policy: "fire_once",
          budget: 16384,
          metadata: { openclaw: "windows_desktop" },
        },
      });
    });

    await user.click(screen.getByRole("button", { name: "立即检查到期任务" }));

    await screen.findByText("已触发 1 个计划任务");
    expect(requests).toContainEqual({
      path: "/api/v1/admin/schedules/tick",
      method: "POST",
      body: { now: "2026-08-13T09:00:00+08:00" },
    });

    await user.click(screen.getByRole("button", { name: "删除计划任务" }));

    expect(window.confirm).toHaveBeenCalledWith("删除计划任务 daily-report?");
    await waitFor(() => {
      expect(requests).toContainEqual({
        path: "/api/v1/admin/schedules/11111111-1111-4111-8111-111111111111",
        method: "DELETE",
        body: undefined,
      });
    });
  });
});
