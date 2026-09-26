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
      const teamSkill = {
        id: "team-skill",
        name: "team-skill",
        status: "enabled",
        scan_diff: [],
        requested_permissions: [],
        current_version_id: "team-version-1",
        versions: [{ id: "team-version-1", status: "enabled", is_current: true }],
      };
      await route.fulfill({
        json:
          skillStatus === "missing"
            ? [teamSkill]
            : [
                teamSkill,
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
    if (path === "/api/v1/admin/skill-sources" && request.method() === "GET") {
      await route.fulfill({
        json: [{
          id: "team-tap",
          name: "研发团队 Tap",
          repository_url: "https://github.com/example/team-tap",
          ref: "main",
          subdirectory: "skills",
          enabled: true,
          has_credential: false,
          expected_commit_sha: "1".repeat(40),
          expected_archive_sha256: "a".repeat(64),
          trust_state: "trusted",
          trusted_by: "admin",
          trusted_at: "2026-09-27T00:00:00Z",
          trust_reason: "已核验",
          sync_state: "succeeded",
          last_sync_id: "sync-1",
          resolved_commit_sha: "1".repeat(40),
          archive_sha256: "a".repeat(64),
          source_archive_bytes: 1024,
          last_synced_at: "2026-09-27T00:00:00Z",
          last_error: null,
          linked_skill_ids: ["team-skill"],
          active_revision_id: null,
        }],
      });
      return;
    }
    if (path === "/api/v1/admin/skill-sources/team-tap/revisions" && request.method() === "GET") {
      await route.fulfill({
        json: [{
          id: "revision-1",
          source_id: "team-tap",
          sync_id: "sync-1",
          commit_sha: "1".repeat(40),
          archive_sha256: "a".repeat(64),
          created_at: "2026-09-27T00:00:00Z",
          items: [{
            skill_name: "team-skill",
            version_id: "team-version-1",
            source_path: "skills/team-skill",
            content_sha256: "b".repeat(64),
            archive_sha256: "c".repeat(64),
          }],
          previous_revision_id: null,
          previous_active_mapping: {},
          active_mapping: { "team-skill": "team-version-1" },
          is_active: false,
        }],
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

test("team Tap revisions stay usable without horizontal overflow", async ({ page }) => {
  await mockAdminApi(page);
  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.goto("/skills");
    const source = page.getByRole("region", { name: "团队 Skill 来源" });
    await expect(source.getByText("研发团队 Tap")).toBeVisible();
    await source.getByRole("button", { name: "查看研发团队 Tap版本历史" }).click();
    const history = source.getByRole("region", { name: "研发团队 Tap版本历史" });
    await expect(history.getByText("已审批，可激活")).toBeVisible();
    await expect(history.getByText("team-skill", { exact: true })).toBeVisible();
    await expect(source.getByLabel("可信快照 ZIP")).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  }
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
