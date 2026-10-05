'use strict';

// Real component/CSS layout over local HTTP, without a backend or credentials.
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { once } = require('node:events');
const { mkdirSync, readFileSync } = require('node:fs');
const { after, before, test } = require('node:test');
const { chromium, expect } = require('@playwright/test');
const esbuild = require(require.resolve('esbuild', { paths: [path.dirname(require.resolve('vite'))] }));

const webRoot = path.resolve(__dirname, '..');
const previewId = '10000000-0000-4000-8000-000000000010';
const contentPath = `/api/v1/web-previews/${previewId}/content/`;
const preview = {
  id: previewId, status: 'ready', preview_url: contentPath, lease_expires_at: null, application_transport: false,
  identity: {
    preview_id: previewId, kind: 'static', tenant_id: '33333333-3333-4333-8333-333333333333',
    user_id: '11111111-1111-4111-8111-111111111111', project_id: 'layout-project',
    conversation_id: 'layout-conversation', workspace_session_id: 'layout-session', runtime_handle: null,
    source: { scheme: 'preview-static-tree-v1', sha256: 'b'.repeat(64) },
    display_root: '.', display_entrypoint: 'index.html',
  },
  cleanup_url: `/api/v1/web-previews/${previewId}/cleanup`, cleanup_receipt: null,
};
const observedAt = '2026-10-05T00:00:00Z';
// Synthetic fixture facts are unknown, never native cleanup proof.
const stoppedPreview = {
  ...preview, status: 'stopped', preview_url: null,
  cleanup_receipt: {
    schema_version: 1, identity: preview.identity, observation_id: '10000000-0000-4000-8000-000000000011',
    requested_at: observedAt, observed_at: observedAt, reason: 'explicit', status: 'unknown',
    coverage: 'static-loopback-v1', unobserved: [],
    observations: ['serving_thread', 'listener', 'accepted_threads', 'accepted_sockets',
      'proxy_operations', 'proxy_sockets', 'capability', 'snapshot'].map((resource) => ({
      resource, observer: 'manager', observed_at: observedAt, result: 'unknown', reason_code: 'not_attempted',
      load_state: null, active_state: null, main_pid: null, identity_match: null,
    })),
  },
};
const previewPath = `/website-preview/${previewId}?conversation=layout-conversation`;
let server;
let browser;
let origin;

before(async () => {
  const bundle = await esbuild.build({
    stdin: {
      contents: `import React from 'react';
        import { createRoot } from 'react-dom/client';
        import { BrowserRouter, Route, Routes } from 'react-router-dom';
        import { WebsitePreviewPage } from './src/pages/WebsitePreviewPage';
        createRoot(document.getElementById('root')).render(
          <BrowserRouter><Routes><Route path="/website-preview/:previewId" element={<WebsitePreviewPage />} /></Routes></BrowserRouter>
        );`,
      resolveDir: webRoot,
      loader: 'tsx',
    },
    bundle: true,
    write: false,
    format: 'iife',
    platform: 'browser',
    jsx: 'automatic',
    define: { 'process.env.NODE_ENV': '"production"' },
  });
  const css = readFileSync(path.join(webRoot, 'src/styles.css'), 'utf8');
  server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://127.0.0.1').pathname;
    if (pathname === '/api/v1/web-previews/conversations/layout-conversation' && req.method === 'GET') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(preview));
    } else if (pathname === `/api/v1/web-previews/${previewId}` && req.method === 'DELETE') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(stoppedPreview));
    } else if (pathname === '/styles.css') {
      res.setHeader('Content-Type', 'text/css');
      res.end(css);
    } else if (pathname === '/fixture.js') {
      res.setHeader('Content-Type', 'application/javascript');
      res.end(bundle.outputFiles[0].text);
    } else if (pathname === contentPath) {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end('<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body><h1>Local website preview</h1></body></html>');
    } else if (pathname === `/website-preview/${previewId}`) {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Website preview layout fixture</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/styles.css"></head><body><div id="root"></div><script src="/fixture.js"></script></body></html>');
    } else {
      res.writeHead(404).end();
    }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true });
});

