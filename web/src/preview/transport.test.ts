import { afterEach, describe, expect, it, vi } from "vitest";
import { attachPreviewBridge, validatePreviewRequest, validatePreviewResponse } from "./transport";

afterEach(() => vi.unstubAllGlobals());
const request = { method: "POST", target: "/tasks?q=one", headers: [["content-type", "application/json"]], body_base64: "e30=" };

describe("owned application transport", () => {
  it("keeps console-looking paths inside the application", () => {
    expect(validatePreviewRequest(request)).toEqual(request);
    expect(validatePreviewRequest({ ...request, target: "/api/v1/runs" }).target).toBe("/api/v1/runs");
  });
  it.each(["https://evil.test/tasks", "//evil.test/tasks", "/../tasks", "/%2e%2e/tasks", "/%252e%252e/tasks", "/tasks#fragment", "/x\\tasks", "/tasks\n"])("rejects target %s", (target) => {
    expect(() => validatePreviewRequest({ ...request, target })).toThrow();
  });
  it.each(["authorization", "cookie", "host", "origin", "connection", "x-forwarded-host"])("rejects header %s", (name) => {
    expect(() => validatePreviewRequest({ ...request, headers: [[name, "private"]] })).toThrow();
  });
  it.each(["CONNECT", "TRACE", "get", "PURGE"])("rejects method %s", (method) => {
    expect(() => validatePreviewRequest({ ...request, method })).toThrow();
  });
  it("rejects malformed, oversized or hidden data", () => {
    expect(() => validatePreviewRequest({ ...request, body_base64: "%%" })).toThrow();
    expect(() => validatePreviewRequest({ ...request, token: "not-allowed" })).toThrow();
    expect(() => validatePreviewRequest({ ...request, body_base64: "AAAA".repeat(400_000) })).toThrow();
    expect(() => validatePreviewRequest({ ...request, headers: [["content-type", "x\r\ny"]] })).toThrow();
  });
  it("handles a full-sized binary response without recursive pattern evaluation", () => {
    const body = btoa("a".repeat(8 * 1024 * 1024));
    expect(validatePreviewResponse({ status_code: 200, headers: [], body_base64: body }).body_base64.length).toBe(body.length);
  });
  function setup() {
    const ports: { onmessage: ((event: MessageEvent) => void) | null; postMessage: ReturnType<typeof vi.fn>; start: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn> }[] = [];
    vi.stubGlobal("MessageChannel", class {
      port1 = { onmessage: null, postMessage: vi.fn(), start: vi.fn(), close: vi.fn() };
      port2 = { onmessage: null, postMessage: vi.fn(), start: vi.fn(), close: vi.fn() };
      constructor() { ports.push(this.port1); }
    });
    const child = { postMessage: vi.fn() } as unknown as Window;
    const frame = { contentWindow: child } as HTMLIFrameElement;
    const proxy = vi.fn().mockResolvedValue({ status_code: 201, headers: [["content-type", "application/json"]], body_base64: "e30=" });
    const bridge = attachPreviewBridge(frame, "preview-owned", proxy);
    const hello = (source: Window | null = child, id = "preview-owned", origin = "null") => window.dispatchEvent(new MessageEvent("message", {
      source, origin, data: { kind: "agent-preview-hello", preview_id: id },
    }));
    return { ports, child, proxy, bridge, hello };
  }
  it("ignores other windows, origins and identities", () => {
    const { ports, bridge, hello } = setup();
    hello(window); hello(undefined, "wrong"); hello(undefined, "preview-owned", "https://evil.test");
    expect(ports).toHaveLength(0); bridge.dispose();
  });
  it("dispatches once and returns application data only", async () => {
    const { ports, bridge, hello, proxy } = setup(); bridge.frameLoaded(); hello();
    ports[0].onmessage?.(new MessageEvent("message", { data: { kind: "request", id: 1, request } }));
    await vi.waitFor(() => expect(ports[0].postMessage).toHaveBeenCalled());
    expect(proxy).toHaveBeenCalledTimes(1);
    expect(proxy.mock.calls[0].slice(0, 2)).toEqual(["preview-owned", request]);
    expect(ports[0].postMessage.mock.calls[0][0]).toMatchObject({ kind: "response", id: 1, result: { status_code: 201 } });
    ports[0].onmessage?.(new MessageEvent("message", { data: { kind: "request", id: 1, request } }));
    expect(proxy).toHaveBeenCalledTimes(1); bridge.dispose();
  });
  it("revokes old ports and aborts in-flight calls on navigation", () => {
    const { ports, bridge, hello, proxy } = setup(); proxy.mockImplementation(() => new Promise(() => {})); bridge.frameLoaded(); hello();
    const handler = ports[0].onmessage;
    handler?.(new MessageEvent("message", { data: { kind: "request", id: 1, request } }));
    const signal = proxy.mock.calls[0][2] as AbortSignal;
    bridge.frameLoaded();
    expect(signal.aborted).toBe(true); expect(ports[0].close).toHaveBeenCalled();
    handler?.(new MessageEvent("message", { data: { kind: "request", id: 2, request } }));
    expect(proxy).toHaveBeenCalledTimes(1); bridge.dispose(); hello(); expect(ports).toHaveLength(1);
  });
  it("waits for document load so initialization requests are not aborted by the initial load", () => {
    const { ports, bridge, hello } = setup(); hello(); expect(ports).toHaveLength(0);
    bridge.frameLoaded(); hello(); expect(ports).toHaveLength(1); bridge.dispose();
  });
  it("old completion cannot remove a new document's same-ID request", async () => {
    const { ports, bridge, hello, proxy } = setup();
    let complete!: (value: unknown) => void;
    proxy.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
    proxy.mockImplementation(() => new Promise(() => {}));
    bridge.frameLoaded(); hello();
    ports[0].onmessage?.(new MessageEvent("message", { data: { kind: "request", id: 1, request } }));
    bridge.frameLoaded(); hello();
    ports[1].onmessage?.(new MessageEvent("message", { data: { kind: "request", id: 1, request } }));
    const signal = proxy.mock.calls[1][2] as AbortSignal;
    complete({ status_code: 200, headers: [], body_base64: "" });
    await new Promise(resolve => setTimeout(resolve, 0));
    bridge.dispose(); expect(signal.aborted).toBe(true);
  });
});
