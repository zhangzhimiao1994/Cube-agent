import { readFileSync } from "node:fs";

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, WebPreviewSchema, type Conversation, type ProjectWorkspace, type RunDetail } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import {
  agentInlineSummary,
  ConversationCheckpointNav,
  ConversationManagerTabs,
  ProjectOverview,
  RunsPage,
  conversationProjectOptions,
  sortProjectOverview,
  type ProjectSort,
  conversationManagerMetadata,
  conversationManagerSearchState,
  conversationUsageSummary,
  compactConversationDraft,
  currentConversationRuns,
  conversationIdFromSearch,
  conversationQuestionSearchHref,
  conversationMatchesSearch,
  conversationSelectionIds,
  conversationCheckpoints,
  conversationWorkspaceFiles,
  conversationMessages,
  WorkbenchFilePreview,
  MessageBody,
  mergeConversationRuns,
  requestedPermissionsForSandbox,
  runConversationId,
  runDetailVersion,
  runProcessItems,
  slashCommandsForQuery,
  shouldShowModeEntry,
  scrollToConversationHash,
  processDetailValuePresentation,
  workbenchFileItems,
  workbenchActionDescriptor,
  workspacePreviewPath,
} from "./RunsPage";

function websitePreviewFixture(id: string, scope: string, displayRoot = ".") {
  return WebPreviewSchema.parse({
    id, status: "ready", preview_url: `/api/v1/web-previews/${id}/content/`,
    lease_expires_at: "2026-09-28T08:30:00Z",
    identity: {
      preview_id: id, kind: "static", tenant_id: "33333333-3333-4333-8333-333333333333",
      user_id: "11111111-1111-4111-8111-111111111111", project_id: `project-${scope}`,
      conversation_id: `conv-${scope}`, workspace_session_id: `session-${scope}`,
      runtime_handle: null, source: { scheme: "preview-static-tree-v1", sha256: "b".repeat(64) },
      display_root: displayRoot, display_entrypoint: "index.html",
    },
    cleanup_url: `/api/v1/web-previews/${id}/cleanup`, cleanup_receipt: null,
  });
}

const baseRun: RunDetail = {
  id: "22222222-2222-4222-8222-222222222222",
  status: "running",
  mode: "dispatch",
  version: 9,
  conversation_id: "conv-top-level",
  request: "test",
  created_at: "2026-09-02T00:00:00Z",
  queue_wait_ms: 0,
  capacity_wait_ms: 0,
  cost_usd: "0.00",
  events: [],
  artifacts: [],
  explicit_details: {},
  failure_diagnostics: [],
  tool_lifecycle: [],
  model_outcome_summary: {
    completion_count: 0,
    fallback_used: false,
    fallback_attempt_count: 0,
    requested_logical_models: [],
    actual_logical_models: [],
    attempted_logical_models: [],
    provider_ids: [],
  },
  orchestration_protocol_summary: null,
};

describe("runConversationId", () => {
  it("falls back to the top-level conversation id when explicit details omit it", () => {
    expect(runConversationId(baseRun)).toBe("conv-top-level");
  });

  it("keeps explicit detail conversation id authoritative", () => {
    expect(
      runConversationId({
        ...baseRun,
        explicit_details: { conversation_id: "conv-explicit" },
      }),
    ).toBe("conv-explicit");
  });
});

describe("conversation manager views", () => {
  const currentConversation = {
    conversation_id: "conv-current",
    title: "当前会话",
    project_id: "default",
    project_label: "默认项目",
    workspace_path: "conv-current",
    archived_at: null,
    created_at: "2026-09-27T00:00:00Z",
    updated_at: "2026-09-27T00:00:00Z",
  };
  const archivedConversation = {
    ...currentConversation,
    conversation_id: "conv-archived",
    title: "归档会话",
    workspace_path: "conv-archived",
    archived_at: "2026-09-27T01:00:00Z",
  };

  it("keeps archived conversations out of the current view", () => {
    expect(
      conversationManagerMetadata([currentConversation], [archivedConversation], "current"),
    ).toEqual([currentConversation]);
    expect(
      conversationManagerMetadata([currentConversation], [archivedConversation], "archived"),
    ).toEqual([archivedConversation]);
  });

  it("counts full-history question hits and does not show a contradictory empty state", () => {
    expect(
      conversationManagerSearchState({
        localConversationCount: 0,
        questionResultCount: 2,
        questionResultsCurrent: true,
        questionSearchPending: false,
      }),
    ).toEqual({
      effectiveQuestionResultCount: 2,
      hasVisibleResults: true,
      summary: "0 个会话 · 已加载 2 个问题",
    });
  });

  it("hides stale debounced question hits while the next search is pending", () => {
    expect(
      conversationManagerSearchState({
        localConversationCount: 0,
        questionResultCount: 3,
        questionResultsCurrent: false,
        questionSearchPending: true,
      }),
    ).toEqual({
      effectiveQuestionResultCount: 0,
      hasVisibleResults: true,
      summary: "0 个会话",
    });
  });

  it("keeps runs from archived conversations out of the current view", () => {
    const currentRun = { ...baseRun, id: "run-current", conversation_id: "conv-current" };
    const archivedRun = { ...baseRun, id: "run-archived", conversation_id: "conv-archived" };

    expect(currentConversationRuns([currentRun, archivedRun], [archivedConversation])).toEqual([
      currentRun,
    ]);
  });

  it("switches between current and archived conversations with accessible tabs", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(
      <ConversationManagerTabs
        value="current"
        currentCount={3}
        archivedCount={2}
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("tab", { name: "当前会话 3" }).getAttribute("aria-selected")).toBe(
      "true",
    );
    await user.click(screen.getByRole("tab", { name: "已归档 2" }));
    expect(onChange).toHaveBeenCalledWith("archived");

    rerender(
      <ConversationManagerTabs
        value="archived"
        currentCount={3}
        archivedCount={2}
        onChange={onChange}
      />,
    );
    expect(screen.getByRole("tab", { name: "已归档 2" }).getAttribute("aria-selected")).toBe(
      "true",
    );
  });
});

describe("conversation action menu", () => {
  const clients: QueryClient[] = [];

  afterEach(() => {
    cleanup();
    clients.splice(0).forEach((client) => client.clear());
    vi.restoreAllMocks();
  });

  async function renderMenuPage(persisted = true) {
    vi.spyOn(api, "me").mockResolvedValue({
      tenant_id: "menu-tenant", user_id: "menu-user", username: "menu-user", role: "operator", permissions: ["*"],
    });
    const conversations: Conversation[] = ["alpha", "beta"].map((name) => ({
      conversation_id: `conv-menu-${name}`,
      title: `Menu ${name}`,
      project_id: "menu-project",
      project_label: "Menu project",
      workspace_path: `conv-menu-${name}`,
      archived_at: null,
      created_at: persisted ? "2026-10-22T00:00:00Z" : null,
      updated_at: "2026-10-22T00:00:00Z",
      runs: [],
    }));
    const client = new QueryClient({
      defaultOptions: { queries: { enabled: false, retry: false, staleTime: Infinity } },
    });
    clients.push(client);
    client.setQueryData(["runs"], []);
    client.setQueryData(["conversations", false], conversations);
    client.setQueryData(["conversations", true], []);
    client.setQueryData(["project-workspaces"], []);
    conversations.forEach((conversation) => {
      client.setQueryData(["conversation", conversation.conversation_id], conversation);
      client.setQueryData(["conversation-queue", conversation.conversation_id], []);
      client.setQueryData(["workspace-files", "menu-project", conversation.workspace_path], {
        items: [], bundle_download_url: "",
      });
    });
    vi.spyOn(api, "conversation").mockImplementation(async (id) => {
      const conversation = conversations.find((item) => item.conversation_id === id);
      if (!conversation) throw new Error(`Unexpected conversation ${id}`);
      return conversation;
    });
    vi.spyOn(api, "conversationQueue").mockResolvedValue([]);
    vi.spyOn(api, "workspaceFiles").mockResolvedValue({ items: [], bundle_download_url: "" });
    const update = vi.spyOn(api, "updateConversation").mockImplementation(async (id, patch) => {
      const conversation = conversations.find((item) => item.conversation_id === id)!;
      Object.assign(conversation, {
        ...(patch.title !== undefined ? { title: patch.title } : {}),
        ...(patch.archived !== undefined ? { archived_at: patch.archived ? "2026-10-22T01:00:00Z" : null } : {}),
      });
      return { ...conversation };
    });
    let navigate!: ReturnType<typeof useNavigate>;
    function MenuTestPage() {
      navigate = useNavigate();
      return <RunsPage />;
    }
    render(
      <QueryClientProvider client={client}>
        <AuthProvider><MemoryRouter initialEntries={["/?conversation=conv-menu-alpha"]}><MenuTestPage /></MemoryRouter></AuthProvider>
      </QueryClientProvider>,
    );
    await screen.findByRole("form", { name: "发送消息" });
    return { navigate: (to: string) => navigate(to), update };
  }

  it("lets historical conversations copy their ID without offering unavailable metadata edits", async () => {
    const user = userEvent.setup();
    const { update } = await renderMenuPage(false);
    const trigger = screen.getByRole("button", { name: "会话操作" });
    expect(trigger.hasAttribute("disabled")).toBe(false);
    await user.click(trigger);
    expect(screen.getByRole("menuitem", { name: "重命名" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("menuitem", { name: "归档" }).hasAttribute("disabled")).toBe(true);
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    await user.click(screen.getByRole("menuitem", { name: "复制会话 ID" }));
    expect(copy).toHaveBeenCalledExactlyOnceWith("conv-menu-alpha");
    expect(update).not.toHaveBeenCalled();
  });

  it("keeps the composer blank and exposes the current conversation ID only in its menu", async () => {
    const user = userEvent.setup();
    await renderMenuPage();
    const input = screen.getByRole("textbox", { name: "消息" });
    expect(input.getAttribute("placeholder")).toBeNull();
    expect(screen.queryByText("conv-menu-alpha")).toBeNull();
    await user.click(screen.getByRole("button", { name: "会话操作" }));
    const menu = screen.getByRole("menu", { name: "会话操作" });
    expect(within(menu).getByText("conv-menu-alpha")).not.toBeNull();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    await user.click(within(menu).getByRole("menuitem", { name: "复制会话 ID" }));
    expect(copy).toHaveBeenCalledExactlyOnceWith("conv-menu-alpha");
    expect(await within(menu).findByText("已复制")).not.toBeNull();
  });

  it("shows copy failure without closing the menu or changing the conversation", async () => {
    const user = userEvent.setup();
    await renderMenuPage();
    await user.click(screen.getByRole("button", { name: "会话操作" }));
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("clipboard blocked"));
    await user.click(screen.getByRole("menuitem", { name: "复制会话 ID" }));
    expect(await within(screen.getByRole("menu", { name: "会话操作" })).findByRole("alert")).toHaveProperty("textContent", "复制失败，请重试");
    expect(screen.getByText("会话：Menu alpha")).not.toBeNull();
  });

  it("dismisses outside pointer clicks without stealing their focus", async () => {
    const user = userEvent.setup();
    await renderMenuPage();
    const trigger = screen.getByRole("button", { name: "会话操作" });
    await user.click(trigger);
    await user.click(screen.getByRole("menu"));
    expect(screen.queryByRole("menu")).not.toBeNull();
    const composer = screen.getByRole("textbox", { name: "消息" });
    await user.click(composer);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(composer);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    const user = userEvent.setup();
    await renderMenuPage();
    const trigger = screen.getByRole("button", { name: "会话操作" });
    await user.click(trigger);
    screen.getByRole("menuitem", { name: "重命名" }).focus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it.each([
    ["{Enter}", "重命名"], [" ", "重命名"],
    ["{ArrowDown}", "重命名"], ["{ArrowUp}", "复制会话 ID"],
  ])("opens with %s and focuses %s", async (key, name) => {
    const user = userEvent.setup();
    await renderMenuPage();
    const trigger = screen.getByRole("button", { name: "会话操作" });
    expect(trigger.getAttribute("title")).toBe("会话操作：重命名、归档");
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    trigger.focus();
    await user.keyboard(key);
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name }));
  });

  it("navigates with wrapping arrows and Home/End without invoking actions", async () => {
    const user = userEvent.setup();
    const { update } = await renderMenuPage();
    await user.click(screen.getByRole("button", { name: "会话操作" }));
    const rename = screen.getByRole("menuitem", { name: "重命名" });
    const archive = screen.getByRole("menuitem", { name: "归档" });
    const copy = screen.getByRole("menuitem", { name: "复制会话 ID" });
    rename.focus();
    for (const [key, target] of [
      ["{ArrowDown}", archive], ["{ArrowDown}", copy], ["{ArrowDown}", rename],
      ["{ArrowUp}", copy], ["{Home}", rename], ["{End}", copy],
    ] as const) {
      await user.keyboard(key);
      expect(document.activeElement).toBe(target);
    }
    expect(update).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each([false, true])("dismisses on Tab (shift=%s) without trapping focus", async (shift) => {
    const user = userEvent.setup();
    await renderMenuPage();
    const trigger = screen.getByRole("button", { name: "会话操作" });
    await user.click(trigger);
    screen.getByRole("menuitem", { name: "重命名" }).focus();
    await user.tab({ shift });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(shift ? trigger : screen.getByRole("textbox", { name: "消息" }));
  });

  it("resets on conversation navigation without pulling focus from the composer", async () => {
    const user = userEvent.setup();
    const { navigate } = await renderMenuPage();
    await user.click(screen.getByRole("button", { name: "会话操作" }));
    const composer = screen.getByRole("textbox", { name: "消息" });
    composer.focus();
    await act(async () => { await navigate("/?conversation=conv-menu-beta"); });
    expect(screen.getByText("会话：Menu beta")).not.toBeNull();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(composer);
  });

  it("returns from the rename dialog to the trigger instead of its removed menu item", async () => {
    const user = userEvent.setup();
    await renderMenuPage();
    const trigger = screen.getByRole("button", { name: "会话操作" });
    await user.click(trigger);
    screen.getByRole("menuitem", { name: "重命名" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.queryByRole("menu")).toBeNull();
    const dialog = screen.getByRole("dialog", { name: "重命名会话" });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("preserves archive/restore actions and restores focus after success", async () => {
    const user = userEvent.setup();
    const { update } = await renderMenuPage();
    const trigger = screen.getByRole("button", { name: "会话操作" });
    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "归档" }));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(update).toHaveBeenCalledWith("conv-menu-alpha", { archived: true });
    expect(document.activeElement).toBe(trigger);
    expect(trigger.getAttribute("title")).toBe("会话操作：重命名、恢复");
    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "恢复" }));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(update).toHaveBeenLastCalledWith("conv-menu-alpha", { archived: false });
    expect(document.activeElement).toBe(trigger);
    expect(trigger.getAttribute("title")).toBe("会话操作：重命名、归档");
  });
});

