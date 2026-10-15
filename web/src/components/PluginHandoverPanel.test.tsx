import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type RunDetail } from "../api/client";
import { PluginHandoverPanel } from "./PluginHandoverPanel";

const mocks = vi.hoisted(() => ({
  me: vi.fn(), createPluginHandover: vi.fn(), pluginHandover: vi.fn(),
  cancelPluginHandover: vi.fn(), confirmPluginHandover: vi.fn(),
  workspaceFiles: vi.fn(),
  approvePlugin: vi.fn(), updatePlugin: vi.fn(), trustPluginSigningKey: vi.fn(),
}));
vi.mock("../api/client", async (original) => ({
  ...await original<typeof import("../api/client")>(), api: mocks,
}));

const runId = "55555555-5555-4555-8555-555555555555";
const artifactId = "11111111-1111-4111-8111-111111111111";
const hid = `gh-${"a".repeat(64)}`;
const creatorId = "22222222-2222-4222-8222-222222222222";
const artifact = {
  id: artifactId, kind: "generated_file", title: "plugin.zip", filename: "plugin.zip",
  mime_type: "application/zip", download_url: `/api/v1/runs/${runId}/artifacts/${artifactId}/download`,
};
const receipt = {
  schema_version: 1, handover_id: hid, run_id: runId, creator_id: creatorId,
  installer_id: null, version: 1, status: "awaiting_signature", plugin_id: "sample.plugin",
  plugin_version: "1.2.3", plan_hash: "b".repeat(64), permissions: ["network:read"],
  risk_summary: ["连接外部服务"], expires_at: "2030-01-01T00:00:00Z",
  created_at: "2026-10-13T00:00:00Z", updated_at: "2026-10-13T00:00:00Z",
};
const admin = { user_id: creatorId, tenant_id: artifactId, username: "admin", role: "admin",
  permissions: ["run:create", "run:read", "plugin:read", "plugin:write"] };

