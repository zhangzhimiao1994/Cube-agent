import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { McpPage } from "./McpPage";

const defaultPermissionGrants = ["mcp:read", "plugin:read", "plugin:write"];
const permissionGrants = new Set(defaultPermissionGrants);

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
    plugins: vi.fn(async () => [
      {
        id: "calendar",
        name: "Calendar HTTP",
        enabled: true,
        description: null,
        version: "1.0.0",
        endpoint_url: null,
        domain_allowlist: [],
        resource_config: {},
        timeout_seconds: 10,
        credential_ref: null,
        credential_header: "X-Plugin-Credential",
        credential_scheme: "Bearer",
        capabilities: [],
        source_filename: "calendar-plugin.zip",
        content_sha256: "abc123",
        package_metadata: {
          schema_version: 1,
          kind: "adapter_package",
          package_version: "1.2.3",
          adapter_id: "calendar_python",
          sdk_api_version: "1.0",
          signature: null,
          signature_verification: "not_provided",
          verified_public_key_sha256: null,
          approval_state: "pending",
          approval_reason: "adapter package requires plugin approval before activation",
          approved_by: null,
          approved_at: null,
          activation_state: "blocked_pending_approval",
          activation_reason: "adapter package requires plugin approval before activation",
          runtime: "python",
          entrypoint: "adapter/main.py",
          isolation: "local_process",
          install_mode: "scan_only",
        },
        status: "stopped",
        health: "stopped",
        last_error_type: null,
      },
    ]),
    pluginAdapters: vi.fn(async () => []),
    capabilityInstallerCatalog: vi.fn(async () => ({ entries: [] })),
    resolveCapabilityInstall: vi.fn(async () => ({
      matches: [
        {
          id: "office_doc_search",
          name_cn: "Office 文档搜索",
          summary_cn: "读取授权范围内的 Office 文档索引。",
          aliases: ["office"],
          risks: ["read_only"],
          permission_summary: ["读取 Office 文档索引", "执行前仍按能力策略审批"],
          plugin: {
            id: "office-doc-search",
            name: "Office 文档搜索",
            enabled: true,
            capabilities: [],
          },
        },
      ],
    })),
    planCapabilityInstall: vi.fn(async () => ({
      plan: {
        id: "cap-install-1234",
        status: "planned",
        entry_id: "office_doc_search",
        name_cn: "Office 文档搜索",
        summary_cn: "读取授权范围内的 Office 文档索引。",
        query: "读取 Office 文档并搜索",
        plugin_id: "office-doc-search",
        capabilities: ["office.search_documents"],
        risks: ["read_only"],
        permission_summary: ["读取 Office 文档索引", "执行前仍按能力策略审批"],
        rollback_strategy: "restore_previous_plugin_or_delete_installed_plugin",
        requires_confirmation: true,
        plugin_request: {
          id: "office-doc-search",
          name: "Office 文档搜索",
          enabled: true,
          capabilities: [],
        },
      },
    })),
    cancelCapabilityInstall: vi.fn(async () => ({
      plan: {
        id: "cap-install-1234",
        status: "cancelled",
        entry_id: "office_doc_search",
        name_cn: "Office 文档搜索",
        summary_cn: "读取授权范围内的 Office 文档索引。",
        query: "读取 Office 文档并搜索",
        plugin_id: "office-doc-search",
        capabilities: ["office.search_documents"],
        risks: ["read_only"],
        permission_summary: ["读取 Office 文档索引", "执行前仍按能力策略审批"],
        rollback_strategy: "restore_previous_plugin_or_delete_installed_plugin",
        requires_confirmation: true,
        plugin_request: {
          id: "office-doc-search",
          name: "Office 文档搜索",
          enabled: true,
          capabilities: [],
        },
      },
    })),
    installCapability: vi.fn(async () => ({
      plan: {
        id: "cap-install-1234",
        status: "installed",
        entry_id: "office_doc_search",
        name_cn: "Office 文档搜索",
        summary_cn: "读取授权范围内的 Office 文档索引。",
        query: "读取 Office 文档并搜索",
        plugin_id: "office-doc-search",
        capabilities: ["office.search_documents"],
        risks: ["read_only"],
        permission_summary: ["读取 Office 文档索引", "执行前仍按能力策略审批"],
        rollback_strategy: "restore_previous_plugin_or_delete_installed_plugin",
        requires_confirmation: true,
        plugin_request: {
          id: "office-doc-search",
          name: "Office 文档搜索",
          enabled: true,
          capabilities: [],
        },
      },
      plugin: {
        id: "office-doc-search",
        name: "Office 文档搜索",
        enabled: true,
        description: null,
        version: "1.0.0",
        endpoint_url: null,
        domain_allowlist: [],
        resource_config: {},
        timeout_seconds: 10,
        credential_ref: null,
        credential_header: "X-Plugin-Credential",
        credential_scheme: "Bearer",
        capabilities: [],
        source_filename: null,
        content_sha256: null,
        package_metadata: null,
        status: "running",
        health: "healthy",
        last_error_type: null,
      },
    })),
    rollbackCapabilityInstall: vi.fn(async () => ({
      plan: {
        id: "cap-install-1234",
        status: "rolled_back",
        entry_id: "office_doc_search",
        name_cn: "Office 文档搜索",
        summary_cn: "读取授权范围内的 Office 文档索引。",
        query: "读取 Office 文档并搜索",
        plugin_id: "office-doc-search",
        capabilities: ["office.search_documents"],
        risks: ["read_only"],
        permission_summary: ["读取 Office 文档索引", "执行前仍按能力策略审批"],
        rollback_strategy: "restore_previous_plugin_or_delete_installed_plugin",
        requires_confirmation: true,
        plugin_request: {
          id: "office-doc-search",
          name: "Office 文档搜索",
          enabled: true,
          capabilities: [],
        },
      },
    })),
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
    approvePluginPackage: vi.fn(async () => ({})),
    rejectPluginPackage: vi.fn(async () => ({})),
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
  beforeEach(() => {
    permissionGrants.clear();
    for (const permission of defaultPermissionGrants) {
      permissionGrants.add(permission);
    }
    vi.clearAllMocks();
  });

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
    expect((screen.getByRole("button", { name: "审批插件包 Calendar HTTP" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((screen.getByRole("button", { name: "拒绝插件包 Calendar HTTP" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((screen.getByLabelText("插件包审批理由") as HTMLTextAreaElement).disabled).toBe(true);
    expect(screen.getByText("当前账号无权审批插件包。")).not.toBeNull();
  });

  it("allows plugin package approval actions with plugin approval permission", async () => {
    permissionGrants.add("plugin:approve");
    const user = userEvent.setup();
    renderMcpPage();

    await screen.findByText("Calendar HTTP");
    const reasonInput = screen.getByLabelText("插件包审批理由");
    await user.type(reasonInput, "reviewed by security");
    await user.click(screen.getByRole("button", { name: "审批插件包 Calendar HTTP" }));
    await user.type(reasonInput, "requires isolation review");
    await user.click(screen.getByRole("button", { name: "拒绝插件包 Calendar HTTP" }));

    expect(api.approvePluginPackage).toHaveBeenCalledWith("calendar", { reason: "reviewed by security" });
    expect(api.rejectPluginPackage).toHaveBeenCalledWith("calendar", { reason: "requires isolation review" });
  });

  it("searches, plans, and confirms trusted capability installs from the MCP page", async () => {
    const user = userEvent.setup();
    renderMcpPage();

    const installerRegion = await screen.findByRole("region", { name: "能力安装器" });
    await user.type(within(installerRegion).getByLabelText("能力需求"), "读取 Office 文档并搜索");
    await user.click(within(installerRegion).getByRole("button", { name: "搜索能力" }));

    expect(await within(installerRegion).findByText("Office 文档搜索")).not.toBeNull();
    await user.click(
      within(installerRegion).getByRole("button", { name: "生成安装计划 Office 文档搜索" }),
    );
    expect(await within(installerRegion).findByText("office.search_documents")).not.toBeNull();
    await user.click(
      within(installerRegion).getByRole("button", { name: "确认安装 Office 文档搜索" }),
    );

    await waitFor(() =>
      expect(api.installCapability).toHaveBeenCalledWith({
        entry_id: "office_doc_search",
        query: "读取 Office 文档并搜索",
        plan_id: "cap-install-1234",
        confirm: true,
      }),
    );
    expect(await within(installerRegion).findByText("能力已安装：Office 文档搜索")).not.toBeNull();

    await user.click(within(installerRegion).getByRole("button", { name: "重新检测能力" }));
    expect(await within(installerRegion).findByText("已重新检测能力：Office 文档搜索")).not.toBeNull();

    await user.click(within(installerRegion).getByRole("button", { name: "回滚本次安装" }));
    await waitFor(() =>
      expect(api.rollbackCapabilityInstall).toHaveBeenCalledWith({
        entry_id: "office_doc_search",
        query: "读取 Office 文档并搜索",
      }),
    );
    expect(await within(installerRegion).findByText("已回滚能力安装：Office 文档搜索")).not.toBeNull();
  });
});