describe("project overview", () => {
  const workspaces: ProjectWorkspace[] = [
    {
      project_id: "alpha", label: "Alpha", workspace_path: "main", legacy_workspace_count: 1,
      created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-17T00:00:00Z",
    },
    {
      project_id: "beta", label: "Beta", workspace_path: "main", legacy_workspace_count: 1,
      created_at: "2026-10-10T00:00:00Z", updated_at: "2026-10-18T00:00:00Z",
    },
    {
      project_id: "gamma", label: "Gamma", workspace_path: "main", legacy_workspace_count: 1,
      created_at: "2026-10-05T00:00:00Z", updated_at: "2026-10-16T00:00:00Z",
    },
  ];

  it("maps only project DTO timestamps and keeps shared project options in name order", () => {
    const options = conversationProjectOptions([...workspaces].reverse(), [{
      conversation_id: "legacy-conversation", title: "Legacy conversation", project_id: "legacy",
      project_label: "Legacy", workspace_path: "archive", created_at: "2020-01-01T00:00:00Z",
      updated_at: "2026-10-18T12:00:00Z",
    }], []);

    expect(options.map((project) => project.id)).toEqual(["alpha", "beta", "gamma", "legacy"]);
    expect(options[0]).toMatchObject({
      createdAt: "2026-10-01T00:00:00Z", updatedAt: "2026-10-17T00:00:00Z",
    });
    expect(options[3]).toMatchObject({ createdAt: null, updatedAt: null });
  });

  it("does not fill missing project timestamps from current or archived conversations", () => {
    const conversation = {
      conversation_id: "conversation-with-date", title: "Conversation", project_id: "alpha",
      created_at: "2020-01-01T00:00:00Z", updated_at: "2026-10-18T12:00:00Z",
    };
    const options = conversationProjectOptions([
      { ...workspaces[0], created_at: null, updated_at: undefined },
    ], [conversation], [{ ...conversation, project_id: "archived-project" }]);

    expect(options.map((project) => [project.id, project.createdAt, project.updatedAt])).toEqual([
      ["alpha", null, null], ["archived-project", null, null],
    ]);
  });

  it.each([
    ["updated-desc", ["beta", "alpha", "gamma"]],
    ["updated-asc", ["gamma", "alpha", "beta"]],
    ["created-desc", ["beta", "gamma", "alpha"]],
    ["created-asc", ["alpha", "gamma", "beta"]],
  ] as const)("sorts the overview by %s without changing shared options", (sort, expected) => {
    const options = conversationProjectOptions(workspaces, [], []);
    Object.freeze(options);
    expect(sortProjectOverview(options, sort).map((project) => project.id)).toEqual(expected);
    expect(options.map((project) => project.id)).toEqual(["alpha", "beta", "gamma"]);
  });

  it("defaults to most recently modified projects", () => {
    expect(sortProjectOverview(conversationProjectOptions(workspaces, [], [])).map((project) => project.id))
      .toEqual(["beta", "alpha", "gamma"]);
  });

  it.each(["created-asc", "created-desc", "updated-asc", "updated-desc"] as const)(
    "puts missing, invalid and timezone-less timestamps last for %s with stable project IDs",
    (sort) => {
      const invalidDates = [null, undefined, "", "not-a-date", "2026-10-18T12:00:00",
        "2026-10-18", "2026-02-30T12:00:00Z", "2026-10-18T12:00:00+25:00"];
      const projects = [
        ...invalidDates.map((value, index) => ({
          id: `unknown-${index}`, label: "Unknown", workspacePath: "main", legacyWorkspaceCount: 1,
          createdAt: value, updatedAt: value,
        })).reverse(),
        { id: "valid-b", label: "B", workspacePath: "main", legacyWorkspaceCount: 1,
          createdAt: "2026-10-18T08:00:00+08:00", updatedAt: "2026-10-18T08:00:00+08:00" },
        { id: "valid-a", label: "Z", workspacePath: "main", legacyWorkspaceCount: 1,
          createdAt: "2026-10-18T00:00:00Z", updatedAt: "2026-10-18T00:00:00Z" },
      ];
      expect(sortProjectOverview(projects, sort).map((project) => project.id)).toEqual([
        "valid-a", "valid-b", "unknown-0", "unknown-1", "unknown-2", "unknown-3",
        "unknown-4", "unknown-5", "unknown-6", "unknown-7",
      ]);
    },
  );

  it("renders both project timestamps and reorders the visible list through the select", async () => {
    const user = userEvent.setup();
    const projects = conversationProjectOptions(workspaces, [], []);
    function Overview() {
      const [sort, setSort] = useState<ProjectSort>();
      return <ProjectOverview projects={projects} sort={sort} onSortChange={setSort}
        onSelect={() => {}} onNewConversation={() => {}} />;
    }
    render(<Overview />);
    const projectOrder = () => screen.getAllByRole("button", { name: /^进入项目 / })
      .map((button) => button.getAttribute("aria-label"));
    expect(projectOrder()).toEqual(["进入项目 Beta", "进入项目 Alpha", "进入项目 Gamma"]);
    const alpha = screen.getByRole("button", { name: "进入项目 Alpha" });
    expect(alpha.textContent).toContain("创建：");
    expect(alpha.textContent).toContain("修改：");
    expect(Array.from(alpha.querySelectorAll("time")).map((time) => time.dateTime)).toEqual([
      "2026-10-01T00:00:00Z", "2026-10-17T00:00:00Z",
    ]);
    for (const [value, labels] of [
      ["created-asc", ["Alpha", "Gamma", "Beta"]],
      ["created-desc", ["Beta", "Gamma", "Alpha"]],
      ["updated-asc", ["Gamma", "Alpha", "Beta"]],
      ["updated-desc", ["Beta", "Alpha", "Gamma"]],
    ] as const) {
      await user.selectOptions(screen.getByRole("combobox", { name: "项目排序" }), value);
      expect(projectOrder()).toEqual(labels.map((label) => `进入项目 ${label}`));
    }
  });

  it("shows unknown project times instead of borrowing a valid timestamp from the other field", () => {
    render(<ProjectOverview projects={conversationProjectOptions([
      { ...workspaces[0], created_at: "2026-10-01T00:00:00", updated_at: null },
      { ...workspaces[1], created_at: "invalid" },
    ], [], [])} onSortChange={() => {}} onSelect={() => {}} onNewConversation={() => {}} />);
    const alpha = screen.getByRole("button", { name: "进入项目 Alpha" });
    expect(within(alpha).getByText("创建：未知")).toBeTruthy();
    expect(within(alpha).getByText("修改：未知")).toBeTruthy();
    expect(alpha.querySelectorAll("time")).toHaveLength(0);
    expect(within(screen.getByRole("button", { name: "进入项目 Beta" })).getByText("创建：未知"))
      .toBeTruthy();
  });
});

