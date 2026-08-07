import { expect, test, type Page } from "@playwright/test";

const runId = "22222222-2222-4222-8222-222222222222";

async function mockAdminApi(page: Page) {
  let skillStatus: "missing" | "quarantined" | "enabled" = "missing";
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/me") {
      await route.fulfill({
        json: { username: "owner", role: "super_admin", permissions: ["*"] },
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
    if (path === "/api/v1/admin/skills" && request.method() === "POST") {
      skillStatus = "quarantined";
      await route.fulfill({
        json: {
          id: "safe-skill",
          name: "safe-skill",
          status: "quarantined",
          scan_diff: ["added SKILL.md"],
          requested_permissions: ["filesystem:read"],
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
    if (path === "/api/v1/admin/hermes" && request.method() === "GET") {
      await route.fulfill({
        json: [
          {
            id: "hermes-1",
            outcome: "success",
            lesson: "Use dispatch mode when the request has clear deliverables.",
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
  await page.getByLabel("Skill ZIP").setInputFiles("e2e/fixtures/safe-skill.zip");
  await page.getByRole("button", { name: "Upload" }).click();
  await expect(page.getByText("Status: quarantined")).toBeVisible();
  await page.getByRole("button", { name: "Approve and enable" }).click();
  await expect(page.getByText("Status: enabled")).toBeVisible();
});

test("administrator can inspect MCP and export safe audit view", async ({ page }) => {
  await mockAdminApi(page);
  await page.goto("/mcp");
  await expect(page.getByText("Health: healthy")).toBeVisible();
  await page.goto("/audit");
  await expect(page.getByText("config.publish")).toBeVisible();
  await expect(page.getByText(/api_key|hidden_reasoning|fingerprint/i)).toHaveCount(0);
});

test("administrator asks Hermes for a safe recommendation", async ({ page }) => {
  await mockAdminApi(page);
  await page.goto("/hermes");
  await page.getByRole("button", { name: "Ask Hermes" }).click();
  await expect(page.getByText("Mode: group_chat")).toBeVisible();
  await expect(page.getByText("Requires approval: no")).toBeVisible();
});
