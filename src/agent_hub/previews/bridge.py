"""Credential-free fetch transport for an opaque generated-site iframe."""

from __future__ import annotations

import json
from uuid import UUID

_SHIM = r"""
(() => {
  const previewId = __PREVIEW_ID__;
  const prefix = '/api/v1/web-previews/' + previewId + '/content/';
  const origin = window.location.origin;
  const allowedHeaders = new Set(['accept', 'accept-language', 'content-type', 'if-match', 'if-none-match', 'range']);
  let port = null;
  let counter = 0;
  let closed = false;
  let helloTimer = null;
  const pending = new Map();
  const waiting = new Set();
  const unavailable = () => new TypeError('Application preview transport unavailable');
  function rejectAll() {
    port?.close(); port = null;
    for (const item of pending.values()) item.reject(unavailable());
    pending.clear();
  }
  function hello() {
    if (!closed && !port) window.parent.postMessage({kind: 'agent-preview-hello', preview_id: previewId}, '*');
  }
  function beginHello() {
    if (helloTimer !== null) clearInterval(helloTimer);
    hello(); helloTimer = setInterval(hello, 500);
  }
  window.addEventListener('message', (event) => {
    if (closed || event.source !== window.parent || event.origin !== origin || event.data?.preview_id !== previewId) return;
    if (event.data.kind === 'agent-preview-init') { rejectAll(); beginHello(); return; }
    if (event.data.kind !== 'agent-preview-port' || port || event.ports.length !== 1) return;
    port = event.ports[0];
    clearInterval(helloTimer); helloTimer = null;
    port.onmessage = (message) => {
      const data = message.data;
      const item = pending.get(data?.id);
      if (!item) return;
      if (data.kind === 'error') { item.reject(unavailable()); return; }
      if (data.kind === 'response') item.resolve(data.result);
    };
    port.start();
    for (const ready of waiting) ready();
    waiting.clear();
  });
  function ready(signal) {
    if (closed) return Promise.reject(unavailable());
    if (port) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const finish = (error) => {
        clearTimeout(timer); waiting.delete(success);
        signal?.removeEventListener('abort', abort);
        error ? reject(error) : resolve();
      };
      const success = () => finish();
      const abort = () => finish(new DOMException('Aborted', 'AbortError'));
      const timer = setTimeout(() => finish(unavailable()), 10_000);
      waiting.add(success);
      signal?.addEventListener('abort', abort, {once: true});
      if (signal?.aborted) abort();
    });
  }
  function encode(bytes) {
    let result = '';
    for (let offset = 0; offset < bytes.length; offset += 8192) result += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    return btoa(result);
  }
  function decode(text) {
    const raw = atob(text);
    return Uint8Array.from(raw, (char) => char.charCodeAt(0));
  }
  window.fetch = async (input, init) => {
    const rawUrl = input instanceof Request ? input.url : String(input);
    const url = new URL(rawUrl, document.baseURI);
    if (url.origin !== origin || url.username || url.password || url.hash) throw new TypeError('Only this preview application is accessible');
    let target = url.pathname + url.search;
    if (url.pathname.startsWith(prefix)) target = '/' + url.pathname.slice(prefix.length) + url.search;
    const prepared = new Request(input instanceof Request ? input : url.href, init);
    if (!['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'].includes(prepared.method)) throw new TypeError('Unsupported application method');
    const headers = [];
    for (const [name, value] of prepared.headers) {
      if (!allowedHeaders.has(name)) throw new TypeError('Unsupported application header');
      headers.push([name, value]);
    }
    const bytes = new Uint8Array(await prepared.arrayBuffer());
    if (bytes.length > 1024 * 1024) throw new TypeError('Application request too large');
    await ready(prepared.signal);
    if (prepared.signal.aborted) throw new DOMException('Aborted', 'AbortError');
    if (pending.size >= 32 || counter >= 0x7fffffff) throw unavailable();
    const id = ++counter;
    const result = await new Promise((resolve, reject) => {
      const finish = (error, value) => {
        clearTimeout(timer); pending.delete(id);
        prepared.signal.removeEventListener('abort', abort);
        error ? reject(error) : resolve(value);
      };
      const abort = () => { port?.postMessage({kind:'cancel', id}); finish(new DOMException('Aborted', 'AbortError')); };
      const timer = setTimeout(() => { port?.postMessage({kind:'cancel', id}); finish(unavailable()); }, 25_000);
      pending.set(id, {resolve: (value) => finish(null, value), reject: (error) => finish(error)});
      prepared.signal.addEventListener('abort', abort, {once:true});
      port.postMessage({kind:'request', id, request:{method:prepared.method, target, headers, body_base64:encode(bytes)}});
    });
    const body = ['HEAD'].includes(prepared.method) || [204,205,304].includes(result.status_code) ? null : decode(result.body_base64);
    return new Response(body, {status:result.status_code, headers:result.headers});
  };
  window.addEventListener('pagehide', () => {
    closed = true; clearInterval(helloTimer); rejectAll();
    for (const ready of waiting) ready(); waiting.clear();
  }, {once:true});
  beginHello();
})();
"""


def preview_fetch_shim(preview_id: str) -> str:
    """Only a server-generated UUID can be inserted into trusted script source."""
    if str(UUID(preview_id)) != preview_id:
        raise ValueError("invalid preview identity")
    source = _SHIM.replace("__PREVIEW_ID__", json.dumps(preview_id))
    return f"<script data-agent-preview-fetch-shim>{source}</script>"
