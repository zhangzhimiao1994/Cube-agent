import { afterEach, describe, expect, it, vi } from "vitest";
import { attachPreviewBridge, validatePreviewRequest, validatePreviewResponse } from "./transport";

afterEach(() => vi.unstubAllGlobals());
const request = { method: "POST", target: "/tasks?q=one", headers: [["content-type", "application/json"]], body_base64: "e30=" };

describe("owned application transport", () => {
  it("keeps console-looking paths inside the application", () => {
    expect(validatePreviewRequest(request)).toEqual(request);
    expect(validatePreviewRequest({ ...request, target: "/api/v1/runs" }).target).toBe("/api/v1/runs");
  });
  it.each([
    "/tasks?include_deleted=true",
    "/tasks?q=100%25",
    "/tasks?q=a%5Cb",
    "/tasks?q=%2525&literal=%250A",
    "/tasks?tag=one&tag=two&empty=&bare",
    "/tasks?plus=a+b&space=a%20b&slash=a%2Fb",
    "/tasks?q=..%2F..%2F&hash=%23&question=%3F",
    "/tasks?q=%25252525252525",
    "/tasks?",
  ])("preserves the original GET query in %s", (target) => {
    const payload = { method: "GET", target, headers: [], body_base64: "" };
    expect(validatePreviewRequest(payload)).toEqual(payload);
  });
  it.each([
    "/tasks%2Fprivate?q=allowed", "/tasks%2fprivate", "/tasks%252Fprivate",
    "/%2Ftasks", "/%252Ftasks", "/tasks%5Cprivate", "/tasks%255cprivate",
    "/./tasks", "/tasks/../private", "/%25252e%25252e/tasks",
    "/prefix%3F/../tasks", "/prefix%3F/%252e%252e/tasks",
    "/tasks%", "/tasks%2", "/tasks%GG", "/tasks%2525252541",
    "/tasks?q=raw\\value", "/tasks?q=raw value", "/tasks?q=\u00e9",
    "http://evil.test/tasks?q=one", "//evil.test/tasks?q=one",
  ])("rejects unsafe paths or raw targets despite query support: %s", (target) => {
    expect(() => validatePreviewRequest({ method: "GET", target, headers: [], body_base64: "" })).toThrow();
  });
  it.each(["%00", "%09", "%0a", "%0D", "%1F", "%7f", "\0", "\t", "\n", "\r", "\x1f", "\x7f"])("rejects control bytes in paths and queries: %j", (control) => {
    for (const target of [`/tasks${control}`, `/tasks?q=${control}`]) {
      expect(() => validatePreviewRequest({ method: "GET", target, headers: [], body_base64: "" })).toThrow();
    }
  });
  it("keeps nested path controls forbidden while query values are decoded only once", () => {
    expect(() => validatePreviewRequest({ ...request, target: "/tasks%250A?q=one" })).toThrow();
  });
  it("enforces the original target length boundary including the query", () => {
    const target = "/tasks?q=" + "x".repeat(4087);
    expect(validatePreviewRequest({ ...request, target }).target).toBe(target);
    expect(() => validatePreviewRequest({ ...request, target: target + "x" })).toThrow();
  });
  it.each(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])("preserves query values for allowed method %s", (method) => {
    const payload = { method, target: "/tasks?q=100%25", headers: [], body_base64: "" };
    expect(validatePreviewRequest(payload)).toEqual(payload);
  });
  it.each(["GET", "HEAD"])("still rejects a body for %s", (method) => {
    expect(() => validatePreviewRequest({ ...request, method, target: "/tasks?q=100%25" })).toThrow();
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