beforeEach(() => {
  vi.resetAllMocks();
  mocks.me.mockResolvedValue(admin);
  mocks.createPluginHandover.mockResolvedValue(receipt);
  mocks.pluginHandover.mockResolvedValue(receipt);
  mocks.cancelPluginHandover.mockResolvedValue({ ...receipt, version: 2, status: "cancelled" });
  mocks.confirmPluginHandover.mockResolvedValue({ ...receipt, version: 2, status: "pending" });
  mocks.workspaceFiles.mockResolvedValue(workspaceList());
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function mount(artifacts: RunDetail["artifacts"] = [artifact]) {
  render(<PluginHandoverPanel runId={runId} artifacts={artifacts} />);
}
const workspaceScope = { projectId: "project-a", sessionId: "session-a" };
function workspaceList(paths = ["plugin.json", "src/main.py"], scope = workspaceScope) {
  const base = `/api/v1/workspaces/projects/${scope.projectId}/sessions/${scope.sessionId}`;
  return { items: paths.map((path) => ({ path, filename: path.split("/").at(-1),
    mime_type: "text/plain", size_bytes: 12, sha256: "c".repeat(64),
    download_url: `${base}/files/download?path=${encodeURIComponent(path)}` })),
  bundle_download_url: `${base}/bundle/download` };
}
function mountWorkspace(scope: typeof workspaceScope | null = workspaceScope) {
  return render(<PluginHandoverPanel runId={runId} artifacts={[artifact]} workspaceScope={scope ?? undefined} />);
}
async function chooseWorkspace() {
  const user = await open();
  await user.click(screen.getByRole("radio", { name: "工作区文件" }));
  return user;
}

describe("workspace creation", () => {
  it("lists only after opening, permission and explicit source selection; creates selected run scope", async () => {
    mountWorkspace();
    expect(mocks.workspaceFiles).not.toHaveBeenCalled();
    const user = await open();
    expect(mocks.workspaceFiles).not.toHaveBeenCalled();
    await user.click(screen.getByRole("radio", { name: "工作区文件" }));
    const file = await screen.findByRole("checkbox", { name: "plugin.json" });
    expect(mocks.workspaceFiles).toHaveBeenCalledWith("project-a", "session-a");
    expect(screen.getByRole("button", { name: "创建安装计划" }).hasAttribute("disabled")).toBe(true);
    await user.click(file);
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByText("等待签名归档");
    expect(mocks.createPluginHandover).toHaveBeenCalledWith(runId, {
      source: { kind: "workspace", project_id: "project-a", session_id: "session-a", paths: ["plugin.json"] },
      expires_at: expect.any(String),
    }, expect.stringMatching(/^handover-[a-f0-9]{32}$/));
    expect(mocks.confirmPluginHandover).not.toHaveBeenCalled();
    expect(mocks.approvePlugin).not.toHaveBeenCalled();
    expect(mocks.updatePlugin).not.toHaveBeenCalled();
    expect(mocks.trustPluginSigningKey).not.toHaveBeenCalled();
  });

  it.each([null, { projectId: "", sessionId: "session-a" },
    { projectId: " project-a", sessionId: "session-a" }, { projectId: "project-a", sessionId: "../other" },
    { projectId: "project-a\n", sessionId: "session-a" }, { projectId: "project-a", sessionId: "session-a\n" },
    { projectId: "x".repeat(129), sessionId: "session-a" }])("refuses missing/invalid scope %j without reads or POST", async (scope) => {
    mountWorkspace(scope);
    await chooseWorkspace();
    expect(screen.getByText(/当前运行没有可用的工作区范围/)).not.toBeNull();
    expect(mocks.workspaceFiles).not.toHaveBeenCalled();
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
  });

  it.each([[], ["run:read"], ["run:read", "run:create"], ["plugin:read", "run:create"]].map((permissions) => ({ permissions })))("does not list without current creation permissions $permissions", async ({ permissions }) => {
    mocks.me.mockResolvedValue({ ...admin, permissions });
    mountWorkspace();
    await chooseWorkspace();
    expect(mocks.workspaceFiles).not.toHaveBeenCalled();
    expect(screen.getByText(/当前账号无创建计划权限/)).not.toBeNull();
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
  });

  it("lets an ordinary operator create but keeps signing disabled", async () => {
    mocks.me.mockResolvedValue({ ...admin, role: "operator", permissions: ["run:read", "run:create", "plugin:read"] });
    mountWorkspace();
    const user = await chooseWorkspace();
    await user.click(await screen.findByRole("checkbox", { name: "src/main.py" }));
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByText("等待签名归档");
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "取消计划" }).hasAttribute("disabled")).toBe(false);
  });

  it.each([
    { projectId: "project:a", sessionId: "session-a" },
    { projectId: "project-a", sessionId: "session:a" },
  ])("rejects a backend-normalized download scope instead of rewriting current run %j", async (scope) => {
    mocks.workspaceFiles.mockResolvedValue(workspaceList());
    mountWorkspace(scope);
    await chooseWorkspace();
    await screen.findByRole("alert");
    expect(mocks.workspaceFiles).toHaveBeenCalledWith(scope.projectId, scope.sessionId);
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
  });

  it("handles loading, empty, safe error and explicit retry/back without creating", async () => {
    let finish!: (value: unknown) => void;
    mocks.workspaceFiles.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    mountWorkspace();
    const user = await chooseWorkspace();
    expect(screen.getByRole("status").textContent).toContain("正在读取工作区文件");
    await act(async () => finish(workspaceList([])));
    expect(screen.getByText("此工作区暂无可用于计划的文件")).not.toBeNull();
    mocks.workspaceFiles.mockRejectedValueOnce(new Error("PRIVATE root SQL token"));
    await user.click(screen.getByRole("button", { name: "重试读取文件" }));
    expect((await screen.findByRole("alert")).textContent).not.toContain("PRIVATE");
    expect(screen.getByRole("button", { name: "创建安装计划" }).hasAttribute("disabled")).toBe(true);
    await user.click(screen.getByRole("radio", { name: "ZIP 产物" }));
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
    await user.click(screen.getByRole("radio", { name: "工作区文件" }));
    await screen.findByRole("checkbox", { name: "plugin.json" });
    expect(mocks.workspaceFiles).toHaveBeenCalledTimes(3);
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
  });

  it.each(["../escape", "/absolute", "a\\b", "a%2fb", "a:b", "a//b", "a/./b", "a\u0000b", "a\u007fb",
    "a".repeat(256), "a. ", "CON", "dir/NUL.txt", "dir/file.", "a?b", "a|b"])("rejects unsafe workspace path %j", async (path) => {
    mocks.workspaceFiles.mockResolvedValue(workspaceList(["plugin.json", path]));
    mountWorkspace();
    await chooseWorkspace();
    await screen.findByRole("alert");
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
  });

  it.each(["duplicate", "case-alias", "parent-file", "project", "session", "bundle", "foreign", "path", "query", "fragment"])("rejects invalid workspace listing %s", async (kind) => {
    const list = workspaceList(kind === "duplicate" ? ["plugin.json", "plugin.json"]
      : kind === "case-alias" ? ["plugin.json", "Plugin.JSON"]
        : kind === "parent-file" ? ["src", "src/main.py"] : ["plugin.json"]);
    if (kind === "project") list.items[0].download_url = list.items[0].download_url.replace("project-a", "project-b");
    if (kind === "session") list.items[0].download_url = list.items[0].download_url.replace("session-a", "session-b");
    if (kind === "bundle") list.bundle_download_url = list.bundle_download_url.replace("session-a", "session-b");
    if (kind === "foreign") list.items[0].download_url = `https://foreign.test${list.items[0].download_url}`;
    if (kind === "path") list.items[0].download_url = list.items[0].download_url.replace("plugin.json", "other.json");
    if (kind === "query") list.items[0].download_url += "&root=other";
    if (kind === "fragment") list.items[0].download_url += "#other";
    mocks.workspaceFiles.mockResolvedValue(list);
    mountWorkspace();
    await chooseWorkspace();
    await screen.findByRole("alert");
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
  });

  it("accepts at most 200 selected paths without increasing the server limit", async () => {
    mocks.workspaceFiles.mockResolvedValue(workspaceList(Array.from({ length: 201 }, (_, i) => `file-${i}.txt`)));
    mountWorkspace();
    const user = await chooseWorkspace();
    await screen.findByRole("checkbox", { name: "file-200.txt" });
    const files = screen.getAllByRole("checkbox") as HTMLInputElement[];
    for (const file of files.slice(0, 200)) fireEvent.click(file);
    expect(files[200].disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByText("等待签名归档");
    expect(mocks.createPluginHandover.mock.calls[0][1].source.paths).toHaveLength(200);
    expect(mocks.createPluginHandover.mock.calls[0][1].source.paths).not.toContain("file-200.txt");
  });

  it.each([
    "\u{1F600}".repeat(150),
    Array.from({ length: 8 }, () => "\u{1F600}".repeat(31)).join("/"),
  ])("preserves legal Unicode paths up to 255 code points (%#)", async (path) => {
    mocks.workspaceFiles.mockResolvedValue(workspaceList([path]));
    mountWorkspace();
    const user = await chooseWorkspace();
    await user.click(await screen.findByRole("checkbox", { name: path }));
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByText("等待签名归档");
    expect(mocks.createPluginHandover.mock.calls[0][1].source.paths).toEqual([path]);
  });

  it("rejects a 256-code-point Unicode path without offering any selection", async () => {
    const path = Array.from({ length: 8 }, () => "\u{1F600}".repeat(31)).join("/") + "a";
    mocks.workspaceFiles.mockResolvedValue(workspaceList([path]));
    mountWorkspace();
    await chooseWorkspace();
    await screen.findByRole("alert");
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
  });

  it.each(["button", "backdrop", "escape"])("preserves UNKNOWN workspace input/key across %s and changed props", async (method) => {
    mocks.createPluginHandover.mockRejectedValueOnce(new ApiError("private", 0, "network_error"));
    const view = mountWorkspace();
    const user = await chooseWorkspace();
    await user.click(await screen.findByRole("checkbox", { name: "plugin.json" }));
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByRole("alert");
    const original = mocks.createPluginHandover.mock.calls[0];
    expect(screen.getByRole("radio", { name: "ZIP 产物" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByLabelText("计划到期时间").hasAttribute("disabled")).toBe(true);
    expect((screen.getByRole("checkbox", { name: "plugin.json" }) as HTMLInputElement).disabled).toBe(true);
    if (method === "button") await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
    if (method === "backdrop") fireEvent.click(screen.getByTestId("plugin-handover-backdrop"));
    if (method === "escape") fireEvent.keyDown(document, { key: "Escape" });
    view.rerender(<PluginHandoverPanel runId={runId} artifacts={[]} workspaceScope={{ projectId: "project-b", sessionId: "session-b" }} />);
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect(mocks.workspaceFiles).toHaveBeenCalledTimes(1);
    expect(mocks.createPluginHandover).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "重试创建原计划" }));
    await screen.findByText("等待签名归档");
    expect(mocks.createPluginHandover.mock.calls[1]).toEqual(original);
  });

  it.each(["close", "switch", "scope"])("ignores stale listing after %s and clears unsubmitted selection", async (kind) => {
    let finish!: (value: unknown) => void;
    mocks.workspaceFiles.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    const view = mountWorkspace();
    const user = await chooseWorkspace();
    if (kind === "close") {
      await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
      await user.click(screen.getByRole("button", { name: "插件安装计划" }));
      await screen.findByText("当前账号：admin");
    } else if (kind === "switch") {
      await user.click(screen.getByRole("radio", { name: "ZIP 产物" }));
      await user.click(screen.getByRole("radio", { name: "工作区文件" }));
    } else {
      const next = { projectId: "project-b", sessionId: "session-b" };
      mocks.workspaceFiles.mockResolvedValue(workspaceList(["plugin.json"], next));
      view.rerender(<PluginHandoverPanel runId={runId} artifacts={[artifact]} workspaceScope={next} />);
    }
    const file = await screen.findByRole("checkbox", { name: "plugin.json" });
    await user.click(file);
    await act(async () => finish(workspaceList(["old-only.py"])));
    expect(screen.queryByRole("checkbox", { name: "old-only.py" })).toBeNull();
    expect((file as HTMLInputElement).checked).toBe(true);
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect((await screen.findByRole("checkbox", { name: "plugin.json" }) as HTMLInputElement).checked).toBe(false);
  });

  it("includes file checkboxes in the keyboard focus trap", async () => {
    mountWorkspace();
    const user = await chooseWorkspace();
    const file = await screen.findByRole("checkbox", { name: "plugin.json" });
    file.focus();
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("checkbox", { name: "src/main.py" }));
    screen.getByLabelText("计划 ID").focus();
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭安装计划" }));
  });
});
async function open() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "插件安装计划" }));
  await waitFor(() => expect(mocks.me).toHaveBeenCalledTimes(1));
  await screen.findByText("当前账号：admin");
  return user;
}
async function load() {
  const user = await open();
  await user.type(screen.getByLabelText("计划 ID"), hid);
  await user.click(screen.getByRole("button", { name: "载入回执" }));
  await screen.findByText("等待签名归档");
  return user;
}
async function prepareConfirm(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("既有可信 key ID"), "reviewed-key");
  const archive = new File(["signed zip bytes"], "signed.zip", { type: "application/zip" });
  fireEvent.change(screen.getByLabelText("已签名 ZIP"), { target: { files: [archive] } });
  await user.click(screen.getByRole("checkbox", { name: "确认提交此归档，仅登记待审批插件" }));
  return archive;
}

