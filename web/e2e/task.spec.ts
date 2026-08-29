import { expect, test, type Page } from "@playwright/test";

const runId = "22222222-2222-4222-8222-222222222222";

async function mockRunApi(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
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
    if (path === `/api/v1/admin/runs/${runId}`) {
      await route.fulfill({
        json: {
          id: runId,
          status: "running",
          mode: "dispatch",
          queue_wait_ms: 120,
          capacity_wait_ms: 40,
          cost_usd: "0.0132",
          request: "Summarize current deployment readiness.",
          events: [
            {
              sequence: 1,
              kind: "queued",
              message: "Run accepted and queued.",
              created_at: "2026-08-07T00:00:00Z",
            },
          ],
          artifacts: [{ id: "artifact-1", kind: "markdown", title: "Readiness report" }],
          explicit_details: { routing: "dispatch mode selected explicitly" },
        },
      });
      return;
    }
    if (path === `/api/v1/admin/runs/${runId}/cancel`) {
      await route.fulfill({
        json: {
          id: runId,
          status: "cancelled",
          mode: "dispatch",
          queue_wait_ms: 120,
          capacity_wait_ms: 40,
          cost_usd: "0.0132",
          request: "Summarize current deployment readiness.",
          events: [],
          artifacts: [],
          explicit_details: {},
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: { error: "not_found" } });
  });
}

test("operator inspects run detail and cancels safely", async ({ page }) => {
  await mockRunApi(page);
  await page.goto(`/runs/${runId}`);
  await expect(page.getByText("排队等待")).toBeVisible();
  await expect(page.getByRole("heading", { name: "120 ms" })).toBeVisible();
  await expect(page.getByText("Readiness report")).toBeVisible();
  await page.getByRole("button", { name: "取消" }).click();
  await expect(page.getByRole("heading", { name: "cancelled" })).toBeVisible();
});
