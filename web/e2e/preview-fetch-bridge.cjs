'use strict';

// A real HTTP/browser contract fixture, never a project acceptance receipt.
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { once } = require('node:events');
const { test } = require('node:test');
const { chromium } = require('@playwright/test');
const esbuild = require(require.resolve('esbuild', { paths: [path.dirname(require.resolve('vite'))] }));
const root = path.resolve(__dirname, '../..');
const python = process.env.PREVIEW_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const previewId = '12345678-1234-4234-9234-123456789abc';
const queryTargets = [
  '/tasks?q=100%25',
  '/tasks?q=a%5Cb',
  '/tasks?tag=one&tag=two&empty=&bare&plus=a+b&space=a%20b&slash=a%2Fb',
  '/tasks?q=%2525&literal=%250A',
  '/tasks?q=..%2F..%2F&hash=%23&question=%3F',
];

for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
  test(`opaque fetch actual HTTP CRUD ${viewport.width}x${viewport.height}`, { timeout: 45000 }, async () => {
    const shim = execFileSync(python, ['-c', `from agent_hub.previews.bridge import preview_fetch_shim; print(preview_fetch_shim('${previewId}'))`], { cwd: root, encoding: 'utf8' });
    const bundle = await esbuild.build({ entryPoints: [path.join(root, 'web/src/preview/transport.ts')], bundle: true, write: false, format: 'iife', globalName: 'OwnedPreview', platform: 'browser' });
    let task = null;
    const actualRequests = [];
    const server = http.createServer(async (req, res) => {
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      const bytes = Buffer.concat(chunks);
      if (req.url === '/proxy' && req.method === 'POST') {
        assert.equal(req.headers.authorization, 'Bearer parent-fixture-only');
        const envelope = JSON.parse(bytes.toString()); actualRequests.push(envelope);
        let status = 200; let result;
        if (envelope.target === '/tasks?q=one' && envelope.method === 'GET') result = task ? [task] : [];
        else if (queryTargets.includes(envelope.target) && envelope.method === 'GET') result = { target: envelope.target };
        else if (envelope.target === '/tasks' && envelope.method === 'POST') { task = { id: 1, ...JSON.parse(Buffer.from(envelope.body_base64, 'base64')) }; status = 201; result = task; }
        else if (envelope.target === '/tasks/1' && envelope.method === 'PATCH') { task = { ...task, ...JSON.parse(Buffer.from(envelope.body_base64, 'base64')) }; result = task; }
        else if (envelope.target === '/tasks/1' && envelope.method === 'DELETE') { task = null; status = 204; result = null; }
        else if (envelope.target === '/fail') { status = 409; result = { error: 'actual conflict' }; }
        else { status = 404; result = { error: 'not found' }; }
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ status_code: status, headers: [['content-type', 'application/json']], body_base64: result === null ? '' : Buffer.from(JSON.stringify(result)).toString('base64') })); return;
      }
      if (req.url === '/child') {
        res.setHeader('Content-Type', 'text/html');
        res.setHeader('Content-Security-Policy', "sandbox allow-scripts allow-forms; default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'");
        res.end(`<!doctype html><head>${shim}</head><body><button id="create">Create</button><button id="modify">Modify</button><button id="delete">Delete</button><output id="result"></output><script>
          document.querySelector('#create').onclick=async()=>{const r=await fetch(location.origin+'/tasks',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({title:'Actual fixture task'})});document.querySelector('#result').textContent=(await r.json()).title;};
          document.querySelector('#modify').onclick=async()=>{const r=await fetch('/tasks/1',{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({done:true})});document.querySelector('#result').textContent=String((await r.json()).done);};
          document.querySelector('#delete').onclick=async()=>{const r=await fetch('/tasks/1',{method:'DELETE'});document.querySelector('#result').textContent=String(r.status);};
          fetch('/tasks?q=one').then(r=>r.json()).then(()=>{document.body.dataset.initial='ready';}).catch(()=>{document.body.dataset.initial='failed';});
        </script><script src="/slow.js"></script></body>`); return;
      }
      if (req.url === '/slow.js') {
        res.setHeader('Content-Type', 'application/javascript');
        setTimeout(() => res.end('/* deterministic document-load race fixture */'), 300); return;
      }
      if (req.url === '/parent') {
        res.setHeader('Content-Type', 'text/html');
        res.end(`<!doctype html><body><iframe id="frame" src="/child" sandbox="allow-scripts allow-forms"></iframe><script>${bundle.outputFiles[0].text}</script><script>
          window.bridge=OwnedPreview.attachPreviewBridge(document.querySelector('#frame'),'${previewId}',async(id,request,signal)=>{
            const response=await fetch('/proxy',{method:'POST',signal,headers:{'content-type':'application/json','authorization':'Bearer parent-fixture-only'},body:JSON.stringify(request)});return response.json();
          });document.querySelector('#frame').onload=()=>window.bridge.frameLoaded();
        </script></body>`); return;
      }
      res.statusCode = 404; res.end();
    });
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    let browser;
    try {
      browser = await chromium.launch({ headless: true });
      const page = await browser.newPage({ viewport });
      await page.goto(`http://127.0.0.1:${server.address().port}/parent`);
      const frame = page.frameLocator('#frame');
      await frame.locator('body[data-initial="ready"]').waitFor({ timeout: 3000 });
      await frame.locator('#create').click(); await frame.locator('#result').filter({ hasText: 'Actual fixture task' }).waitFor();
      await frame.locator('#modify').click(); await frame.locator('#result').filter({ hasText: 'true' }).waitFor();
      await frame.locator('#delete').click(); await frame.locator('#result').filter({ hasText: '204' }).waitFor();
      const child = page.frames()[1];
      const data = await child.evaluate(async () => {
        const get = await fetch('/tasks?q=one'); const conflict = await fetch('/fail');
        let externalRejected = false; try { await fetch('https://external.invalid/'); } catch { externalRejected = true; }
        let cookiesBlocked = false; try { document.cookie; } catch { cookiesBlocked = true; }
        return { items: await get.json(), conflict: conflict.status, cookiesBlocked, externalRejected, token: typeof window.agent_hub_access_token };
      });
      assert.deepEqual(data, { items: [], conflict: 409, cookiesBlocked: true, externalRejected: true, token: 'undefined' });
      assert.deepEqual(actualRequests.map(r => r.method), ['GET', 'POST', 'PATCH', 'DELETE', 'GET', 'GET']);
      const queryStart = actualRequests.length;
      for (const inputKind of ['root', 'content-prefix', 'request']) {
        const results = await child.evaluate(async ({ targets, previewId, inputKind }) => {
          const results = [];
          for (const target of targets) {
            const input = inputKind === 'content-prefix' ? '/api/v1/web-previews/' + previewId + '/content' + target
              : inputKind === 'request' ? new Request(location.origin + target) : target;
            try {
              const response = await fetch(input);
              results.push({ status: response.status, body: await response.json() });
            } catch (error) {
              results.push({ error: error.message });
            }
          }
          return results;
        }, { targets: queryTargets, previewId, inputKind });
        assert.deepEqual(results, queryTargets.map(target => ({ status: 200, body: { target } })), inputKind);
      }
      assert.deepEqual(actualRequests.slice(queryStart).map(({ method, target, body_base64 }) => ({ method, target, body_base64 })),
        Array.from({ length: 3 }, () => queryTargets.map(target => ({ method: 'GET', target, body_base64: '' }))).flat());
      assert(actualRequests.every(r => !r.headers.some(([name]) => /authorization|cookie|origin|host/i.test(name))));
      const requestCount = actualRequests.length;
      await page.evaluate(() => window.bridge.dispose());
      const revoked = await child.evaluate(async () => { const c=new AbortController();setTimeout(()=>c.abort(),100);try{await fetch('/tasks',{signal:c.signal});return false;}catch{return true;} });
      assert.equal(revoked, true); assert.equal(actualRequests.length, requestCount);
    } finally {
      if (browser) await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    }
  });
}
