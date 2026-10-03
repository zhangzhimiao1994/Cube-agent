export type PreviewAppRequest = {
  method: string;
  target: string;
  headers: [string, string][];
  body_base64: string;
};
export type PreviewAppResponse = {
  status_code: number;
  headers: [string, string][];
  body_base64: string;
};
type Proxy = (id: string, request: PreviewAppRequest, signal: AbortSignal) => Promise<PreviewAppResponse>;
const METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]);
const REQUEST_HEADERS = new Set(["accept", "accept-language", "content-type", "if-match", "if-none-match", "range"]);
const RESPONSE_HEADERS = new Set(["content-type", "content-language", "etag", "last-modified", "cache-control", "content-range", "accept-ranges", "location"]);
const CONTROL = /[\u0000-\u001f\u007f]/;

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("Invalid preview message");
  return value as Record<string, unknown>;
}
function onlyKeys(value: Record<string, unknown>, keys: string[]) {
  if (Object.keys(value).length !== keys.length || keys.some((key) => !(key in value))) throw new TypeError("Invalid preview fields");
}
function base64(value: unknown, maxBytes: number): string {
  if (typeof value !== "string" || value.length > 4 * Math.ceil(maxBytes / 3) || /[^A-Za-z0-9+/=]/.test(value)) throw new TypeError("Invalid preview body");
  const decoded = atob(value);
  if (decoded.length > maxBytes || btoa(decoded) !== value) throw new TypeError("Invalid preview body");
  return value;
}
function headerPairs(value: unknown, allowed: Set<string>): [string, string][] {
  if (!Array.isArray(value) || value.length > 64) throw new TypeError("Invalid preview headers");
  let size = 0;
  return value.map((pair: unknown) => {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== "string" || typeof pair[1] !== "string" || !allowed.has(pair[0].toLowerCase()) || CONTROL.test(pair[1])) throw new TypeError("Invalid preview header");
    size += pair[0].length + pair[1].length;
    if (size > 8192) throw new TypeError("Preview headers too large");
    return [pair[0].toLowerCase(), pair[1]];
  });
}
export function validatePreviewRequest(value: unknown): PreviewAppRequest {
  const data = record(value);
  onlyKeys(data, ["method", "target", "headers", "body_base64"]);
  if (typeof data.method !== "string" || !METHODS.has(data.method)) throw new TypeError("Unsupported preview method");
  if (typeof data.target !== "string" || data.target.length > 4096 || !data.target.startsWith("/") || data.target.startsWith("//") || data.target.includes("#")) throw new TypeError("Invalid preview target");
  if (/[^\u0021-\u007e]/.test(data.target) || data.target.includes("\\")) throw new TypeError("Invalid preview target");
  const queryIndex = data.target.indexOf("?");
  let path = queryIndex === -1 ? data.target : data.target.slice(0, queryIndex);
  // Query values are data: only paths undergo recursive traversal checks.
  if (queryIndex !== -1 && CONTROL.test(decodeURIComponent(data.target.slice(queryIndex + 1)))) throw new TypeError("Invalid preview target");
  for (let i = 0; i < 4; i++) {
    if (CONTROL.test(path) || path.split("/").some((part) => part === "." || part === "..")) throw new TypeError("Invalid preview target");
    const decoded = decodeURIComponent(path);
    if (decoded === path) break;
    if (decoded.split("/").length !== path.split("/").length || decoded.includes("\\")) throw new TypeError("Invalid preview target");
    if (i === 3) throw new TypeError("Excessive preview encoding");
    path = decoded;
  }
  const body = base64(data.body_base64, 1024 * 1024);
  if ((data.method === "GET" || data.method === "HEAD") && body) throw new TypeError("Unexpected preview body");
  return { method: data.method, target: data.target, headers: headerPairs(data.headers, REQUEST_HEADERS), body_base64: body };
}
export function validatePreviewResponse(value: unknown): PreviewAppResponse {
  const data = record(value);
  onlyKeys(data, ["status_code", "headers", "body_base64"]);
  if (!Number.isInteger(data.status_code) || Number(data.status_code) < 200 || Number(data.status_code) > 599) throw new TypeError("Invalid preview status");
  const headers = headerPairs(data.headers, RESPONSE_HEADERS);
  for (const [name, value] of headers) {
    if (name === "location") validatePreviewRequest({ method: "GET", target: value, headers: [], body_base64: "" });
  }
  return { status_code: Number(data.status_code), headers, body_base64: base64(data.body_base64, 8 * 1024 * 1024) };
}

export function attachPreviewBridge(frame: HTMLIFrameElement, previewId: string, proxy: Proxy) {
  let disposed = false;
  let loaded = false;
  let epoch = 0;
  let port: MessagePort | null = null;
  let lastId = 0;
  const pending = new Map<number, AbortController>();
  const reset = () => {
    epoch++;
    port?.close(); port = null;
    for (const controller of pending.values()) controller.abort();
    pending.clear(); lastId = 0;
  };
  const receive = (event: MessageEvent) => {
    if (disposed || !loaded || port || event.source !== frame.contentWindow || event.origin !== "null") return;
    const hello = event.data;
    if (!hello || hello.kind !== "agent-preview-hello" || hello.preview_id !== previewId) return;
    const channel = new MessageChannel();
    port = channel.port1;
    const owned = port;
    const ownedEpoch = epoch;
    const send = (value: unknown) => { if (!disposed && epoch === ownedEpoch) owned.postMessage(value); };
    owned.onmessage = (message) => {
      if (disposed || epoch !== ownedEpoch) return;
      const data = message.data;
      if (!data || !Number.isSafeInteger(data.id) || data.id < 1 || data.id > 0x7fffffff) return;
      if (data.kind === "cancel") { pending.get(data.id)?.abort(); return; }
      if (data.kind !== "request" || data.id <= lastId) return;
      lastId = data.id;
      let request: PreviewAppRequest;
      try { request = validatePreviewRequest(data.request); } catch { send({ kind: "error", id: data.id, error: "Invalid application request" }); return; }
      if (pending.size >= 32) { send({ kind: "error", id: data.id, error: "Application request capacity exhausted" }); return; }
      const controller = new AbortController(); pending.set(data.id, controller);
      const timer = window.setTimeout(() => {
        controller.abort(); if (pending.get(data.id) === controller) pending.delete(data.id);
        send({ kind: "error", id: data.id, error: "Application request timed out" });
      }, 20_000);
      controller.signal.addEventListener("abort", () => window.clearTimeout(timer), { once: true });
      void proxy(previewId, request, controller.signal).then((result) => {
        if (!controller.signal.aborted) send({ kind: "response", id: data.id, result: validatePreviewResponse(result) });
      }).catch(() => {
        if (!controller.signal.aborted) send({ kind: "error", id: data.id, error: "Application request unavailable" });
      }).finally(() => { window.clearTimeout(timer); if (pending.get(data.id) === controller) pending.delete(data.id); });
    };
    owned.start();
    frame.contentWindow?.postMessage({ kind: "agent-preview-port", preview_id: previewId }, "*", [channel.port2]);
  };
  window.addEventListener("message", receive);
  return {
    frameLoaded() { if (!disposed) { reset(); loaded = true; frame.contentWindow?.postMessage({ kind: "agent-preview-init", preview_id: previewId }, "*"); } },
    dispose() { disposed = true; reset(); window.removeEventListener("message", receive); },
  };
}