describe("PluginHandoverPanel", () => {
  it("isolates light modal text and disabled control contrast from global styles", () => {
    const style = document.createElement("style");
    style.textContent = `section > p { color: #8aa3b2; } button:disabled { opacity: .48; }
      ${readFileSync("src/components/PluginHandoverPanel.css", "utf8")}`;
    document.head.append(style);
    try {
      render(<div className="plugin-handover-modal" data-testid="contrast-modal">
        <section className="plugin-handover-section">
          <p data-testid="contrast-p">归档信息</p>
          <dl><dt>计划</dt><dd data-testid="contrast-dd"><code data-testid="contrast-code">hash</code></dd></dl>
          <a data-testid="contrast-link" href="/mcp">审批</a>
          <button data-testid="contrast-button" disabled>提交</button>
          <select data-testid="contrast-select" disabled><option>产物</option></select>
        </section>
      </div>);
      expect(getComputedStyle(screen.getByTestId("contrast-modal")).getPropertyValue("--muted").trim()).toBe("#4a535b");
      for (const name of ["p", "dd", "code"]) {
        expect(getComputedStyle(screen.getByTestId(`contrast-${name}`)).color).toBe("rgb(36, 40, 45)");
      }
      expect(getComputedStyle(screen.getByTestId("contrast-link")).color).toBe("rgb(23, 108, 81)");
      for (const name of ["button", "select"]) {
        const computed = getComputedStyle(screen.getByTestId(`contrast-${name}`));
        expect(computed.opacity).toBe("1");
        expect(computed.color).toBe("rgb(66, 75, 85)");
        expect(computed.backgroundColor).toBe("rgb(240, 242, 244)");
      }
    } finally { style.remove(); }
  });
  it("is collapsed and performs no API call before opening", () => {
    mount();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.me).not.toHaveBeenCalled();
    expect(mocks.createPluginHandover).not.toHaveBeenCalled();
  });

  it("creates the artifact plan with frozen expiry/key and displays the real receipt", async () => {
    mount();
    const user = await open();
    fireEvent.change(screen.getByLabelText("计划到期时间"), { target: { value: "2030-01-01T08:00" } });
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByText("等待签名归档");
    expect(mocks.createPluginHandover).toHaveBeenCalledWith(runId, {
      source: { kind: "artifact", artifact_id: artifactId },
      expires_at: new Date("2030-01-01T08:00").toISOString(),
    }, expect.stringMatching(/^handover-[a-f0-9]{32}$/));
    for (const text of ["sample.plugin", "1.2.3", "network:read", "连接外部服务", receipt.plan_hash]) {
      expect(screen.getByText(text)).not.toBeNull();
    }
    expect(mocks.confirmPluginHandover).not.toHaveBeenCalled();
  });

  it.each([
    { ...artifact, id: "not-a-uuid" },
    { ...artifact, filename: "note.txt", title: "note.txt", mime_type: "text/plain" },
    { ...artifact, download_url: "/outside/plugin.zip" },
    { ...artifact, download_url: null },
  ])("does not offer an untrusted or non-ZIP source", async (bad) => {
    mount([bad]);
    await open();
    expect(screen.queryByRole("button", { name: "创建安装计划" })).toBeNull();
    expect(screen.getByText("此运行暂无可用于计划的 ZIP 产物")).not.toBeNull();
    expect(screen.getByRole("button", { name: "载入回执" }).hasAttribute("disabled")).toBe(true);
  });

  it("loads run-local receipts without needing an artifact and cancels only explicitly", async () => {
    mount([]);
    const user = await load();
    expect(mocks.pluginHandover).toHaveBeenCalledWith(runId, hid);
    expect(mocks.cancelPluginHandover).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "取消计划" }));
    await screen.findByText("已取消");
    expect(mocks.cancelPluginHandover).toHaveBeenCalledWith(runId, hid, {
      expected_version: 1, plan_hash: receipt.plan_hash,
    });
  });

  it("submits a signed ZIP only after explicit checkbox and never approves/enables/trusts", async () => {
    mount();
    const user = await load();
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
    const archive = await prepareConfirm(user);
    await user.click(screen.getByRole("button", { name: "提交待审批" }));
    await screen.findByText("待审批，插件仍禁用，尚不可调用。");
    expect(mocks.confirmPluginHandover).toHaveBeenCalledWith(runId, hid, {
      expected_version: 1, plan_hash: receipt.plan_hash, key_id: "reviewed-key",
    }, archive, expect.stringMatching(/^handover-[a-f0-9]{32}$/));
    expect(screen.getByRole("link", { name: "前往插件审批" }).getAttribute("href")).toBe("/mcp");
    expect(mocks.approvePlugin).not.toHaveBeenCalled();
    expect(mocks.updatePlugin).not.toHaveBeenCalled();
    expect(mocks.trustPluginSigningKey).not.toHaveBeenCalled();
  });

  it("blocks simultaneous clicks and further confirmation once pending", async () => {
    let finish!: (value: unknown) => void;
    mocks.confirmPluginHandover.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    mount();
    const user = await load();
    await prepareConfirm(user);
    const button = screen.getByRole("button", { name: "提交待审批" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
    finish({ ...receipt, version: 2, status: "pending" });
    await screen.findByText("待审批，插件仍禁用，尚不可调用。");
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
  });

  it.each([2_000_000, 2_000_001])("enforces the exact backend archive boundary at %i bytes", async (size) => {
    mount();
    const user = await load();
    await user.type(screen.getByLabelText("既有可信 key ID"), "reviewed-key");
    const archive = new File([new Uint8Array(size)], "signed.zip", { type: "application/zip" });
    fireEvent.change(screen.getByLabelText("已签名 ZIP"), { target: { files: [archive] } });
    await user.click(screen.getByRole("checkbox", { name: "确认提交此归档，仅登记待审批插件" }));
    const button = screen.getByRole("button", { name: "提交待审批" });
    if (size === 2_000_000) {
      expect(button.hasAttribute("disabled")).toBe(false);
      await user.click(button);
      expect(mocks.confirmPluginHandover).toHaveBeenCalledWith(runId, hid,
        expect.objectContaining({ key_id: "reviewed-key" }), archive, expect.any(String));
    } else {
      expect(button.hasAttribute("disabled")).toBe(true);
      expect(screen.getByRole("alert").textContent).toContain("2,000,000 字节");
      expect(mocks.confirmPluginHandover).not.toHaveBeenCalled();
    }
  });

  it("freezes create identity across error, time changes, close and explicit retry on HTTP", async () => {
    mocks.createPluginHandover.mockRejectedValueOnce(new ApiError("unsafe text", 503, "service_unavailable"));
    const random = vi.spyOn(crypto, "getRandomValues");
    mount();
    const user = await open();
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByRole("alert");
    const first = mocks.createPluginHandover.mock.calls[0];
    expect(screen.getByLabelText("计划到期时间").hasAttribute("disabled")).toBe(true);
    await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect(mocks.createPluginHandover).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "重试创建原计划" }));
    await screen.findByText("等待签名归档");
    expect(mocks.createPluginHandover.mock.calls[1]).toEqual(first);
    expect(random).toHaveBeenCalled();
    random.mockRestore();
    expect(screen.queryByText(/unsafe text/)).toBeNull();
  });

  it.each([
    new ApiError("unsafe SQL", 503, "service_unavailable"),
    new ApiError("unsafe SQL", 409, "conflict"),
    new ApiError("unsafe SQL", 403, "forbidden"),
    new ApiError("unsafe SQL", 0, "network_error"),
    new Error("unsafe SQL"),
  ])("does not retry a failed confirmation; only explicit receipt refresh resolves it", async (error) => {
    mocks.confirmPluginHandover.mockRejectedValue(error);
    mount();
    const user = await load();
    await prepareConfirm(user);
    await user.click(screen.getByRole("button", { name: "提交待审批" }));
    expect((await screen.findByRole("alert")).textContent).not.toContain("unsafe SQL");
    expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "取消计划" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
    expect(mocks.pluginHandover).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(true);
    mocks.pluginHandover.mockRejectedValueOnce(new ApiError("unsafe GET", 503, "service_unavailable"));
    await user.click(screen.getByRole("button", { name: "刷新当前回执" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(true);
    mocks.pluginHandover.mockResolvedValue({ ...receipt, version: 2, status: "pending" });
    await user.click(screen.getByRole("button", { name: "刷新当前回执" }));
    await screen.findByText("待审批，插件仍禁用，尚不可调用。");
    expect(mocks.pluginHandover).toHaveBeenCalledTimes(3);
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
    expect(mocks.approvePlugin).not.toHaveBeenCalled();
    expect(mocks.updatePlugin).not.toHaveBeenCalled();
    expect(mocks.trustPluginSigningKey).not.toHaveBeenCalled();
  });

  it.each(["planned", "awaiting_signature"])("shows frozen consent and explicitly retries the original request after GET %s", async (status) => {
    mocks.confirmPluginHandover.mockRejectedValueOnce(new ApiError("unsafe", 0, "network_error"));
    const random = vi.spyOn(crypto, "getRandomValues");
    mount();
    const user = await load();
    const archive = await prepareConfirm(user);
    await user.click(screen.getByRole("button", { name: "提交待审批" }));
    await screen.findByRole("alert");
    const original = mocks.confirmPluginHandover.mock.calls[0];
    await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    const consent = screen.getByRole("checkbox", { name: "已确认的原归档，仅登记待审批插件" }) as HTMLInputElement;
    expect(consent.checked).toBe(true);
    expect(consent.readOnly).toBe(true);
    expect(consent.disabled).toBe(true);
    expect(screen.getByText(`本次归档：${archive.name}`)).not.toBeNull();
    expect((screen.getByLabelText("已签名 ZIP") as HTMLInputElement).files?.length).toBe(0);
    expect(screen.getByLabelText("已签名 ZIP").hasAttribute("disabled")).toBe(true);
    expect((screen.getByLabelText("既有可信 key ID") as HTMLInputElement).value).toBe(original[2].key_id);
    expect(screen.getByLabelText("既有可信 key ID").hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "取消计划" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
    expect(mocks.pluginHandover).toHaveBeenCalledTimes(1);

    mocks.pluginHandover.mockRejectedValueOnce(new ApiError("unsafe GET", 503, "service_unavailable"));
    await user.click(screen.getByRole("button", { name: "刷新当前回执" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "取消计划" }).hasAttribute("disabled")).toBe(true);
    let finish!: (value: unknown) => void;
    mocks.pluginHandover.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    await user.click(screen.getByRole("button", { name: "刷新当前回执" }));
    expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
    finish({ ...receipt, status });
    await waitFor(() => expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(false));
    expect(consent.checked).toBe(true);
    expect(consent.readOnly).toBe(true);
    expect(consent.disabled).toBe(true);
    await user.click(consent);
    expect(consent.checked).toBe(true);
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "重试原提交" }));
    await screen.findByText("待审批，插件仍禁用，尚不可调用。");
    const retry = mocks.confirmPluginHandover.mock.calls[1];
    expect(retry).toEqual(original);
    expect(retry[2]).toBe(original[2]);
    expect(retry[2]).toEqual({ expected_version: 1, plan_hash: receipt.plan_hash, key_id: "reviewed-key" });
    expect(retry[3]).toBe(archive);
    expect(retry[4]).toBe(original[4]);
    expect(random).toHaveBeenCalledTimes(1);
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.cancelPluginHandover).not.toHaveBeenCalled();
    expect(mocks.approvePlugin).not.toHaveBeenCalled();
    expect(mocks.updatePlugin).not.toHaveBeenCalled();
    expect(mocks.trustPluginSigningKey).not.toHaveBeenCalled();
  });

  it("keeps a failed cancellation locked until an explicit read, without resending", async () => {
    mocks.cancelPluginHandover.mockRejectedValue(new ApiError("unsafe", 503, "service_unavailable"));
    mount();
    const user = await load();
    await user.click(screen.getByRole("button", { name: "取消计划" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "取消计划" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.cancelPluginHandover).toHaveBeenCalledTimes(1);
    mocks.pluginHandover.mockResolvedValue({ ...receipt, status: "cancelled", version: 2 });
    await user.click(screen.getByRole("button", { name: "刷新当前回执" }));
    await screen.findByText("已取消");
    expect(mocks.cancelPluginHandover).toHaveBeenCalledTimes(1);
  });

  it.each([
    { ...receipt, run_id: "66666666-6666-4666-8666-666666666666" },
    { ...receipt, handover_id: `gh-${"c".repeat(64)}` },
  ])("rejects a receipt outside the requested identity", async (value) => {
    mocks.pluginHandover.mockResolvedValue(value);
    mount();
    const user = await open();
    await user.type(screen.getByLabelText("计划 ID"), hid);
    await user.click(screen.getByRole("button", { name: "载入回执" }));
    await screen.findByRole("alert");
    expect(screen.queryByText("安装计划回执")).toBeNull();
    expect(mocks.confirmPluginHandover).not.toHaveBeenCalled();
  });

  it.each([new File([], "empty.zip"), new File(["bytes"], "unsigned.txt")])("rejects an empty or non-ZIP archive", async (file) => {
    mount();
    const user = await load();
    await prepareConfirm(user);
    fireEvent.change(screen.getByLabelText("已签名 ZIP"), { target: { files: [file] } });
    await user.click(screen.getByRole("checkbox", { name: "确认提交此归档，仅登记待审批插件" }));
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.confirmPluginHandover).not.toHaveBeenCalled();
  });

  it("locks creation immediately against simultaneous clicks", async () => {
    let finish!: (value: unknown) => void;
    mocks.createPluginHandover.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    mount();
    await open();
    const button = screen.getByRole("button", { name: "创建安装计划" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(mocks.createPluginHandover).toHaveBeenCalledTimes(1);
    finish(receipt);
    await screen.findByText("等待签名归档");
  });

  it.each(["operator", "viewer"])("uses me permissions to disable confirmation for %s", async (role) => {
    mocks.me.mockResolvedValue({ ...admin, username: "admin", role, permissions: ["run:read", "plugin:read"] });
    mount();
    await load();
    expect(screen.getByLabelText("既有可信 key ID").hasAttribute("disabled")).toBe(true);
    expect(screen.getByLabelText("已签名 ZIP").hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
  });

  it("fails closed when permissions cannot be loaded", async () => {
    mocks.me.mockRejectedValue(new Error("secret"));
    mount();
    await userEvent.click(screen.getByRole("button", { name: "插件安装计划" }));
    expect((await screen.findByRole("alert")).textContent).toContain("账号权限读取失败");
    expect(screen.getByRole("button", { name: "创建安装计划" }).hasAttribute("disabled")).toBe(true);
  });

  it("shows unknown status without permitting mutations", async () => {
    mocks.pluginHandover.mockResolvedValue({ ...receipt, status: "unexpected" });
    mount();
    const user = await open();
    await user.type(screen.getByLabelText("计划 ID"), hid);
    await user.click(screen.getByRole("button", { name: "载入回执" }));
    await screen.findByText("未知状态，请刷新回执核实");
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "取消计划" }).hasAttribute("disabled")).toBe(true);
  });

  it("allows historical expired receipts to be read, but not mutated", async () => {
    mocks.pluginHandover.mockResolvedValue({ ...receipt, expires_at: "2020-01-01T00:00:00Z" });
    mount();
    await load();
    expect(screen.getByText("计划已到期，仅可查阅回执。")).not.toBeNull();
    expect(screen.getByRole("button", { name: "取消计划" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.confirmPluginHandover).not.toHaveBeenCalled();
  });

  it("isolates receipts and operation keys when RunDetail changes its keyed run", async () => {
    const view = render(<PluginHandoverPanel key={runId} runId={runId} artifacts={[artifact]} />);
    const user = await open();
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByText("等待签名归档");
    const firstKey = mocks.createPluginHandover.mock.calls[0][2];
    const nextRun = "66666666-6666-4666-8666-666666666666";
    const nextArtifact = { ...artifact, download_url: `/api/v1/runs/${nextRun}/artifacts/${artifactId}/download` };
    mocks.createPluginHandover.mockResolvedValue({ ...receipt, run_id: nextRun, handover_id: `gh-${"d".repeat(64)}` });
    view.rerender(<PluginHandoverPanel key={nextRun} runId={nextRun} artifacts={[nextArtifact]} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.me).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect((screen.getByLabelText("计划 ID") as HTMLInputElement).value).toBe("");
    expect(screen.queryByText("安装计划回执")).toBeNull();
    await user.click(screen.getByRole("button", { name: "创建安装计划" }));
    await screen.findByText("等待签名归档");
    expect(mocks.createPluginHandover.mock.calls[1][0]).toBe(nextRun);
    expect(mocks.createPluginHandover.mock.calls[1][2]).not.toBe(firstKey);
    expect(mocks.confirmPluginHandover).not.toHaveBeenCalled();
  });

  it("contains keyboard focus and restores body scrolling after closing", async () => {
    const previousOverflow = document.body.style.overflow;
    mount();
    const user = await open();
    expect(document.body.style.overflow).toBe("hidden");
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭安装计划" }));
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByLabelText("计划 ID"));
    await user.keyboard("{Escape}");
    expect(document.body.style.overflow).toBe(previousOverflow);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "插件安装计划" }));
  });

  it.each(["body", "document"])("handles Escape from %s after a failed confirmation loses dialog focus", async (target) => {
    mocks.confirmPluginHandover.mockRejectedValue(new ApiError("unsafe", 503, "service_unavailable"));
    mount();
    const user = await load();
    await prepareConfirm(user);
    await user.click(screen.getByRole("button", { name: "提交待审批" }));
    await screen.findByRole("alert");
    document.body.setAttribute("tabindex", "-1");
    document.body.focus();
    document.body.removeAttribute("tabindex");
    expect(document.activeElement).toBe(document.body);
    fireEvent.keyDown(target === "body" ? document.body : document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.cancelPluginHandover).not.toHaveBeenCalled();
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "插件安装计划" }));
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.confirmPluginHandover).toHaveBeenCalledTimes(1);
  });

  it("cleans up its document listener on close and unmount without handling closed Escape", async () => {
    const add = vi.spyOn(document, "addEventListener");
    const remove = vi.spyOn(document, "removeEventListener");
    const view = render(<PluginHandoverPanel runId={runId} artifacts={[artifact]} />);
    const user = await open();
    const handlers = () => add.mock.calls.filter(([name]) => name === "keydown");
    expect(handlers()).toHaveLength(1);
    const firstHandler = handlers()[0][1];
    fireEvent.keyDown(document, { key: "Escape" });
    expect(remove).toHaveBeenCalledWith("keydown", firstHandler);
    const closedEscape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    document.dispatchEvent(closedEscape);
    expect(closedEscape.defaultPrevented).toBe(false);
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect(handlers()).toHaveLength(2);
    view.unmount();
    expect(remove).toHaveBeenCalledWith("keydown", handlers()[1][1]);
  });

  it.each([false, true])("returns outside focus to the dialog on Tab (shift=%s)", async (shiftKey) => {
    mount();
    await open();
    screen.getByRole("button", { name: "插件安装计划" }).focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey });
    expect(document.activeElement).toBe(shiftKey
      ? screen.getByLabelText("计划 ID")
      : screen.getByRole("button", { name: "关闭安装计划" }));
  });

  it.each(["button", "backdrop", "escape"])("closes through %s without cancellation", async (method) => {
    const container = document.createElement("div");
    document.body.append(container);
    const view = render(<PluginHandoverPanel runId={runId} artifacts={[artifact]} />, { container });
    const user = await load();
    expect(view.container.querySelector('[role="dialog"]')).toBeNull();
    if (method === "button") await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
    if (method === "backdrop") fireEvent.click(screen.getByTestId("plugin-handover-backdrop"));
    if (method === "escape") await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.cancelPluginHandover).not.toHaveBeenCalled();
    container.remove();
  });

  it.each(["button", "backdrop", "escape"])("clears an unsubmitted file/key/checkbox draft when closing via %s", async (method) => {
    mount();
    const user = await load();
    await prepareConfirm(user);
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(false);
    if (method === "button") await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
    if (method === "backdrop") fireEvent.click(screen.getByTestId("plugin-handover-backdrop"));
    if (method === "escape") fireEvent.keyDown(document, { key: "Escape" });
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect((screen.getByLabelText("已签名 ZIP") as HTMLInputElement).files?.length).toBe(0);
    expect((screen.getByLabelText("既有可信 key ID") as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
    expect(screen.getByRole("button", { name: "提交待审批" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.confirmPluginHandover).not.toHaveBeenCalled();
    expect(mocks.cancelPluginHandover).not.toHaveBeenCalled();
  });

  it.each(["button", "backdrop", "escape"])("preserves frozen confirmation identity and UNKNOWN across closing via %s", async (method) => {
    mocks.confirmPluginHandover.mockRejectedValue(new ApiError("unsafe", 503, "service_unavailable"));
    mount();
    const user = await load();
    const file = await prepareConfirm(user);
    await user.click(screen.getByRole("button", { name: "提交待审批" }));
    await screen.findByRole("alert");
    const original = mocks.confirmPluginHandover.mock.calls[0];
    if (method === "button") await user.click(screen.getByRole("button", { name: "关闭安装计划" }));
    if (method === "backdrop") fireEvent.click(screen.getByTestId("plugin-handover-backdrop"));
    if (method === "escape") fireEvent.keyDown(document, { key: "Escape" });
    await user.click(screen.getByRole("button", { name: "插件安装计划" }));
    await screen.findByText("当前账号：admin");
    expect(screen.getByText(`本次归档：${file.name}`)).not.toBeNull();
    expect((screen.getByLabelText("既有可信 key ID") as HTMLInputElement).value).toBe("reviewed-key");
    expect(screen.getByRole("button", { name: "重试原提交" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "取消计划" }).hasAttribute("disabled")).toBe(true);
    expect(mocks.confirmPluginHandover.mock.calls).toEqual([original]);
    expect(mocks.pluginHandover).toHaveBeenCalledTimes(1);
    mocks.pluginHandover.mockResolvedValue({ ...receipt, status: "pending", version: 2 });
    await user.click(screen.getByRole("button", { name: "刷新当前回执" }));
    await screen.findByText("待审批，插件仍禁用，尚不可调用。");
    expect(mocks.confirmPluginHandover.mock.calls).toEqual([original]);
    expect(mocks.cancelPluginHandover).not.toHaveBeenCalled();
  });
});
import { readFileSync } from "node:fs";