describe("runDetailVersion", () => {
  it("prefers the top-level run version", () => {
    expect(
      runDetailVersion({
        ...baseRun,
        version: 9,
        explicit_details: { version: "3" },
      }),
    ).toBe(9);
  });

  it("falls back to explicit detail version for rolling upgrades", () => {
    expect(
      runDetailVersion({
        ...baseRun,
        version: 0,
        explicit_details: { version: "3" },
      }),
    ).toBe(3);
  });
});

describe("conversation ordering", () => {
  it("searches conversation history by question, id, mode, and status", () => {
    const run = {
      ...baseRun,
      conversation_id: "conv-office-search",
      request: "构建 Office 文档搜索插件",
      mode: "hybrid",
      status: "completed",
    };

    const followUp = { ...run, id: "follow-up", request: "继续完善移动端预览" };

    expect(conversationMatchesSearch(run, "Office 搜索", [run, followUp])).toBe(true);
    expect(conversationMatchesSearch(run, "conv-office", [run, followUp])).toBe(true);
    expect(conversationMatchesSearch(run, "混合 已完成", [run, followUp])).toBe(true);
    expect(conversationMatchesSearch(run, "移动端预览", [run, followUp])).toBe(true);
    expect(conversationMatchesSearch(run, "支付网关", [run, followUp])).toBe(false);
  });

  it("limits bulk conversation selection to visible terminal search results", () => {
    const items = [
      { ...baseRun, id: "office", conversation_id: "conv-office", request: "Office 搜索", status: "completed" },
      { ...baseRun, id: "mobile", conversation_id: "conv-mobile", request: "移动端预览", status: "failed" },
      { ...baseRun, id: "running", conversation_id: "conv-running", request: "Office 运行中", status: "running" },
    ];

    expect(conversationSelectionIds(items, "Office")).toEqual(["office"]);
    expect(conversationSelectionIds(items, "")).toEqual(["office", "mobile"]);
  });

  it("restores a linked conversation id from the URL query", () => {
    expect(conversationIdFromSearch("?conversation=conv-office-search")).toBe("conv-office-search");
    expect(conversationIdFromSearch("?conversation=%20%20")).toBeNull();
    expect(shouldShowModeEntry("?conversation=conv-office-search")).toBe(false);
    expect(shouldShowModeEntry("")).toBe(true);
  });

  it("builds a stable deep link for a full-history question result", () => {
    expect(
      conversationQuestionSearchHref({
        conversationId: "conv/history",
        runId: "11111111-1111-4111-8111-111111111111",
      }),
    ).toBe(
      "?conversation=conv%2Fhistory#chat-message-11111111-1111-4111-8111-111111111111-request",
    );
  });

  it("renders conversation turns by run creation time even when incoming data is out of order", () => {
    const laterRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "第二轮请求",
      created_at: "2026-09-02T00:02:00Z",
    };
    const earlierRun: RunDetail = {
      ...baseRun,
      request: "第一轮请求",
      created_at: "2026-09-02T00:01:00Z",
    };

    const messages = conversationMessages([laterRun, earlierRun]);

    expect(messages.filter((message) => message.id.endsWith("-request")).map((message) => message.body)).toEqual([
      "第一轮请求",
      "第二轮请求",
    ]);
  });

  it("keeps merged cached conversation runs in chronological order", () => {
    const laterRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "第二轮请求",
      created_at: "2026-09-02T00:02:00Z",
    };
    const earlierRun: RunDetail = {
      ...baseRun,
      request: "第一轮请求",
      created_at: "2026-09-02T00:01:00Z",
    };

    const merged = mergeConversationRuns([laterRun], [earlierRun, laterRun]);

    expect(merged.map((run) => run.request)).toEqual(["第一轮请求", "第二轮请求"]);
  });

  it("prefers fresh conversation snapshots over stale cached runs with the same id", () => {
    const cachedRun: RunDetail = {
      ...baseRun,
      request: "旧请求",
      artifacts: [{ id: "artifact-old", kind: "markdown", title: "旧产物", text: "旧回复" }],
    };
    const freshRun: RunDetail = {
      ...baseRun,
      request: "新请求",
      artifacts: [{ id: "artifact-new", kind: "markdown", title: "新产物", text: "新回复" }],
    };

    const merged = mergeConversationRuns([cachedRun], [freshRun]);

    expect(merged).toHaveLength(1);
    expect(merged[0].request).toBe("新请求");
    expect(merged[0].artifacts[0]?.text).toBe("新回复");
  });

  it("does not downgrade cached conversation runs with older incoming progress", () => {
    const cachedRun: RunDetail = {
      ...baseRun,
      version: 2,
      request: "更新后的请求",
      events: [
        {
          sequence: 2,
          kind: "step.completed",
          message: "step.completed",
          created_at: "2026-09-02T00:00:02Z",
          participants: [],
          payload: {},
        },
      ],
    };
    const staleIncomingRun: RunDetail = {
      ...baseRun,
      version: 1,
      request: "旧请求",
      events: [],
    };

    const merged = mergeConversationRuns([cachedRun], [staleIncomingRun]);

    expect(merged).toHaveLength(1);
    expect(merged[0].request).toBe("更新后的请求");
    expect(merged[0].version).toBe(2);
  });

  it("keeps runs without reliable timestamps after timestamped history", () => {
    const timestampedRun: RunDetail = {
      ...baseRun,
      request: "已有历史",
      created_at: "2026-09-02T00:01:00Z",
    };
    const pendingRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "新提交待写入时间",
      created_at: null,
    };

    const messages = conversationMessages([pendingRun, timestampedRun]);

    expect(messages.filter((message) => message.id.endsWith("-request")).map((message) => message.body)).toEqual([
      "已有历史",
      "新提交待写入时间",
    ]);
  });

  it.each(["main", "final_synthesizer"])("prefers an explicit final answer over upstream %s and later reviewers or attachments", (title) => {
    const run: RunDetail = {
      ...baseRun,
      status: "completed",
      artifacts: [
        { id: "upstream", kind: "text", title, text: "Upstream draft." },
        { id: "report", kind: "text", title: "delivery_main_agent", presentation: "final_answer",
          text: "## Delivery\n\nThe requested changes are ready. Checks passed." },
        { id: "review", kind: "text", title: "quality_reviewer", text: "Later review notes." },
        { id: "attachment", kind: "tool_result", title: "main", text: "Project archive ready.",
          presentation: "final_attachment", filename: "project.zip", mime_type: "application/zip",
          download_url: "/api/v1/runs/run/artifacts/attachment/download" },
      ],
    };

    const reply = conversationMessages([run]).find((message) => message.title === "回复");

    expect(reply?.id).toContain("report");
    expect(reply?.body).toContain("The requested changes are ready.");
    expect(reply?.body).not.toContain("Upstream draft");
    expect(reply?.body).not.toContain("Later review notes");
    expect(reply?.body).not.toContain("Project archive ready");
  });

  it("preserves explicit final answer identity when its text duplicates an upstream draft", () => {
    const run: RunDetail = { ...baseRun, artifacts: [
      { id: "upstream", kind: "text", title: "main", text: "Delivered and checked." },
      { id: "report", kind: "text", title: "delivery_main_agent", text: "Delivered and checked.", presentation: "final_answer" },
      { id: "later", kind: "text", title: "main", text: "Later process note." },
    ] };
    const reply = conversationMessages([run]).find((message) => message.title === "回复");
    expect(reply?.id).toContain("report");
    expect(reply?.body).toContain("Delivered and checked.");
  });

  it.each([undefined, "", " \n\t "])("ignores empty final answer text (%s) while retaining legacy replies", (text) => {
    const run: RunDetail = { ...baseRun, artifacts: [
      { id: "ordinary", kind: "text", title: "main", text: "An ordinary reply." },
      { id: "empty-final", kind: "text", title: "delivery_main_agent", text, presentation: "final_answer" },
      { id: "review", kind: "text", title: "reviewer", text: "Later review." },
    ] };
    expect(conversationMessages([run]).find((message) => message.title === "回复")?.body)
      .toContain("An ordinary reply.");
  });

  it.each(["main", "final_synthesizer", "domain_expert", "copywriter", "assistant"])("retains ordinary and legacy %s replies without explicit final presentation", (title) => {
    const run: RunDetail = { ...baseRun, artifacts: [
      { id: "ordinary", kind: "text", title, text: "An ordinary reply." },
      { id: "review", kind: "text", title: "reviewer", text: "Later review." },
    ] };
    expect(conversationMessages([run]).find((message) => message.title === "回复")?.body)
      .toContain("An ordinary reply.");
  });

  it("does not render download-only main artifacts as standalone attachment replies", () => {
    const run: RunDetail = {
      ...baseRun,
      status: "completed",
      artifacts: [
        {
          id: "artifact-main",
          kind: "tool_result",
          title: "main",
          filename: "project.zip",
          mime_type: "application/zip",
          size_bytes: 1024,
          sha256: "abc",
          download_url: "/api/v1/runs/run/artifacts/artifact-main/download",
          presentation: "final_attachment",
        },
      ],
    };

    const messages = conversationMessages([run]);

    expect(messages.map((message) => message.title)).toEqual(["你"]);
  });

  it("shows a neutral outcome for a completed round without a visible reply or final delivery", () => {
    const completed: RunDetail = { ...baseRun, status: "completed", artifacts: [] };
    const snapshot = JSON.stringify(completed);
    const messages = conversationMessages([completed]);
    const notice = messages.find((item) => item.title === "暂无交付结果");
    expect(notice?.body).toContain("本轮已结束，但未提供最终回复或交付文件");
    expect(notice?.body).toContain("不能据此确认项目完成");
    expect(notice?.body).not.toMatch(/失败|重试|重跑|已完成项目/);
    expect(JSON.stringify(completed)).toBe(snapshot);
    expect(conversationMessages([{ ...completed, status: "running" }]).some((item) => item.title === "暂无交付结果")).toBe(false);
    expect(conversationMessages([{ ...completed, artifacts: [{ id: "reply", kind: "text", title: "main", text: "已提供回复。" }] }])
      .some((item) => item.title === "暂无交付结果")).toBe(false);
  });

  it("keeps final artifacts with text as normal assistant replies", () => {
    const run: RunDetail = {
      ...baseRun,
      status: "completed",
      artifacts: [
        {
          id: "artifact-main",
          kind: "tool_result",
          title: "main",
          text: "插件已完成构建。\n\n核心设计：后台解析文档，前台搜索。",
          filename: "project.zip",
          mime_type: "application/zip",
          size_bytes: 1024,
          sha256: "abc",
          download_url: "/api/v1/runs/run/artifacts/artifact-main/download",
          presentation: "final_attachment",
        },
      ],
    };

    const messages = conversationMessages([run]);

    expect(messages.map((message) => message.title)).toEqual(["你", "回复"]);
    expect(messages[1].body).toContain("插件已完成构建");
    expect(messages[1].artifact?.filename).toBe("project.zip");
  });

  it("keeps automatic recovery noise out of an active conversation but preserves the final result", () => {
    const recovering: RunDetail = { ...baseRun, status: "running", artifacts: [
      { id: "partial", kind: "text", title: "main", text: "上次失败的中间输出" },
    ], events: [{ sequence: 8, kind: "repair.started", message: "repair.started", created_at:"2026-10-22T00:00:00Z", participants: [],
      payload: { automatic_execution: true, requires_approval: false } }] };
    expect(conversationMessages([recovering]).map(item => item.title)).toEqual(["你"]);
    expect(conversationMessages([{...recovering, status:"waiting_approval"}]).map(item=>item.title)).toContain("回复");
    expect(conversationMessages([{...recovering, events:recovering.events.map(event=>({...event,
      payload:{automatic_execution:true,requires_approval:true}}))}]).map(item=>item.title)).toContain("回复");
    const complete: RunDetail = { ...recovering, status: "completed", artifacts: [
      { id: "final", kind: "text", title: "main", text: "构建验证完成，这是最终结果。" },
    ] };
    expect(conversationMessages([complete]).at(-1)?.body).toBe("构建验证完成，这是最终结果。");
  });

  it("builds compact jump checkpoints from user questions", () => {
    const runs: RunDetail[] = [
      {
        ...baseRun,
        id: "11111111-1111-4111-8111-111111111111",
        request: "需要编写一个浏览器插件，不触发切屏读取 office 文档，并可以进行搜索查询",
        created_at: "2026-09-02T00:01:00Z",
      },
      {
        ...baseRun,
        id: "33333333-3333-4333-8333-333333333333",
        request: "继续优化 UI 交互，重点检查文件预览和配置页面",
        created_at: "2026-09-02T00:02:00Z",
      },
    ];

    expect(conversationCheckpoints(conversationMessages(runs))).toEqual([
      {
        id: "11111111-1111-4111-8111-111111111111-request",
        anchorId: "chat-message-11111111-1111-4111-8111-111111111111-request",
        artifactCount: 0,
        artifacts: [],
        href: "#chat-message-11111111-1111-4111-8111-111111111111-request",
        label: "需要编写一个浏览器插件，不触发切屏读取 office 文档，并...",
        searchText: "需要编写一个浏览器插件，不触发切屏读取 office 文档，并可以进行搜索查询",
        index: 1,
      },
      {
        id: "33333333-3333-4333-8333-333333333333-request",
        anchorId: "chat-message-33333333-3333-4333-8333-333333333333-request",
        artifactCount: 0,
        artifacts: [],
        href: "#chat-message-33333333-3333-4333-8333-333333333333-request",
        label: "继续优化 UI 交互，重点检查文件预览和配置页面",
        searchText: "继续优化 UI 交互，重点检查文件预览和配置页面",
        index: 2,
      },
    ]);
  });

  it("adds copyable anchors and related artifact counts to conversation checkpoints", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    window.history.replaceState(null, "", "/runs?conversation=conv-checkpoints");
    const runs: RunDetail[] = [
      {
        ...baseRun,
        id: "11111111-1111-4111-8111-111111111111",
        request: "第一轮做计划",
        created_at: "2026-09-02T00:01:00Z",
        artifacts: [
          {
            id: "plan",
            kind: "markdown",
            title: "计划",
            text: "计划内容",
            filename: "plan.md",
            mime_type: "text/markdown",
            size_bytes: 128,
            sha256: "a".repeat(64),
            download_url: "/download/plan",
            presentation: "final_attachment",
          },
        ],
      },
      {
        ...baseRun,
        id: "33333333-3333-4333-8333-333333333333",
        request: "第二轮检查 UI",
        created_at: "2026-09-02T00:02:00Z",
      },
    ];
    const checkpoints = conversationCheckpoints(conversationMessages(runs));

    expect(checkpoints[0]).toMatchObject({
      artifactCount: 1,
      artifacts: [
        {
          href: "#chat-message-11111111-1111-4111-8111-111111111111-artifact-plan",
          label: "plan.md",
        },
      ],
      href: "#chat-message-11111111-1111-4111-8111-111111111111-request",
    });

    const { container } = render(
      <ConversationCheckpointNav checkpoints={checkpoints} conversationId="conv-checkpoints" />,
    );

    expect(screen.getByRole("searchbox", { name: "搜索对话检查点" })).not.toBeNull();
    expect(container.querySelector(".conversation-checkpoint-list")).not.toBeNull();
    expect(screen.getByText("产物 1")).not.toBeNull();

    await userEvent.click(screen.getByText("产物 1"));
    expect(screen.getByRole("link", { name: "plan.md" })).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /复制检查点链接：第一轮做计划/ }));

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        expect.stringContaining("?conversation=conv-checkpoints#chat-message-11111111-1111-4111-8111-111111111111-request"),
      ),
    );

    await userEvent.type(screen.getByRole("searchbox", { name: "搜索对话检查点" }), "第二轮");
    expect(screen.queryByText("第一轮做计划")).toBeNull();
    expect(screen.getByText("第二轮检查 UI")).not.toBeNull();
  });

  it("finds a checkpoint by question text beyond the compact label", async () => {
    const runs: RunDetail[] = [
      {
        ...baseRun,
        id: "11111111-1111-4111-8111-111111111111",
        request: "这是一个足够长的问题，用来验证紧凑标题截断以后仍然能够搜索问题末尾的唯一关键词海王星",
        created_at: "2026-09-02T00:01:00Z",
      },
      {
        ...baseRun,
        id: "33333333-3333-4333-8333-333333333333",
        request: "另一个问题",
        created_at: "2026-09-02T00:02:00Z",
      },
    ];
    render(<ConversationCheckpointNav checkpoints={conversationCheckpoints(conversationMessages(runs))} />);

    await userEvent.type(screen.getByRole("searchbox", { name: "搜索对话检查点" }), "海王星");

    expect(screen.getByText(/这是一个足够长的问题/)).not.toBeNull();
    expect(screen.queryByText("另一个问题")).toBeNull();
  });

  it("restores a conversation checkpoint from the current URL hash", () => {
    const target = document.createElement("div");
    target.id = "chat-message-deep-link";
    const scrollIntoView = vi.fn();
    const focus = vi.fn();
    target.scrollIntoView = scrollIntoView;
    target.focus = focus;
    document.body.append(target);
    window.history.replaceState(null, "", "/runs#chat-message-deep-link");

    expect(scrollToConversationHash("auto")).toBe(true);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start", behavior: "auto" });
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });

    target.remove();
    window.history.replaceState(null, "", "/runs");
  });

  it("orders process cards by event sequence when backend events arrive out of order", () => {
    const outOfOrderRun: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 2,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-09-02T00:00:02Z",
          actor: "engineer",
          participants: [],
          tool_name: "artifact_writer",
          step_id: "write-output",
          action: null,
          decision: null,
          payload: { result: "第二步产物" },
        },
        {
          sequence: 1,
          kind: "step.started",
          message: "step.started",
          created_at: "2026-09-02T00:00:01Z",
          actor: "main_agent",
          participants: [],
          tool_name: null,
          step_id: "plan",
          action: null,
          decision: null,
          payload: { task: "第一步规划" },
        },
      ],
    };

    const items = runProcessItems(outOfOrderRun, new Map());

    expect(items.flatMap((item) => (item.createdAt ? [item.createdAt] : []))).toEqual([
      "2026-09-02T00:00:01Z",
      "2026-09-02T00:00:02Z",
    ]);
  });

  it("surfaces runtime recovery without checkpoint internals in chat workbench process items", () => {
    const recoveredRun: RunDetail = {
      ...baseRun,
      runtime_recovery_summary: {
        recovery_count: 1,
        last_completed_steps: 2,
        last_total_steps: 5,
        model_status_counts: { failed: 1, succeeded: 2 },
        tool_status_counts: { running: 1 },
        review_artifacts: 1,
      },
      events: [
        {
          sequence: 1,
          kind: "runtime.recovered",
          message: "runtime recovered from checkpoint",
          created_at: "2026-09-02T00:00:01Z",
          actor: "main_agent",
          participants: [],
          tool_name: null,
          step_id: "runtime-recovery",
          action: null,
          decision: null,
          payload: {
            recovery_count: 1,
            completed_steps: 2,
            total_steps: 5,
            model_status_counts: { failed: 1, succeeded: 2 },
            tool_status_counts: { running: 1 },
            review_artifacts: 1,
            checkpoint_id: "checkpoint-00000000-0000-4000-8000-000000000001",
          },
        },
      ],
    };

    const items = runProcessItems(recoveredRun, new Map());
    const recovery = items.find((item) => item.badge === "断点续跑");

    expect(recovery).toBeTruthy();
    expect(`${recovery?.title} ${recovery?.message}`).toContain("恢复完成");
    expect(`${recovery?.title} ${recovery?.message}`).toContain("2/5 步");
    expect(`${recovery?.title} ${recovery?.message}`).toContain("模型状态：异常 1，已完成 2");
    expect(`${recovery?.title} ${recovery?.message}`).toContain("工具状态：进行中 1");
    expect(`${recovery?.title} ${recovery?.message}`).toContain("审查产物 1");
    expect(JSON.stringify(recovery?.rows)).not.toContain("checkpoint-00000000-0000-4000-8000-000000000001");
    expect(JSON.stringify(recovery?.rows)).not.toContain("checkpoint_id");
  });

  it("surfaces tool lifecycle fallback actions when tool events are absent", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [],
      tool_lifecycle: [
        {
          tool_call_id: "tool-terminal-1",
          tool_name: "run_safe_command",
          status: "completed",
          operation_kind: "terminal",
          actor: "implementer",
          step_id: "install-deps",
          started_sequence: 10,
          terminal_sequence: 12,
          sequences: [10, 11, 12],
          approval_id: null,
          replay_safe: true,
          argument_bytes: 82,
          output_bytes: 4096,
          exit_code: 0,
          artifact_id: null,
          failure_kind: null,
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const terminal = items.find((item) => item.badge === "运行终端");
    const rows = terminal?.rows.map((row) => `${row.label}:${row.value}`).join("\n") ?? "";

    expect(terminal?.message).toBe("运行终端 已完成");
    expect(terminal?.sourceActor).toBe("implementer");
    expect(terminal?.sourceStepId).toBe("install-deps");
    expect(rows).toContain("事件范围:#10-#12");
    expect(rows).toContain("退出码:0");
  });

  it("projects nested discussion traces into chat workbench process rows", () => {
    const discussionRun: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "discussion.completed",
          message: "discussion.completed",
          summary: "多角色完成方案讨论",
          created_at: "2026-09-02T00:00:01Z",
          actor: "main_agent",
          participants: ["planner", "critic"],
          payload: {
            discussion_trace: {
              participants: ["planner", "critic"],
              member_statements: {
                planner: "建议先生成计划文件和架构图谱，再进入实现。",
                critic: "担心直接实现会遗漏测试标准，需要先补验收清单。",
              },
              disagreement_summary: "是否立即实现存在分歧，风险是跳过验收。",
              verification_steps: ["核对技能规则", "读取约束"],
              final_decision: "先完成计划和验收清单，再派发实现任务。",
            },
          },
        },
      ],
    };

    const item = runProcessItems(discussionRun, new Map()).find((candidate) => candidate.badge === "讨论过程");
    const rows = item?.rows.map((row) => `${row.label}:${row.value}`).join("\n") ?? "";

    expect(rows).toContain("Planner意见:建议先生成计划文件和架构图谱");
    expect(rows).toContain("分歧与风险:是否立即实现存在分歧");
    expect(rows).toContain("求证与验证:核对技能规则、读取约束");
    expect(rows).toContain("最终决策:先完成计划和验收清单");
  });
});

