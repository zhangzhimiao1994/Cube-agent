import { expect, test, type Page } from "@playwright/test";

const runId = "22222222-2222-4222-8222-222222222222";

async function mockAdminApi(page: Page) {
  let skillStatus: "missing" | "quarantined" | "enabled" = "missing";
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") {
      await route.fulfill({
        json: {
          user_id: "11111111-1111-4111-8111-111111111111",
          tenant_id: "00000000-0000-4000-8000-000000000001",
          role: "super_admin",
        },
      });
      return;
    }
    if (path === "/api/v1/admin/runs") {
      await route.fulfill({
        json: [
          {
            id: runId,
            status: "running",
            mode: "dispatch",
            queue_wait_ms: 120,
            capacity_wait_ms: 40,
            cost_usd: "0.0132",
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/skills" && request.method() === "GET") {
      await route.fulfill({
        json:
          skillStatus === "missing"
            ? []
            : [
                {
                  id: "safe-skill",
                  name: "safe-skill",
                  status: skillStatus,
                  scan_diff: ["added SKILL.md"],
                  requested_permissions: ["filesystem:read"],
                },
              ],
      });
      return;
    }
    if (path === "/api/v1/admin/skills/upload" && request.method() === "POST") {
      skillStatus = "scanned";
      await route.fulfill({
        json: {
          filename: "safe-skill.zip",
          bundle: false,
          items: [
            {
              id: "safe-skill",
              name: "safe-skill",
              status: "scanned",
              scan_diff: ["added SKILL.md"],
              requested_permissions: ["filesystem:read"],
            },
          ],
        },
      });
      return;
    }
    if (path === "/api/v1/admin/skills/safe-skill/approve") {
      skillStatus = "enabled";
      await route.fulfill({
        json: {
          id: "safe-skill",
          name: "safe-skill",
          status: "enabled",
          scan_diff: ["added SKILL.md"],
          requested_permissions: ["filesystem:read"],
        },
      });
      return;
    }
    if (path === "/api/v1/admin/mcp") {
      await route.fulfill({
        json: [
          {
            id: "filesystem",
            name: "Filesystem MCP",
            health: "healthy",
            allowed_tools: ["read_file"],
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/audit") {
      await route.fulfill({
        json: [
          {
            id: "audit-1",
            actor: "system",
            action: "config.publish",
            resource: "configuration",
            created_at: "2026-08-07T00:00:00Z",
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/logs") {
      await route.fulfill({
        json: [
          {
            id: "audit-log-1",
            category: "audit",
            level: "info",
            title: "config.publish",
            message: "configuration published",
            source: "audit",
            details: { resource: "configuration", actor: "system" },
            created_at: "2026-08-07T00:00:00Z",
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/hermes" && request.method() === "GET") {
      await route.fulfill({
        json: [
          {
            id: "hermes-1",
            category: "conversation",
            outcome: "success",
            lesson: "Use dispatch mode when the request has clear deliverables.",
            summary: "Matched dispatch mode for concrete deliverables.",
            run_id: runId,
            conversation_id: "conversation-1",
            confirmed_at: null,
            tags: ["dispatch"],
            weight: 3,
            created_at: "2026-08-07T00:00:00Z",
          },
        ],
      });
      return;
    }
    if (path === "/api/v1/admin/hermes/recommend") {
      await route.fulfill({
        json: {
          recommended_mode: "group_chat",
          recommended_model: "deepseek-chat",
          recommended_skills: ["architecture-review"],
          confidence: 0.7,
          reasons: ["Matched prior Hermes lesson."],
          requires_approval: false,
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: { error: "not_found" } });
  });
}

test("administrator uploads and approves a skill", async ({ page }) => {
  await mockAdminApi(page);
  await page.goto("/skills");
  await page.getByLabel("Skill 压缩包").setInputFiles("e2e/fixtures/safe-skill.zip");
  await page.getByRole("button", { name: "上传并扫描" }).click();
  await expect(page.getByRole("row", { name: /safe-skill/ }).getByRole("cell", { name: "scanned" })).toBeVisible();
  await page.getByRole("button", { name: "审批启用" }).click();
  await expect(page.getByRole("row", { name: /safe-skill/ }).getByRole("cell", { name: "enabled" })).toBeVisible();
});

test("administrator can inspect MCP and export safe audit view", async ({ page }) => {
  await mockAdminApi(page);
  await page.goto("/mcp");
  const mcpCard = page.getByRole("article").filter({ hasText: "Filesystem MCP" });
  await expect(mcpCard.getByText("healthy")).toBeVisible();
  await page.goto("/logs/audit");
  await expect(page.getByText("config.publish")).toBeVisible();
  await expect(page.getByText(/api_key|hidden_reasoning|fingerprint/i)).toHaveCount(0);
});

test("administrator reviews Hermes learning records", async ({ page }) => {
  await mockAdminApi(page);
  await page.goto("/hermes");
  await expect(page.getByRole("heading", { name: "Hermes 学习" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "Matched dispatch mode for concrete deliverables." })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认 Hermes 学习 hermes-1" })).toBeVisible();
});
