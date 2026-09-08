import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { McpPage } from "./McpPage";

const permissionGrants = new Set(["mcp:read", "plugin:read", "plugin:write"]);

vi.mock("../auth/AuthProvider", () => ({
  useAuth: () => ({
    user: {
      user_id: "11111111-1111-4111-8111-111111111111",
      tenant_id: "33333333-3333-4333-8333-333333333333",
      username: "plugin-writer",
      role: "plugin_writer",
      permissions: Array.from(permissionGrants),
    },
    loading: false,
    login: vi.fn(),
    setup: vi.fn(),
    logout: vi.fn(),
    hasPermission: (permission: string) => permissionGrants.has(permission),
  }),
}));

vi.mock("../api/client", () => ({
  api: {
    mcpServers: vi.fn(async () => []),
    plugins: vi.fn(async () => []),
    pluginAdapters: vi.fn(async () => []),
    capabilityManifest: vi.fn(async () => ({ schema_version: 1, capabilities: [] })),
    pluginSigningKeys: vi.fn(async () => [
      {
        key_id: "calendar-prod",
        algorithm: "ed25519",
        public_key: "A".repeat(43),
        trusted: true,
        not_before: null,
        not_after: null,
      },
    ]),
    createMcpServer: vi.fn(),
    deleteMcpServer: vi.fn(),
    createPlugin: vi.fn(),
    uploadPluginArchive: vi.fn(),
    startPlugin: vi.fn(),
    enablePlugin: vi.fn(),
    disablePlugin: vi.fn(),
    stopPlugin: vi.fn(),
    reloadPlugin: vi.fn(),
    deletePlugin: vi.fn(),
    upsertPluginSigningKey: vi.fn(),
    deletePluginSigningKey: vi.fn(),
  },
}));

function renderMcpPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <McpPage />
    </QueryClientProvider>,
  );
}

describe("McpPage plugin approval permissions", () => {
  it("keeps ordinary plugin writes enabled while blocking trusted signing key mutations without plugin approval", async () => {
    renderMcpPage();

    const signingKeyRegion = await screen.findByRole("region", { name: "可信插件签名 Key" });
    const saveSigningKey = within(signingKeyRegion).getByRole("button", {
      name: "保存签名 Key",
    }) as HTMLButtonElement;
    const deleteSigningKey = await within(signingKeyRegion).findByRole("button", {
      name: "删除签名 Key calendar-prod",
    }) as HTMLButtonElement;

    expect(saveSigningKey.disabled).toBe(true);
    expect(deleteSigningKey.disabled).toBe(true);
    expect(within(signingKeyRegion).getByText("当前账号无权保存插件签名 Key。")).not.toBeNull();
    expect((screen.getByRole("button", { name: "保存插件" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });
});
