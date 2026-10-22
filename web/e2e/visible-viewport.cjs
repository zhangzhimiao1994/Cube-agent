'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { test, before, after } = require('node:test');
const { build } = require('esbuild');
const { chromium, expect } = require('@playwright/test');
let browser, server, base;
before(async () => {
  const bundle = await build({ entryPoints: [path.join(__dirname, 'fixtures/public-output-layout.tsx')],
    bundle: true, write: false, outdir: 'viewport-bundle', jsx: 'automatic', format: 'iife',
    define: { 'process.env.NODE_ENV': '"production"' } });
  server = http.createServer((req, res) => {
    const css = req.url === '/fixture.css', js = req.url === '/fixture.js';
    res.writeHead(200, { 'Content-Type': css ? 'text/css' : js ? 'text/javascript' : 'text/html' });
    res.end(css || js ? bundle.outputFiles.find(file => file.path.endsWith(css ? '.css' : '.js')).text
      : '<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Visible viewport regression</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/fixture.css"><body><div id="root"></div><script src="/fixture.js"></script></body></html>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, channel: 'msedge', chromiumSandbox: true });
});
after(async () => { if (browser) await browser.close(); if (server) await new Promise(resolve => server.close(resolve)); });

for (const [name, viewport] of [['mobile', { width: 390, height: 844 }], ['desktop', { width: 1280, height: 900 }]]) {
  test(name + ': expanded source preview keeps its final line reachable', async () => {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    try {
      await page.goto(base + '/conversation?files=1&codefile=1');
      await page.getByRole('button', { name: '预览文件 scroll-preview.js', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: '文件内容预览' });
      const reader = dialog.locator('.agent-workbench-file-preview');
      await dialog.getByRole('button', { name: '展开', exact: true }).click();
      const pre = dialog.locator('.code-block-editor pre');
      await pre.scrollIntoViewIfNeeded();
      await pre.evaluate(el => { el.scrollTop = el.scrollHeight; });
      assert(await pre.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop <= 2));
      await expect(pre).toContainText('line-64');
      assert(await reader.evaluate(el => el.scrollHeight <= el.clientHeight + 1));
      const box = await pre.boundingBox();
      assert(box.y >= 0 && box.y + box.height <= viewport.height);
    } finally { await context.close(); }
  });
  test(name + ': scrolling over a short JSON reader continues to later fields', async () => {
    const context = await browser.newContext({ viewport, isMobile: name === 'mobile', hasTouch: name === 'mobile' });
    const page = await context.newPage();
    try {
      await page.goto(base + '/detail?count=40&short=1');
      await page.getByRole('button', { name: /Agent 工作席/ }).click();
      const drawer = page.getByRole('dialog', { name: 'Agent 工作席详情' });
      await drawer.getByRole('button', { name: /LAYOUT_ACTIVITY/ }).click();
      await drawer.getByRole('button', { name: /^活动：/ }).click();
      const dialog = page.getByRole('dialog', { name: '活动详情', exact: true });
      const list = dialog.locator('dl');
      const pre = dialog.locator('.code-block-editor').filter({ has: page.locator('code[data-language="json"]') }).locator('pre');
      await pre.scrollIntoViewIfNeeded();
      assert(await pre.evaluate(el => el.scrollHeight <= el.clientHeight + 1), 'the short JSON has no vertical scroll of its own');
      const before = await list.evaluate(el => el.scrollTop);
      if (name === 'mobile') {
        const box = await pre.boundingBox();
        const session = await context.newCDPSession(page);
        const x = Math.round(box.x + box.width / 2), y = Math.round(box.y + box.height / 2);
        await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
        for (let step = 1; step <= 9; step++) {
          await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y - step * 20 }] });
          await page.waitForTimeout(20);
        }
        await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
        await session.detach();
      } else { await pre.hover(); await page.mouse.wheel(0, 600); }
      await expect.poll(async () => (await list.evaluate(el => el.scrollTop)) - before, { timeout: 3000 }).toBeGreaterThan(10);
    } finally { await context.close(); }
  });
  test(name + ': dialogs keep their bottom inside the visible viewport and the final row is reachable', async () => {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(() => {
      const view = Object.assign(new EventTarget(), { height: innerHeight, width: innerWidth, offsetTop: 0, offsetLeft: 0 });
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: view });
    });
    try {
      await page.goto(base + '/detail?count=40');
      await page.getByRole('button', { name: /Agent 工作席/ }).click();
      const drawer = page.getByRole('dialog', { name: 'Agent 工作席详情' });
      await drawer.getByRole('button', { name: /LAYOUT_ACTIVITY/ }).click();
      await drawer.getByRole('button', { name: /^活动：/ }).click();
      const dialog = page.getByRole('dialog', { name: '活动详情', exact: true });
      const shrink = async () => page.evaluate(() => {
        Object.assign(visualViewport, { height: 520, offsetTop: 36 });
        visualViewport.dispatchEvent(new Event('resize'));
        visualViewport.dispatchEvent(new Event('scroll'));
      });
      await shrink();
      const box = await dialog.boundingBox();
      assert(box.y >= 35 && box.y + box.height <= 557, 'dialog bottom is behind browser chrome: ' + JSON.stringify(box));
      const scroll = dialog.getByLabel('活动详情内容', { exact: true });
      await scroll.focus(); await scroll.press('Control+End');
      await expect.poll(() => scroll.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThanOrEqual(2);
      const tail = dialog.getByText('TAIL_REACHABLE', { exact: true });
      const tailBox = await tail.boundingBox();
      assert(tailBox.y >= box.y && tailBox.y + tailBox.height <= box.y + box.height - 2, 'last field must fit completely');
      await expect(tail).toBeVisible();
      if (process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR) {
        fs.mkdirSync(process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR, { recursive: true });
        await page.screenshot({ path: path.join(process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR, name + '-activity-bottom.png') });
      }
      await scroll.evaluate(el => { el.scrollTop = 0; });
      const json = dialog.locator('.code-block-editor').filter({ has: page.locator('code[data-language="json"]') });
      await json.getByRole('button', { name: '展开', exact: true }).click();
      const expandedBox = await dialog.boundingBox();
      assert(expandedBox.y >= 35 && expandedBox.y + expandedBox.height <= 557,
        'expanded reader must fit: ' + JSON.stringify(expandedBox));
      await json.getByRole('button', { name: '收起', exact: true }).click();
      await dialog.getByRole('button', { name: '关闭', exact: true }).click();
      await expect(dialog).toHaveCount(0);
      const outer = await drawer.boundingBox();
      assert(outer.y >= 35 && outer.y + outer.height <= 557, 'parent workbench must also fit');
      await drawer.getByRole('button', { name: '关闭', exact: true }).click();
      await page.goto(base + '/conversation?files=1');
      await shrink();
      await page.getByRole('button', { name: /^预览文件 项目交付/ }).click();
      const preview = page.getByRole('dialog', { name: '文件内容预览' });
      const previewBox = await preview.boundingBox();
      assert(previewBox.y >= 35 && previewBox.y + previewBox.height <= 557, 'file preview must fit');
      await preview.getByRole('button', { name: '关闭', exact: true }).click();
      await page.getByRole('button', { name: '打开本次运行配置', exact: true }).click();
      const settings = page.getByRole('dialog', { name: '本次运行设置', exact: true });
      const settingsBox = await settings.boundingBox();
      assert(settingsBox.y >= 35 && settingsBox.y + settingsBox.height <= 557, 'run settings must fit');
      assert(settingsBox.width <= 760, 'desktop settings must retain their width limit: ' + JSON.stringify(settingsBox));
      await settings.getByRole('button', { name: '关闭运行设置', exact: true }).click();
      const modalWidth = await page.evaluate(() => {
        const backdrop = document.createElement('div'); backdrop.className = 'modal-backdrop';
        const card = document.createElement('section'); card.className = 'modal-card';
        const content = document.createElement('div'); content.style.width = '1600px';
        card.append(content); backdrop.append(card); document.body.append(backdrop);
        const width = card.getBoundingClientRect().width; backdrop.remove(); return width;
      });
      assert(modalWidth <= 560 && modalWidth <= viewport.width, 'generic configuration cards must retain their width limit');
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}
