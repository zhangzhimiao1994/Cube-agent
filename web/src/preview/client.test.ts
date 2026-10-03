import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("authenticated application proxy client", () => {
  it("uses the fixed owned endpoint and preserves application error statuses", async () => {
    const payload = { method: "PATCH", target: "/tasks/1?q=x", headers: [["content-type", "application/json"]] as [string, string][], body_base64: "e30=" };
    const result = { status_code: 409, headers: [["content-type", "application/json"]], body_base64: "e30=" };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(result), { status: 200 })); vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    expect(await api.requestWebPreviewApplication("owned", payload, controller.signal)).toEqual(result);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/web-previews/owned/app-request");
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: "POST", signal: controller.signal, body: JSON.stringify(payload), credentials: "include" });
  });
  it("rejects forbidden headers before transport and never retries", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(api.requestWebPreviewApplication("owned", { method: "GET", target: "/tasks", headers: [["authorization", "secret"]], body_base64: "" }, new AbortController().signal)).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
});
