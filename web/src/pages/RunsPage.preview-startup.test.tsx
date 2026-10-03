import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, type WebPreview } from "../api/client";
import { WebsiteServicePreview } from "./RunsPage";

const scope = { conversationId: "conv-preview", projectId: "project", workspaceSessionId: "session" };
const ready: WebPreview = {
  id: "preview-1", status: "ready", preview_url: "/api/v1/web-previews/preview-1/content/",
  lease_expires_at: "2026-10-04T12:00:00Z", application_transport: true,
};
const networkError = () => new ApiError("network request failed", 0, "network_error");
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => { resolve = accept; });
  return { promise, resolve };
}
const view = (conversationId = scope.conversationId) => (
  <WebsiteServicePreview scope={{ ...scope, conversationId }} title="app">
    <p>static fallback</p>
  </WebsiteServicePreview>
);
async function click(name: string) {
  await act(async () => { fireEvent.click(screen.getByRole("button", { name })); });
}
async function advance(ms = 1500) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe("website preview startup recovery", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(api, "renewWebPreview").mockResolvedValue(ready);
    vi.spyOn(api, "stopWebPreview").mockResolvedValue({ ...ready, status: "stopped" });
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

  it("recovers a lost POST response through current without replaying start", async () => {
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null).mockResolvedValue(ready);
    const start = vi.spyOn(api, "startWebPreview").mockRejectedValue(networkError());
    await act(async () => { render(view()); });
    await click("运行网站");
    expect(screen.getByTitle("app 网站预览").getAttribute("src")).toBe(ready.preview_url);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("keeps querying through absent, reset and repeated starting results", async () => {
    const starting = { ...ready, status: "starting" as const, preview_url: null };
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockResolvedValueOnce(null).mockRejectedValueOnce(networkError())
      .mockResolvedValueOnce(starting).mockResolvedValueOnce(starting).mockResolvedValue(ready);
    const start = vi.spyOn(api, "startWebPreview").mockRejectedValue(networkError());
    await act(async () => { render(view()); });
    await click("运行网站");
    expect(screen.queryByRole("alert")).toBeNull();
    for (let index = 0; index < 4; index++) await advance();
    expect(screen.getByTitle("app 网站预览")).toBeTruthy();
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("polls serially while a current response is pending", async () => {
    const pending = deferred<WebPreview | null>();
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockReturnValueOnce(pending.promise).mockResolvedValue(ready);
    vi.spyOn(api, "startWebPreview").mockRejectedValue(networkError());
    await act(async () => { render(view()); });
    await click("运行网站");
    await advance(15_000);
    expect(current).toHaveBeenCalledTimes(2);
    await act(async () => { pending.resolve(null); });
    await advance();
    expect(screen.getByTitle("app 网站预览")).toBeTruthy();
  });

  it("can continue after a starting response exhausts the query budget", async () => {
    const starting = { ...ready, status: "starting" as const, preview_url: null };
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockResolvedValue(starting);
    const start = vi.spyOn(api, "startWebPreview").mockResolvedValue(starting);
    await act(async () => { render(view()); });
    await click("运行网站");
    await advance(300_000);
    current.mockResolvedValue(ready);
    await click("继续查询");
    expect(screen.getByTitle("app 网站预览")).toBeTruthy();
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("does not revive a starting preview after stop invalidates its pending query", async () => {
    const pending = deferred<WebPreview | null>();
    const starting = { ...ready, status: "starting" as const, preview_url: null };
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockReturnValue(pending.promise);
    vi.spyOn(api, "startWebPreview").mockResolvedValue(starting);
    await act(async () => { render(view()); });
    await click("运行网站");
    await click("停止预览");
    await act(async () => { pending.resolve(ready); });
    await advance(300_000);
    expect(screen.queryByTitle("app 网站预览")).toBeNull();
    expect(current).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "运行网站" })).toBeTruthy();
  });

  it("keeps unknown after its budget and continues checking without a second POST", async () => {
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(null);
    const start = vi.spyOn(api, "startWebPreview").mockRejectedValue(networkError());
    await act(async () => { render(view()); });
    await click("运行网站");
    await advance(300_000);
    expect(screen.getByText(/启动结果尚未确认/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "运行网站" })).toBeNull();
    const calls = current.mock.calls.length;
    await advance(30_000);
    expect(current).toHaveBeenCalledTimes(calls);
    current.mockResolvedValue(ready);
    await click("继续查询");
    expect(screen.getByTitle("app 网站预览")).toBeTruthy();
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("bounds a hanging query and reuses it when continuing, ignoring the expired round", async () => {
    const pending = deferred<WebPreview | null>();
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockReturnValue(pending.promise);
    const start = vi.spyOn(api, "startWebPreview").mockRejectedValue(networkError());
    await act(async () => { render(view()); });
    await click("运行网站");
    await advance(300_000);
    expect(screen.getByText(/启动结果尚未确认/)).toBeTruthy();
    await click("继续查询");
    expect(current).toHaveBeenCalledTimes(2);
    await act(async () => { pending.resolve(ready); });
    expect(screen.getByTitle("app 网站预览")).toBeTruthy();
    expect(start).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/启动结果尚未确认/)).toBeNull();
  });

  it.each([401, 403])("stops recovery on an explicit HTTP %s permission error", async (status) => {
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockRejectedValue(new ApiError("denied", status, "permission_denied"));
    const start = vi.spyOn(api, "startWebPreview").mockRejectedValue(networkError());
    await act(async () => { render(view()); });
    await click("运行网站");
    expect(screen.getByRole("alert").textContent).toContain(`HTTP ${status}`);
    await advance(300_000);
    expect(current).toHaveBeenCalledTimes(2);
    expect(start).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "继续查询" })).toBeNull();
  });

  it.each([302, 307, 401, 403])("stops ordinary starting polling on HTTP %s", async (status) => {
    const starting = { ...ready, status: "starting" as const, preview_url: null };
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockResolvedValueOnce(starting).mockRejectedValue(new ApiError("denied", status));
    const start = vi.spyOn(api, "startWebPreview").mockResolvedValue(starting);
    await act(async () => { render(view()); });
    await click("运行网站");
    await advance();
    expect(screen.getByRole("alert").textContent).toContain(`HTTP ${status}`);
    expect(screen.queryByTitle("app 网站预览")).toBeNull();
    await advance(300_000);
    expect(current).toHaveBeenCalledTimes(3);
    expect(start).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "继续查询" })).toBeNull();
  });

  it.each([502, 503, 504])("recovers an ambiguous gateway HTTP %s without replay", async (status) => {
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockRejectedValueOnce(new ApiError("gateway unavailable", status, "invalid_error_response"))
      .mockResolvedValue(ready);
    const start = vi.spyOn(api, "startWebPreview")
      .mockRejectedValue(new ApiError("gateway unavailable", status, "invalid_error_response"));
    await act(async () => { render(view()); });
    await click("运行网站");
    await advance();
    expect(screen.getByTitle("app 网站预览")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(start).toHaveBeenCalledTimes(1);
  });

  it.each([
    [401, "unauthorized"], [403, "permission_denied"], [422, "invalid_preview_path"],
    [503, "dynamic_preview_unavailable"], [503, "preview_cleanup_pending"],
  ] as const)("does not recover a definitive start rejection HTTP %s %s", async (status, code) => {
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(null);
    vi.spyOn(api, "startWebPreview").mockRejectedValue(new ApiError("denied", status, code));
    await act(async () => { render(view()); });
    await click("运行网站");
    expect(screen.getByRole("alert").textContent).toContain(`HTTP ${status}`);
    await advance(300_000);
    expect(current).toHaveBeenCalledTimes(1);
  });

  it("ignores an old initial lookup after switching conversations", async () => {
    const pending = deferred<WebPreview | null>();
    vi.spyOn(api, "webPreviewForConversation").mockReturnValueOnce(pending.promise).mockResolvedValue(null);
    let rendered!: ReturnType<typeof render>;
    await act(async () => { rendered = render(view()); });
    await act(async () => { rendered.rerender(view("conv-next")); });
    await act(async () => { pending.resolve(ready); });
    expect(screen.queryByTitle("app 网站预览")).toBeNull();
    expect(screen.getByRole("button", { name: "运行网站" })).toBeTruthy();
  });

  it("rejects and cleans up a late start result after switching conversations", async () => {
    const pending = deferred<WebPreview>();
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(null);
    vi.spyOn(api, "startWebPreview").mockReturnValue(pending.promise);
    let rendered!: ReturnType<typeof render>;
    await act(async () => { rendered = render(view()); });
    await click("运行网站");
    await act(async () => { rendered.rerender(view("conv-next")); });
    await act(async () => { pending.resolve(ready); });
    expect(screen.queryByTitle("app 网站预览")).toBeNull();
    expect(api.stopWebPreview).toHaveBeenCalledWith(ready.id, { keepalive: false });
  });

  it.each(["switch", "unmount"])("ignores a late recovery response after %s", async (action) => {
    const pending = deferred<WebPreview | null>();
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockReturnValueOnce(pending.promise).mockResolvedValue(null);
    vi.spyOn(api, "startWebPreview").mockRejectedValue(networkError());
    let rendered!: ReturnType<typeof render>;
    await act(async () => { rendered = render(view()); });
    await click("运行网站");
    await act(async () => {
      if (action === "switch") rendered.rerender(view("conv-next"));
      else rendered.unmount();
    });
    expect(current).toHaveBeenCalledTimes(action === "switch" ? 3 : 2);
    await act(async () => { pending.resolve(ready); });
    const calls = current.mock.calls.length;
    await advance(300_000);
    expect(current).toHaveBeenCalledTimes(calls);
    expect(screen.queryByTitle("app 网站预览")).toBeNull();
    expect(api.stopWebPreview).not.toHaveBeenCalled();
  });

  it("ignores a response arriving after the recovery budget until the user continues", async () => {
    const pending = deferred<WebPreview | null>();
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValueOnce(null)
      .mockReturnValueOnce(pending.promise).mockResolvedValue(ready);
    const start = vi.spyOn(api, "startWebPreview").mockRejectedValue(networkError());
    await act(async () => { render(view()); });
    await click("运行网站");
    await advance(300_000);
    await act(async () => { pending.resolve(ready); });
    expect(screen.getByText(/启动结果尚未确认/)).toBeTruthy();
    expect(screen.queryByTitle("app 网站预览")).toBeNull();
    await click("继续查询");
    expect(screen.getByTitle("app 网站预览")).toBeTruthy();
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("prevents a rapid double click from submitting two starts", async () => {
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(null);
    const pending = deferred<WebPreview>();
    const start = vi.spyOn(api, "startWebPreview").mockReturnValue(pending.promise);
    await act(async () => { render(view()); });
    const button = screen.getByRole("button", { name: "运行网站" });
    await act(async () => { fireEvent.click(button); fireEvent.click(button); });
    expect(start).toHaveBeenCalledTimes(1);
    await act(async () => { pending.resolve(ready); });
    expect(screen.getByTitle("app 网站预览")).toBeTruthy();
  });

  it("does not resurrect a stopped preview when its renewal arrives late", async () => {
    const pending = deferred<WebPreview>();
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(ready);
    vi.mocked(api.renewWebPreview).mockReturnValue(pending.promise);
    await act(async () => { render(view()); });
    await advance(60_000);
    await click("停止预览");
    await act(async () => { pending.resolve(ready); });
    expect(screen.queryByTitle("app 网站预览")).toBeNull();
    expect(screen.getByRole("button", { name: "运行网站" })).toBeTruthy();
  });
});