describe("MessageBody", () => {
  it("keeps long content compact while allowing expand and full copy", async () => {
    const longText = Array.from({ length: 22 }, (_item, index) => `line-${index + 1}: long architecture detail`)
      .join("\n");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<MessageBody text={longText} title="回复" />);

    expect(screen.queryByText(/line-22/)).toBeNull();
    expect(screen.getByRole("button", { name: "展开全文" })).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "复制全文" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(longText));
    expect(screen.getByRole("button", { name: "已复制全文" })).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "展开全文" }));
    expect(screen.getByText(/line-22: long architecture detail/)).not.toBeNull();
  });

  it("renders fenced code blocks as categorized copyable blocks", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<MessageBody title="回复" text={"快速定位\n```bash\nssh -vvv user@host\nnc -v host 22\n```\n完成"} />);

    expect(screen.getByText("bash")).not.toBeNull();
    expect(screen.getByText(/ssh -vvv user@host/)).not.toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "复制 bash 代码" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("ssh -vvv user@host\nnc -v host 22"));
    expect(screen.getByRole("button", { name: "已复制 bash 代码" })).not.toBeNull();
  });
});

describe("WorkbenchFilePreview", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(["README.md", "example.ts"])("uses safe shared output for %s file previews", async (filename) => {
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    const source = filename.endsWith(".md")
      ? "正文\n\n| | 状态 |\n|---|---|\n| a\\|b | first<br>second |\n\n~~~c++\n  exact <tag>\\n  \n~~~\n\n<script>alert(1)</script>"
      : "  const source = '<script>not executable</script>';\\n  \n";
    const { container } = render(<WorkbenchFilePreview file={{ id: filename, title: filename, filename, path: filename,
      kind: "workspace_file", operation: "读取文件", mimeType: null, size: "1 KB", sha256: null, text: source, source: null,
    }} onOpenSource={vi.fn()} />);
    if (filename.endsWith(".md")) {
      expect(screen.getByRole("cell", { name: "a|b" })).toBeTruthy();
      expect(screen.getByRole("cell", { name: "first second" }).querySelector("br")).toBeTruthy();
      expect(screen.getByLabelText("c++完整内容").textContent).toBe("  exact <tag>\\n  ");
      await user.click(screen.getByRole("button", { name: "复制 c++ 代码" }));
      expect(copy).toHaveBeenCalledWith("  exact <tag>\\n  ");
    } else {
      expect(screen.getByLabelText("typescript完整内容").querySelector("code")?.textContent).toBe(source);
      await user.click(screen.getByRole("button", { name: `复制 ${filename} 内容` }));
      expect(copy).toHaveBeenCalledWith(source);
    }
    expect(container.querySelectorAll("script,iframe,img")).toHaveLength(0);
  });

  it("switches a complete HTML artifact from source to a restricted live preview", async () => {
    const user = userEvent.setup();
    const trailingMarker = "HTML_AFTER_SOURCE_PREVIEW_LIMIT";
    const html = `<!-- fake <head> must not capture the security policy -->\n<!doctype html><html><head><title>Preview</title></head><body><button id="counter">0</button><script>document.querySelector('#counter').onclick = () => document.querySelector('#counter').textContent = '1';</script>${"x".repeat(8_100)}${trailingMarker}</body></html>`;
    const downloadSpy = vi
      .spyOn(api, "downloadGeneratedArtifact")
      .mockResolvedValue(new Response(html, { headers: { "Content-Type": "text/html" } }) as unknown as Blob);

    const { rerender } = render(
      <WorkbenchFilePreview
        file={{
          id: "readme",
          title: "README.md",
          filename: "README.md",
          path: "README.md",
          kind: "workspace_file",
          operation: "读取文件",
          mimeType: "text/markdown",
          size: "1 KB",
          sha256: null,
          text: "previous file",
          source: null,
        }}
        onOpenSource={vi.fn()}
      />,
    );
    rerender(
      <WorkbenchFilePreview
        file={{
          id: "preview-html",
          title: "preview.html",
          filename: "preview.html",
          path: "preview.html",
          kind: "workspace_file",
          operation: "创建文件",
          mimeType: "text/html",
          size: "9 KB",
          sha256: null,
          text: null,
          download: {
            id: "preview-html",
            kind: "workspace_file",
            title: "preview.html",
            filename: "preview.html",
            mime_type: "text/html",
            size_bytes: html.length,
            sha256: "a".repeat(64),
            download_url: "/api/v1/artifacts/preview-html/download",
          },
          source: null,
        }}
        onOpenSource={vi.fn()}
      />,
    );

    expect(screen.queryByTitle("preview.html 运行预览")).toBeNull();
    await waitFor(() => expect(screen.getByRole("button", { name: "源码预览" }).getAttribute("aria-pressed")).toBe("true"));
    expect(screen.getByRole("button", { name: "运行预览" }).hasAttribute("disabled")).toBe(false);
    expect(downloadSpy).toHaveBeenCalledWith("/api/v1/artifacts/preview-html/download");

    await user.click(screen.getByRole("button", { name: "运行预览" }));

    const iframe = screen.getByTitle("preview.html 运行预览");
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-top-navigation");
    expect(iframe.getAttribute("srcdoc")).toContain(trailingMarker);
    expect(iframe.getAttribute("srcdoc")).toContain("default-src 'none'");
    expect(iframe.getAttribute("srcdoc")).toContain("form-action 'none'");
    const previewDocument = new DOMParser().parseFromString(iframe.getAttribute("srcdoc") ?? "", "text/html");
    expect(previewDocument.head.querySelector('meta[http-equiv="Content-Security-Policy"]')).not.toBeNull();
  });

  it("does not let preview.html bypass checks for external scripts, styles, images, or fetches", () => {
    render(
      <WorkbenchFilePreview
        file={{
          id: "external-index",
          title: "preview.html",
          filename: "preview.html",
          path: "preview.html",
          kind: "workspace_file",
          operation: "创建文件",
          mimeType: "text/html",
          size: "2 KB",
          sha256: null,
          text: '<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head><body><img src="hero.png"><script src="app.js"></script><script>fetch("/api/data")</script></body></html>',
          source: null,
        }}
        onOpenSource={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("html完整内容").textContent).toContain("<!doctype html>");
    expect(screen.queryByRole("button", { name: "运行预览" })).toBeNull();
  });

  it("keeps oversized HTML source readable without creating a live iframe", async () => {
    const user = userEvent.setup();
    const html = `<!doctype html><html><body>${"大".repeat(180_000)}</body></html>`;
    render(
      <WorkbenchFilePreview
        file={{
          id: "oversized-preview",
          title: "preview.html",
          filename: "preview.html",
          path: "preview.html",
          kind: "workspace_file",
          operation: "创建文件",
          mimeType: "text/html",
          size: "528 KB",
          sha256: null,
          text: html,
          source: null,
        }}
        onOpenSource={vi.fn()}
      />,
    );

    expect(screen.getByText(/已截断，仅预览前 8000 字符/)).not.toBeNull();
    expect(screen.getByText(/超过 512 KB/)).not.toBeNull();
    const liveButton = screen.getByRole("button", { name: "运行预览" });
    expect(liveButton.hasAttribute("disabled")).toBe(true);
    await user.click(liveButton);
    expect(screen.queryByTitle("preview.html 运行预览")).toBeNull();
  });

  it("ignores a stale HTML download after the user switches files", async () => {
    const user = userEvent.setup();
    let resolveOld: ((value: Blob) => void) | undefined;
    let resolveCurrent: ((value: Blob) => void) | undefined;
    const oldDownload = new Promise<Blob>((resolve) => {
      resolveOld = resolve;
    });
    const currentDownload = new Promise<Blob>((resolve) => {
      resolveCurrent = resolve;
    });
    vi.spyOn(api, "downloadGeneratedArtifact").mockImplementation((path) =>
      path.includes("old") ? oldDownload : currentDownload,
    );
    const file = (id: string, url: string) => ({
      id,
      title: "preview.html",
      filename: "preview.html",
      path: `${id}/preview.html`,
      kind: "workspace_file",
      operation: "创建文件" as const,
      mimeType: "text/html",
      size: "1 KB",
      sha256: null,
      text: null,
      download: {
        id,
        kind: "workspace_file",
        title: "preview.html",
        filename: "preview.html",
        mime_type: "text/html",
        size_bytes: 1024,
        sha256: "b".repeat(64),
        download_url: url,
      },
      source: null,
    });

    const { rerender } = render(
      <WorkbenchFilePreview file={file("old", "/api/v1/artifacts/old/download")} onOpenSource={vi.fn()} />,
    );
    rerender(
      <WorkbenchFilePreview file={file("current", "/api/v1/artifacts/current/download")} onOpenSource={vi.fn()} />,
    );

    resolveCurrent?.(new Response("<!doctype html><html><body>CURRENT_FILE</body></html>") as unknown as Blob);
    await waitFor(() => expect(screen.getByText(/CURRENT_FILE/)).not.toBeNull());
    await user.click(screen.getByRole("button", { name: "运行预览" }));
    const iframe = screen.getByTitle("preview.html 运行预览");
    expect(iframe.getAttribute("srcdoc")).toContain("CURRENT_FILE");

    resolveOld?.(new Response("<!doctype html><html><body>STALE_FILE</body></html>") as unknown as Blob);
    await waitFor(() => expect(iframe.getAttribute("srcdoc")).not.toContain("STALE_FILE"));
    expect(iframe.getAttribute("srcdoc")).toContain("CURRENT_FILE");
  });

  it("keeps the live HTML preview bounded on desktop and mobile", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");

    expect(stylesCss).toMatch(
      /\.agent-workbench-html-preview\s*{[\s\S]*display:\s*block;[\s\S]*inline-size:\s*100%;[\s\S]*max-inline-size:\s*100%;[\s\S]*overflow:\s*hidden;/,
    );
    expect(stylesCss).toMatch(
      /@media \(max-width: 640px\)[\s\S]*\.agent-workbench-html-preview\s*{[\s\S]*min-height:\s*min\(62dvh,\s*36rem\);/,
    );
  });

  it("starts and stops a website service preview from a workspace HTML file", async () => {
    const user = userEvent.setup();
    const preview = websitePreviewFixture("10000000-0000-4000-8000-000000000001", "preview", "dist");
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(null);
    const start = vi.spyOn(api, "startWebPreview").mockResolvedValue(preview);
    const renew = vi.spyOn(api, "renewWebPreview").mockResolvedValue(preview);
    const stop = vi.spyOn(api, "stopWebPreview").mockResolvedValue({ ...preview, status: "stopped" });
    const open = vi.spyOn(window, "open").mockImplementation(() => null);

    const { unmount } = render(
      <WorkbenchFilePreview
        file={{
          id: "dist-index",
          title: "index.html",
          filename: "index.html",
          path: "dist/index.html",
          kind: "workspace_file",
          operation: "创建文件",
          mimeType: "text/html",
          size: "2 KB",
          sha256: null,
          text: "<!doctype html><html><body>fallback</body></html>",
          source: null,
        }}
        onOpenSource={vi.fn()}
        webPreviewScope={{
          conversationId: "conv-preview",
          projectId: "project-preview",
          workspaceSessionId: "session-preview",
        }}
      />,
    );

    await waitFor(() => expect(api.webPreviewForConversation).toHaveBeenCalledWith("conv-preview"));
    await user.click(screen.getByRole("button", { name: "运行网站" }));
    const serviceFrame = await screen.findByTitle("index.html 网站预览");
    await waitFor(() => expect(serviceFrame.getAttribute("src")).toBe(preview.preview_url));
    expect(serviceFrame.getAttribute("sandbox")).toBe("allow-scripts allow-forms allow-modals");
    expect(serviceFrame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(serviceFrame.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(start).toHaveBeenCalledWith({
      conversation_id: "conv-preview",
      project_id: "project-preview",
      workspace_session_id: "session-preview",
      root: "dist",
    });

    await user.click(screen.getByRole("button", { name: "在新窗口打开" }));
    expect(renew).toHaveBeenCalledWith(preview.id);
    expect(open).toHaveBeenCalledWith(preview.preview_url, "_blank", "noopener,noreferrer");

    window.dispatchEvent(new PageTransitionEvent("pagehide"));
    await waitFor(() => expect(stop).toHaveBeenCalledWith(preview.id, { keepalive: true }));
    unmount();
  });

  it("keeps an active website preview running when its drawer closes", async () => {
    const preview = websitePreviewFixture("10000000-0000-4000-8000-000000000002", "close");
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(preview);
    vi.spyOn(api, "renewWebPreview").mockResolvedValue(preview);
    const stop = vi.spyOn(api, "stopWebPreview").mockResolvedValue({ ...preview, status: "stopped" });
    const { unmount } = render(
      <WorkbenchFilePreview
        file={{
          id: "index-close",
          title: "index.html",
          filename: "index.html",
          path: "index.html",
          kind: "workspace_file",
          operation: "创建文件",
          mimeType: "text/html",
          size: "1 KB",
          sha256: null,
          text: "<!doctype html><html><body>preview</body></html>",
          source: null,
        }}
        onOpenSource={vi.fn()}
        webPreviewScope={{
          conversationId: "conv-close",
          projectId: "project-close",
          workspaceSessionId: "session-close",
        }}
      />,
    );
    await screen.findByTitle("index.html 网站预览");

    unmount();

    expect(stop).not.toHaveBeenCalled();
  });

  it("stops the previous website preview when switching conversations", async () => {
    const preview = websitePreviewFixture("10000000-0000-4000-8000-000000000003", "old");
    vi.spyOn(api, "webPreviewForConversation").mockImplementation(async (conversationId) => (
      conversationId === "conv-old" ? preview : null
    ));
    const stop = vi.spyOn(api, "stopWebPreview").mockResolvedValue({ ...preview, status: "stopped" });
    const file = {
      id: "index-switch",
      title: "index.html",
      filename: "index.html",
      path: "index.html",
      kind: "workspace_file" as const,
      operation: "创建文件" as const,
      mimeType: "text/html",
      size: "1 KB",
      sha256: null,
      text: "<!doctype html><html><body>preview</body></html>",
      source: null,
    };
    const { rerender } = render(
      <WorkbenchFilePreview
        file={file}
        onOpenSource={vi.fn()}
        webPreviewScope={{
          conversationId: "conv-old",
          projectId: "project-old",
          workspaceSessionId: "session-old",
        }}
      />,
    );
    await screen.findByTitle("index.html 网站预览");

    rerender(
      <WorkbenchFilePreview
        file={file}
        onOpenSource={vi.fn()}
        webPreviewScope={{
          conversationId: "conv-new",
          projectId: "project-new",
          workspaceSessionId: "session-new",
        }}
      />,
    );

    await waitFor(() => expect(stop).toHaveBeenCalledWith(preview.id, { keepalive: false }));
    await waitFor(() => expect(api.webPreviewForConversation).toHaveBeenCalledWith("conv-new"));
  });

  it("stops a website preview that finishes starting after the drawer unmounts", async () => {
    const user = userEvent.setup();
    let resolveStart!: (preview: Awaited<ReturnType<typeof api.startWebPreview>>) => void;
    const startPromise = new Promise<Awaited<ReturnType<typeof api.startWebPreview>>>((resolve) => {
      resolveStart = resolve;
    });
    const preview = websitePreviewFixture("10000000-0000-4000-8000-000000000004", "late");
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(null);
    vi.spyOn(api, "startWebPreview").mockReturnValue(startPromise);
    const stop = vi.spyOn(api, "stopWebPreview").mockResolvedValue({ ...preview, status: "stopped" });
    const { unmount } = render(
      <WorkbenchFilePreview
        file={{
          id: "index-late",
          title: "index.html",
          filename: "index.html",
          path: "index.html",
          kind: "workspace_file",
          operation: "创建文件",
          mimeType: "text/html",
          size: "1 KB",
          sha256: null,
          text: "<!doctype html><html><body>preview</body></html>",
          source: null,
        }}
        onOpenSource={vi.fn()}
        webPreviewScope={{
          conversationId: "conv-late",
          projectId: "project-late",
          workspaceSessionId: "session-late",
        }}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "运行网站" }));
    unmount();
    resolveStart(preview);

    await waitFor(() => expect(stop).toHaveBeenCalledWith(preview.id, { keepalive: false }));
  });

  it("keeps an active website preview alive when pagehide enters the back-forward cache", async () => {
    const preview = websitePreviewFixture("10000000-0000-4000-8000-000000000005", "bfcache");
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(preview);
    const stop = vi.spyOn(api, "stopWebPreview").mockResolvedValue({ ...preview, status: "stopped" });
    const { unmount } = render(
      <WorkbenchFilePreview
        file={{
          id: "index-bfcache",
          title: "index.html",
          filename: "index.html",
          path: "index.html",
          kind: "workspace_file",
          operation: "创建文件",
          mimeType: "text/html",
          size: "1 KB",
          sha256: null,
          text: "<!doctype html><html><body>preview</body></html>",
          source: null,
        }}
        onOpenSource={vi.fn()}
        webPreviewScope={{
          conversationId: "conv-bfcache",
          projectId: "project-bfcache",
          workspaceSessionId: "session-bfcache",
        }}
      />,
    );
    await screen.findByTitle("index.html 网站预览");

    window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true }));
    expect(stop).not.toHaveBeenCalled();
    unmount();
    expect(stop).not.toHaveBeenCalled();
  });

  it("keeps website service preview controls and iframe usable at 390px", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");

    expect(stylesCss).toMatch(
      /\.agent-workbench-web-preview-actions\s*{[\s\S]*display:\s*flex;[\s\S]*flex-wrap:\s*wrap;/,
    );
    expect(stylesCss).toMatch(
      /\.agent-workbench-web-preview\s*{[\s\S]*inline-size:\s*100%;[\s\S]*min-height:\s*min\(68dvh,\s*44rem\);/,
    );
    expect(stylesCss).toMatch(
      /@media \(max-width: 640px\)[\s\S]*\.agent-workbench-web-preview\s*{[\s\S]*min-height:\s*min\(58dvh,\s*34rem\);/,
    );
  });
});