after(async () => {
  try {
    if (browser) await browser.close();
  } finally {
    if (server) {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
      assert.equal(server.listening, false);
    }
  }
});

for (const viewport of [{ width: 390, height: 844 }, { width: 320, height: 844 }, { width: 1440, height: 960 }]) {
  test(`preview header stays single-line with a compact stop button at ${viewport.width}x${viewport.height}`, { timeout: 30000 }, async (t) => {
    const context = await browser.newContext({ viewport });
    const errors = [];
    await context.route('**/*', (route) => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      errors.push(`Unexpected external request: ${route.request().url()}`);
      return route.abort();
    });
    try {
      const page = await context.newPage();
      page.on('pageerror', (error) => errors.push(error.message));
      page.on('console', (message) => {
        if (['error', 'warning'].includes(message.type())) errors.push(message.text());
      });
      await page.goto(`${origin}${previewPath}`);
      assert.equal(page.url(), `${origin}${previewPath}`);
      assert.equal(await page.title(), 'Website preview layout fixture');
      const stop = page.getByRole('button', { name: '停止预览', exact: true });
      await expect(stop).toBeVisible();
      await expect(page.frameLocator('iframe').getByRole('heading', { name: 'Local website preview' })).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      const layout = await page.evaluate(() => {
        const header = document.querySelector('.website-preview-page > header');
        const title = header.querySelector('strong');
        const button = header.querySelector('button');
        const textLines = (element) => {
          const range = document.createRange();
          range.selectNodeContents(element);
          return range.getClientRects().length;
        };
        return {
          titleLines: textLines(title),
          buttonLines: textLines(button),
          title: title.getBoundingClientRect().toJSON(),
          button: button.getBoundingClientRect().toJSON(),
          header: header.getBoundingClientRect().toJSON(),
          frame: document.querySelector('iframe').getBoundingClientRect().toJSON(),
          scrollWidth: document.documentElement.scrollWidth,
        };
      });
      t.diagnostic(JSON.stringify({ viewport, titleLines: layout.titleLines, buttonWidth: layout.button.width, headerHeight: layout.header.height }));
      if (process.env.PREVIEW_LAYOUT_SCREENSHOT_DIR) {
        mkdirSync(process.env.PREVIEW_LAYOUT_SCREENSHOT_DIR, { recursive: true });
        await page.screenshot({ path: path.join(process.env.PREVIEW_LAYOUT_SCREENSHOT_DIR, `preview-${viewport.width}.png`) });
      }
      assert.equal(layout.titleLines, 1, 'preview title must not wrap');
      assert.equal(layout.buttonLines, 1, 'stop label must not wrap');
      assert(layout.button.width <= 128, `stop button must stay compact, got ${layout.button.width}px`);
      assert(layout.title.right + 12 <= layout.button.left, 'title and button must keep their gap');
      assert(layout.title.left >= 0 && layout.button.right <= viewport.width, 'header content must fit the viewport');
      assert(Math.abs((layout.title.top + layout.title.bottom) - (layout.button.top + layout.button.bottom)) <= 2, 'title and button must remain vertically aligned');
      assert(layout.scrollWidth <= viewport.width, 'page must not overflow horizontally');
      assert.equal(layout.frame.width, viewport.width);
      assert(Math.abs(layout.frame.top - layout.header.bottom) <= 1, 'preview must start below the header');
      assert(Math.abs(layout.frame.bottom - viewport.height) <= 1, 'preview must fill the remaining viewport');

      await stop.click();
      await expect(page.getByRole('alert')).toHaveText('网站预览已停止');
      await expect(stop).toHaveCount(0);
      await expect(page.locator('iframe')).toHaveCount(0);
      assert.deepEqual(errors, [], 'preview must render and stop without browser errors or external requests');
    } finally {
      await context.close();
    }
  });
}