describe("slashCommandsForQuery", () => {
  it("suggests command palette entries by slash prefix and Chinese aliases", () => {
    expect(slashCommandsForQuery("/m").map((command) => command.id)).toEqual(["compress", "memory", "mode"]);
    expect(slashCommandsForQuery("/记忆").map((command) => command.id)).toEqual(["memory"]);
    expect(slashCommandsForQuery("/重试").map((command) => command.id)).toEqual(["retry"]);
    expect(slashCommandsForQuery("/撤销").map((command) => command.id)).toEqual(["undo"]);
    expect(slashCommandsForQuery("/压缩").map((command) => command.id)).toEqual(["compress"]);
    expect(slashCommandsForQuery("普通消息")).toEqual([]);
  });

  it("summarizes real conversation usage without presenting estimates as reported tokens", () => {
    const summary = conversationUsageSummary([
      { ...baseRun, request: "first request", cost_usd: "0.12", status: "completed" },
      { ...baseRun, id: "33333333-3333-4333-8333-333333333333", request: "第二个问题", cost_usd: "0.08", status: "failed" },
    ]);

    expect(summary.runCount).toBe(2);
    expect(summary.completedCount).toBe(1);
    expect(summary.failedCount).toBe(1);
    expect(summary.costUsd).toBe("0.200000");
    expect(summary.estimatedContextTokens).toBeGreaterThan(0);
  });

  it("builds a bounded compression draft from recent turns", () => {
    const runs = Array.from({ length: 20 }, (_, index) => ({
      ...baseRun,
      id: `${String(index).padStart(8, "0")}-2222-4222-8222-222222222222`,
      request: `question-${index} ${"x".repeat(600)}`,
      status: "completed",
    }));

    const draft = compactConversationDraft(runs);

    expect(draft).toContain("压缩上下文");
    expect(draft).toContain("question-19");
    expect(draft).not.toContain("question-0 ");
    expect(draft.length).toBeLessThanOrEqual(5_000);
  });
});

describe("composer settings light surface CSS contract", () => {
  const stylesCss = readFileSync("src/styles.css", "utf8");

  function rule(selector: string) {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return Array.from(
      stylesCss.matchAll(new RegExp(`${escaped}(?:\\s*,[^{}]+)?\\s*\\{([^}]*)\\}`, "g")),
      (match) => match[1],
    ).join("\n");
  }

  it("keeps inherited titles, paths and helper text readable only inside the light dialog", () => {
    const dialog = rule(".composer-settings-dialog");
    expect(dialog).toContain("--text: #1f2937;");
    expect(dialog).toContain("--ink: #1f2937;");
    expect(dialog).toContain("--muted: #475569;");
    expect(dialog).toContain("--accent: #0e7490;");
    expect(rule(":root")).toContain("--text: #e8fbff;");
    expect(rule(":root")).toContain("--accent: #55d7ff;");
  });

  it("gives sandbox choices and secondary actions light backgrounds and visible boundaries", () => {
    const controls = rule(".composer-settings-dialog .secondary-action");
    expect(controls).toContain("background: #ffffff;");
    expect(controls).toContain("border-color: #64748b;");
    expect(controls).toContain("box-shadow: none;");
    expect(controls).toContain("color: var(--text);");
    expect(rule(".composer-settings-dialog .sandbox-choice")).toBe(controls);
    expect(rule(".composer-settings-dialog .sandbox-choice strong")).toContain("color: inherit;");
    const selected = rule(".composer-settings-dialog .sandbox-choice.selected");
    expect(selected).toContain("background: #ecfdf5;");
    expect(selected).toContain("border-color: #0f766e;");
  });

  it("keeps hover, keyboard focus, disabled controls and the details summary readable", () => {
    const hover = rule(".composer-settings-dialog .secondary-action:focus-visible:not(:disabled)");
    expect(hover).toContain("background: #f1f5f9;");
    expect(hover).toContain("border-color: var(--accent);");
    expect(rule(".composer-settings-dialog .sandbox-choice:hover:not(:disabled)")).toBe(hover);
    expect(rule(".composer-settings-dialog .sandbox-choice:focus-visible:not(:disabled)")).toBe(hover);
    expect(rule(".composer-settings-dialog .secondary-action:hover:not(:disabled)")).toBe(hover);
    const selectedHover = rule(".composer-settings-dialog .sandbox-choice.selected:focus-visible:not(:disabled)");
    expect(selectedHover).toContain("background: #d1fae5;");
    expect(selectedHover).toContain("border-color: #0f766e;");
    expect(rule(".composer-settings-dialog .sandbox-choice.selected:hover:not(:disabled)")).toBe(selectedHover);
    const disabled = rule(".composer-settings-dialog .secondary-action:disabled");
    expect(disabled).toContain("background: #f1f5f9;");
    expect(disabled).toContain("color: var(--muted);");
    expect(disabled).toContain("opacity: 1;");
    expect(disabled).toContain("box-shadow: none;");
    expect(disabled).toContain("transform: none;");
    expect(rule(".composer-settings-dialog .sandbox-choice:disabled")).toBe(disabled);
    expect(rule(".composer-settings-dialog .run-settings-panel > summary")).toContain("background: #f1f5f9;");
    expect(rule(".composer-settings-dialog .run-settings-panel > summary::after")).toContain("color: var(--muted);");
    expect(rule(".composer-settings-dialog .run-settings-panel > summary:hover")).toContain("background: #e2e8f0;");
    expect(rule(".composer-settings-dialog .run-settings-panel > summary:focus-visible")).toContain("outline: 2px solid var(--accent);");
  });
});

describe("agentInlineSummary", () => {
  it("does not disable touch scrolling while the workbench owns nested mobile drawers", () => {
    const source = readFileSync("src/pages/RunsPage.tsx", "utf8");

    expect(source).not.toContain('document.body.style.touchAction = "none"');
  });

  it("keeps the closed conversation history drawer out of the visible interaction tree", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");

    expect(stylesCss).toMatch(
      /\.conversation-list\s*{[\s\S]*pointer-events:\s*none;[\s\S]*transform:\s*translateX\(calc\(100% \+ 1\.2rem\)\);[\s\S]*visibility:\s*hidden;/,
    );
    expect(stylesCss).toMatch(
      /\.history-drawer-open \.conversation-list\s*{[\s\S]*pointer-events:\s*auto;[\s\S]*transform:\s*translateX\(0\);[\s\S]*visibility:\s*visible;/,
    );
  });

  it("keeps conversation file previews scrollable when mobile drawers lock page scrolling", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");

    expect(stylesCss).toMatch(
      /\.conversation-file-preview-drawer\s*{[\s\S]*display:\s*grid;[\s\S]*grid-template-rows:\s*auto auto minmax\(0,\s*1fr\);[\s\S]*height:\s*min\(98dvh,\s*1040px\);[\s\S]*overflow:\s*hidden;/,
    );
    expect(stylesCss).toMatch(
      /\.conversation-file-preview-drawer \.agent-workbench-file-preview\s*{[\s\S]*min-height:\s*0;[\s\S]*overflow-y:\s*auto;[\s\S]*overscroll-behavior:\s*contain;/,
    );
    expect(stylesCss).toMatch(
      /\.conversation-file-preview-drawer \.agent-workbench-file-code\s*{[\s\S]*max-height:\s*none;[\s\S]*min-height:\s*0;/,
    );
  });

  it("keeps every light process surface on high-contrast semantic text colors", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");
    const lightSurfaceTokens = stylesCss.match(
      /\/\* Light process surface semantic colors\. \*\/([\s\S]*?)\n}/,
    )?.[0] ?? "";

    for (const selector of [
      ".process-drawer",
      ".process-detail-modal",
      ".agent-workbench-file-preview",
      ".agent-workbench-file-list button",
      ".agent-workbench-preview-pane",
      ".agent-workbench-action-files button",
      ".artifact-file-card",
      ".conversation-artifact-summary",
      ".conversation-file-link",
    ]) {
      expect(lightSurfaceTokens).toContain(selector);
    }
    expect(lightSurfaceTokens).toContain("--text: #0f172a;");
    expect(lightSurfaceTokens).toContain("--muted: #475569;");
    expect(lightSurfaceTokens).toContain("--accent: #0e7490;");
    expect(lightSurfaceTokens).toContain("color: var(--text);");
    expect(stylesCss).toMatch(
      /\.artifact-file-main small,\s*\.artifact-file-meta\s*{[^}]*color:\s*#475569;/,
    );
    expect(stylesCss).toMatch(
      /\.process-drawer small,\s*\.process-detail-modal small\s*{[^}]*font-size:\s*0\.78rem;[^}]*line-height:\s*1\.45;/,
    );
    expect(stylesCss).toMatch(
      /\.process-drawer dt,\s*\.process-detail-modal dt\s*{[^}]*font-size:\s*0\.78rem;/,
    );
    expect(stylesCss).toMatch(
      /\.process-drawer \.eyebrow,\s*\.process-detail-modal \.eyebrow\s*{[^}]*font-size:\s*0\.78rem;[^}]*line-height:\s*1\.4;/,
    );
    expect(stylesCss).toMatch(
      /\.process-drawer \.agent-workbench-action-meta span\s*{[^}]*font-size:\s*0\.75rem;/,
    );
    expect(stylesCss).toMatch(
      /\.artifact-file-icon\s*{[^}]*color:\s*var\(--accent\);[^}]*font-size:\s*0\.78rem;/,
    );
    expect(stylesCss).toMatch(
      /\.artifact-file-main small\s*{[^}]*color:\s*var\(--muted\);[^}]*font-size:\s*0\.78rem;[^}]*line-height:\s*1\.45;/,
    );
    expect(stylesCss).toMatch(
      /\.artifact-file-meta span:not\(:last-child\)::after\s*{[^}]*color:\s*#94a3b8;/,
    );
    expect(stylesCss).toMatch(
      /\.conversation-file-link small\s*{[^}]*font-size:\s*0\.78rem;[^}]*line-height:\s*1\.4;/,
    );
    expect(stylesCss).toMatch(
      /\.conversation-file-link span\s*{[^}]*font-size:\s*0\.78rem;[^}]*line-height:\s*1\.4;/,
    );
  });

  it("opens process detail expansions as full reading sheets instead of cramped cards", () => {
    const stylesCss = readFileSync("src/styles.css", "utf8");
    const source = readFileSync("src/pages/RunsPage.tsx", "utf8");
    const sharedSource = readFileSync("src/components/OutputContent.tsx", "utf8");

    expect(sharedSource).toContain('shouldCollapse && expanded ? " is-expanded"');
    expect(sharedSource).toContain("aria-expanded={expanded}");
    expect(sharedSource).toContain("aria-controls={contentId}");
    expect(sharedSource).toContain("contentRef.current?.focus()");
    expect(sharedSource).toContain("tabIndex={0}");
    expect(source).toContain("shouldCollapse={presentation.shouldCollapse}");
    expect(stylesCss).toMatch(
      /\.process-detail-modal\s*{[\s\S]*height:\s*min\(96dvh,\s*1040px\);[\s\S]*width:\s*min\(98vw,\s*1280px\);/,
    );
    expect(stylesCss).toMatch(
      /\.process-detail-modal:has\(\.bounded-text-block\.is-expanded\)\s*{[\s\S]*height:\s*min\(98dvh,\s*1120px\);[\s\S]*width:\s*min\(99vw,\s*1360px\);/,
    );
    expect(stylesCss).toMatch(
      /\.bounded-text-block\.is-expanded pre\s*{[\s\S]*max-height:\s*none;/,
    );
    expect(stylesCss).toMatch(
      /\.process-detail-modal:has\(\.bounded-text-block\.is-expanded\) dl\s*{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*overflow:\s*hidden;/,
    );
    expect(stylesCss).toMatch(
      /\.process-detail-modal:has\(\.bounded-text-block\.is-expanded\) dl > dd:has\(\.bounded-text-block\.is-expanded\)\s*{[^}]*display:\s*block;[^}]*flex:\s*1 1 auto;[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;/,
    );
    expect(stylesCss).toMatch(
      /\.process-detail-modal \.bounded-text-block\.is-expanded\s*{[^}]*height:\s*100%;[^}]*min-height:\s*0;/,
    );
    expect(stylesCss).toMatch(
      /\.process-detail-modal \.bounded-text-block\.is-expanded pre\s*{[^}]*min-height:\s*0;/,
    );
    expect(stylesCss).toMatch(
      /\.bounded-text-block::after\s*{[^}]*border:\s*1px solid var\(--bounded-text-border\);[^}]*inset:\s*0;[^}]*pointer-events:\s*none;[^}]*position:\s*absolute;/,
    );
    expect(stylesCss).toMatch(/\.bounded-text-block pre\s*{[^}]*overscroll-behavior:\s*contain;/);
    expect(stylesCss).toMatch(
      /@media \(max-width: 640px\)[\s\S]*\.process-detail-modal-backdrop\s*{[\s\S]*align-items:\s*stretch;[\s\S]*padding:\s*env\(safe-area-inset-top\) 0 0;/,
    );
    expect(stylesCss).toMatch(
      /@media \(max-width: 640px\)[\s\S]*\.process-detail-modal\s*{[\s\S]*height:\s*calc\(100dvh - env\(safe-area-inset-top\)\);[\s\S]*width:\s*100vw;/,
    );
  });

  it("closes the history drawer when starting a new blank conversation", () => {
    const source = readFileSync("src/pages/RunsPage.tsx", "utf8");
    const startNewConversationMatch = source.match(/function startNewConversation\(\) \{[\s\S]*?\n  \}/);
    const projectConversationMatch = source.match(
      /function startNewConversationForProject\([\s\S]*?\n  \}\n\n  function startNewConversation\(\)/,
    );

    expect(startNewConversationMatch?.[0]).toContain("startNewConversationForProject()");
    expect(projectConversationMatch?.[0]).toContain("setHistoryOpen(false)");
  });

  it("keeps approval cards cancellable when an action would otherwise proceed", () => {
    const source = readFileSync("src/pages/RunsPage.tsx", "utf8");

    expect(source).toContain("cancelProjectPreflightApproval");
    expect(source).toContain("取消预检");
    expect(source).toContain("cancelOpenClawApproval");
    expect(source).toContain("取消操作建议");
  });

  it("explains scheduled context-loading agents with a compact action summary", () => {
    expect(
      agentInlineSummary({
        id: "context_loader",
        name: "Context Loader",
        role: "Context Loader",
        model: "默认模型",
        summary: "等待执行分配任务",
        purpose: "",
        taskInputs: [],
        dependencyInputs: [],
        toolInputs: [],
        status: "已安排",
      }),
    ).toBe("加载上下文与约束");
  });
});

describe("workspace and sandbox submission helpers", () => {
  it("uses the delivery filename rather than the producing agent as the preview path", () => {
    const run: RunDetail = {
      ...baseRun,
      artifacts: [{
        id: "delivery-name", kind: "tool_result", title: "implementer", text: null,
        filename: "项目交付-20261022.zip", mime_type: "application/zip",
        download_url: "/api/v1/runs/run-1/artifacts/delivery-name/download",
      }],
      events: [],
    };
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, []);
    expect(files[0]?.filename).toBe("项目交付-20261022.zip");
    expect(files[0]?.path).toBe("项目交付-20261022.zip");
  });

  it("groups current conversation downloadable files by final and intermediate artifacts", () => {
    const firstRun: RunDetail = {
      ...baseRun,
      id: "11111111-1111-4111-8111-111111111111",
      created_at: "2026-09-02T00:01:00Z",
      artifacts: [
        {
          id: "final-zip",
          kind: "tool_result",
          title: "源码包",
          text: null,
          filename: "demo.zip",
          mime_type: "application/zip",
          size_bytes: 128,
          sha256: "a".repeat(64),
          download_url: "/api/v1/workspaces/projects/project/sessions/session/bundle/download",
          presentation: "final_attachment",
        },
      ],
      events: [
        {
          sequence: 1,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-09-02T00:01:30Z",
          participants: [],
          payload: {},
          artifact: {
            id: "draft-md",
            kind: "tool_result",
            title: "草稿",
            text: null,
            filename: "draft.md",
            mime_type: "text/markdown",
            size_bytes: 16,
            sha256: "b".repeat(64),
            download_url:
              "/api/v1/workspaces/projects/project/sessions/session/files/download?path=draft.md",
            presentation: "step_detail",
          },
        },
      ],
    };
    const secondRun: RunDetail = {
      ...baseRun,
      id: "22222222-2222-4222-8222-222222222223",
      created_at: "2026-09-02T00:02:00Z",
      artifacts: [
        {
          id: "final-zip-copy",
          kind: "tool_result",
          title: "源码包副本",
          text: null,
          filename: "demo.zip",
          mime_type: "application/zip",
          size_bytes: 128,
          sha256: "a".repeat(64),
          download_url: "/api/v1/workspaces/projects/project/sessions/session/bundle/download",
          presentation: "final_attachment",
        },
      ],
    };

    const files = conversationWorkspaceFiles([secondRun, firstRun]);

    expect(files.final.map((artifact) => artifact.filename)).toEqual(["demo.zip"]);
    expect(files.intermediate.map((artifact) => artifact.filename)).toEqual(["draft.md"]);
  });

  it("projects event workspace files into the workbench file list", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "创建文件 src/app.ts",
          created_at: "2026-09-02T00:01:30Z",
          participants: [],
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
                text: "raw file text should not be used",
              },
            ],
          },
        },
      ],
    };

    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, []);

    expect(files.map((file) => file.path)).toEqual(["src/app.ts"]);
    expect(files[0]?.operation).toBe("创建文件");
    expect(files[0]?.text).toBe("");
  });

  it("links workspace file previews back to the agent action that produced them", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "implementer 创建文件 src/app.ts",
          created_at: "2026-09-02T00:01:30Z",
          actor: "implementer",
          participants: [],
          tool_name: "workspace.write_file",
          step_id: "write-app",
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                operation_kind: "file_create",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, items);

    expect(files[0]?.source?.sourceActor).toBe("implementer");
    expect(files[0]?.source?.sourceStepId).toBe("write-app");
    expect(files[0]?.source?.message).toContain("src/app.ts");
  });

  it("keeps repeated workspace file operations on the same path tied to each producing action", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "implementer 创建文件 src/app.ts",
          created_at: "2026-09-02T00:01:30Z",
          actor: "implementer",
          participants: [],
          tool_name: "workspace.write_file",
          step_id: "write-app",
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                operation_kind: "file_create",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
        },
        {
          sequence: 2,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "tester 编辑文件 src/app.ts",
          created_at: "2026-09-02T00:02:30Z",
          actor: "tester",
          participants: [],
          tool_name: "workspace.patch_file",
          step_id: "patch-app",
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                operation_kind: "file_edit",
                mime_type: "text/typescript",
                size_bytes: 768,
                sha256: "c".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, items);

    expect(files.map((file) => `${file.operation}:${file.source?.sourceActor}:${file.source?.sourceStepId}`)).toEqual([
      "创建文件:implementer:write-app",
      "编辑文件:tester:patch-app",
    ]);
  });

  it("uses workspace file operation metadata before event text fallbacks", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "创建文件 src/app.ts",
          created_at: "2026-09-02T00:01:30Z",
          participants: [],
          payload: {
            workspace_files: [
              {
                path: "src/app.ts",
                filename: "app.ts",
                operation_kind: "file_edit",
                mime_type: "text/typescript",
                size_bytes: 512,
                sha256: "b".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/app.ts",
              },
            ],
          },
        },
      ],
    };

    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, []);

    expect(files.map((file) => `${file.filename}:${file.operation}`)).toEqual(["app.ts:编辑文件"]);
  });

  it("summarizes agent actions by concrete file operation and target path", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "implementer 写入源码文件",
          created_at: "2026-09-02T00:01:30Z",
          actor: "implementer",
          participants: [],
          tool_name: "workspace.write_file",
          step_id: "write-storage",
          payload: {
            workspace_files: [
              {
                path: "src/storage/cloud-drive.ts",
                filename: "cloud-drive.ts",
                operation_kind: "file_create",
                mime_type: "text/typescript",
                size_bytes: 2048,
                sha256: "d".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src/storage/cloud-drive.ts",
              },
            ],
          },
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const action = items.find((item) => item.sourceActor === "implementer" && item.sourceStepId === "write-storage");
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, items);
    const descriptor = workbenchActionDescriptor(action!, files.filter((file) => file.source?.id === action?.id));

    expect(descriptor.operation).toBe("创建文件");
    expect(descriptor.target).toBe("src/storage/cloud-drive.ts");
    expect(descriptor.summary).toContain("写入源码文件");
    expect(descriptor.meta).toEqual(expect.arrayContaining(["implementer", "创建文件", "src/storage/cloud-drive.ts"]));
  });

  it("localizes backend English fallback summaries for workbench action rows", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "step.started",
          message: "architect recorded step.started",
          summary: "architect recorded step.started",
          created_at: "2026-09-02T00:01:30Z",
          actor: "architect",
          participants: [],
          step_id: "architect-step",
          payload: {},
        },
        {
          sequence: 2,
          kind: "harness.started",
          message: "Main Agent selected the runtime mode, roles, and models.",
          summary: "Main Agent selected the runtime mode, roles, and models.",
          created_at: "2026-09-02T00:02:30Z",
          actor: "main_agent",
          participants: [],
          payload: {},
        },
      ],
    };

    const items = runProcessItems(run, new Map([["architect", "林衡 · 审查"]]));
    const architect = items.find((item) => item.sourceActor === "architect");
    const mainAgent = items.find((item) => item.sourceActor === "main_agent");

    expect(workbenchActionDescriptor(architect!).target).toBe("林衡 · 审查 开始执行步骤");
    expect(workbenchActionDescriptor(mainAgent!).target).toBe("主 Agent 已选择运行模式、角色和模型");
  });

  it("classifies long detail values into bounded JSON and code blocks", () => {
    expect(processDetailValuePresentation({ label: "事件内容", value: '{"items":[{"id":"calculator.evaluate"}]}' })).toEqual({
      kind: "json",
      label: "json",
      copyLabel: "复制 json 内容",
      text: '{\n  "items": [\n    {\n      "id": "calculator.evaluate"\n    }\n  ]\n}',
      shouldCollapse: false,
    });
    expect(
      processDetailValuePresentation({
        label: "命令",
        value: "npm run build\nnpm test",
      }),
    ).toMatchObject({
      kind: "code",
      label: "bash",
      copyLabel: "复制 bash 内容",
      shouldCollapse: false,
    });
  });

  it("keeps multi-file action summaries tied to the clicked action instead of only global artifacts", () => {
    const run: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 1,
          kind: "tool.completed",
          message: "tool.completed",
          summary: "tester 更新测试与配置",
          created_at: "2026-09-02T00:01:30Z",
          actor: "tester",
          participants: [],
          tool_name: "workspace.patch_file",
          step_id: "patch-tests",
          payload: {
            workspace_files: [
              {
                path: "tests/storage.test.ts",
                filename: "storage.test.ts",
                operation_kind: "file_edit",
                mime_type: "text/typescript",
                size_bytes: 1024,
                sha256: "e".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=tests/storage.test.ts",
              },
              {
                path: "vitest.config.ts",
                filename: "vitest.config.ts",
                operation_kind: "file_read",
                mime_type: "text/typescript",
                size_bytes: 640,
                sha256: "f".repeat(64),
                download_url:
                  "/api/v1/workspaces/projects/project/sessions/session/files/download?path=vitest.config.ts",
              },
            ],
          },
        },
      ],
    };

    const items = runProcessItems(run, new Map());
    const action = items.find((item) => item.sourceActor === "tester" && item.sourceStepId === "patch-tests");
    const files = workbenchFileItems([run], { final: [], intermediate: [], total: 0 }, items);
    const actionFiles = files.filter((file) => file.source?.id === action?.id);
    const descriptor = workbenchActionDescriptor(action!, actionFiles);

    expect(actionFiles.map((file) => `${file.operation}:${file.path}`)).toEqual([
      "编辑文件:tests/storage.test.ts",
      "读取文件:vitest.config.ts",
    ]);
    expect(descriptor.operation).toBe("编辑文件");
    expect(descriptor.target).toBe("tests/storage.test.ts");
  });

  it("maps sandbox profiles to bounded requested permissions", () => {
    expect(requestedPermissionsForSandbox("none")).toEqual([]);
    expect(requestedPermissionsForSandbox("read_only")).toEqual(["workspace.read"]);
    expect(requestedPermissionsForSandbox("restricted")).toEqual(["workspace.read", "command.run"]);
    expect(requestedPermissionsForSandbox("workspace_write")).toEqual([
      "workspace.read",
      "workspace.write",
      "command.run",
    ]);
  });

  it("previews the logical project session workspace path", () => {
    expect(workspacePreviewPath("Mofang Agent", "Conv Main 01")).toBe(
      "projects/mofang-agent/sessions/conv-main-01",
    );
  });
});
