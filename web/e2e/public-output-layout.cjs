'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { test, before, after } = require('node:test');
const { build } = require('esbuild');
const { chromium, expect } = require('@playwright/test');

let browser, server, baseURL;
const evidence = process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR;
before(async () => {
  const bundle = await build({ entryPoints: [path.join(__dirname, 'fixtures/public-output-layout.tsx')],
    bundle: true, write: false, outdir: 'layout-bundle', jsx: 'automatic', format: 'iife',
    define: { 'process.env.NODE_ENV': '"production"' } });
  const js = bundle.outputFiles.find(file => file.path.endsWith('.js')).text;
  const css = bundle.outputFiles.find(file => file.path.endsWith('.css')).text;
  server = http.createServer((req, res) => {
    const asset = req.url === '/fixture.js' ? ['text/javascript', js]
      : req.url === '/fixture.css' ? ['text/css', css]
        : ['text/html', '<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Public output layout</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/fixture.css"><body><div id="root"></div><script src="/fixture.js"></script></body></html>'];
    res.writeHead(200, { 'Content-Type': asset[0] }); res.end(asset[1]);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  baseURL = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, chromiumSandbox: true,
    ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) });
});
after(async () => {
  if (browser) await browser.close();
  if (server) await new Promise(resolve => server.close(resolve));
});

async function screenshot(page, name) {
  if (!evidence) return;
  fs.mkdirSync(evidence, { recursive: true });
  await page.screenshot({ path: path.join(evidence, `${name}.png`), fullPage: true });
}

async function open(viewport, route) {
  const context = await browser.newContext({ viewport });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseURL });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', msg => { if (['error', 'warning'].includes(msg.type())) errors.push(msg.text()); });
  await page.route('**/*', route => new URL(route.request().url()).origin === baseURL
    ? route.continue() : route.abort());
  await page.goto(baseURL + route);
  assert.equal(await page.title(), 'Public output layout');
  return { context, page, errors };
}

for (const [device, viewport] of [
  ['desktop', { width: 1280, height: 900 }], ['mobile', { width: 390, height: 844 }],
]) {
  test(`${device}: delivery links and preview prioritize the complete filename`, async () => {
    const { context, page, errors } = await open(viewport, '/conversation?files=1');
    try {
      const filename = '项目交付-20261022-完整源代码与验收记录-frontend-backend-documentation.zip';
      const files = page.getByRole('region', { name: '本轮产物摘要', exact: true });
      const link = files.getByRole('button', { name: `预览文件 ${filename}`, exact: true });
      await expect(link).toBeVisible();
      const label = link.locator('strong');
      await expect(label).toHaveText(filename);
      await link.click();
      const preview = page.getByRole('dialog', { name: '文件内容预览' });
      await expect(preview.getByRole('heading', { name: filename, exact: true })).toBeVisible();
      const card = preview.locator('.artifact-file-main strong');
      await expect(card).toHaveText(filename);
      assert(await card.evaluate(el => getComputedStyle(el).whiteSpace !== 'nowrap'
        && el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1), 'filename must not be clipped');
      assert(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth <= 1));
      await screenshot(page, `${device}-delivery-filename`);
      await preview.getByRole('button', { name: '关闭', exact: true }).click();
      await expect(preview).toHaveCount(0);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });

  test(`${device}: actual long reply preserves rich text before and after expansion`, async () => {
    const { context, page, errors } = await open(viewport, '/conversation?long=1');
    try {
      const message = page.locator('.chat-message.assistant');
      const paragraph = message.locator('.message-paragraph');
      await expect(paragraph.locator('strong')).toHaveText('关键结论');
      await expect(paragraph.locator('p code')).toHaveText('settings.json');
      const before = await paragraph.locator('p').evaluate(el => ({
        height: el.clientHeight, scroll: el.scrollHeight, text: el.textContent,
        weight: getComputedStyle(el.querySelector('strong')).fontWeight,
      }));
      assert(before.height < before.scroll && !before.text.includes('**') && before.text.endsWith('末尾完整内容。'));
      await paragraph.scrollIntoViewIfNeeded();
      await screenshot(page, `${device}-runs-long-collapsed`);
      await paragraph.getByRole('button', { name: '展开全文', exact: true }).click();
      const after = await paragraph.locator('p').evaluate(el => ({ height: el.clientHeight,
        text: el.textContent, weight: getComputedStyle(el.querySelector('strong')).fontWeight }));
      assert(after.height > before.height && after.text === before.text && after.weight === before.weight);
      await screenshot(page, `${device}-runs-long-expanded`);
      await paragraph.getByRole('button', { name: '复制全文', exact: true }).click();
      await expect(paragraph.getByRole('button', { name: '已复制', exact: true })).toBeVisible();
      const copied = await page.evaluate(() => navigator.clipboard.readText());
      assert.equal(copied, '**关键结论**与 `settings.json`：'
        + '长回复正文需要保留一致的文字层级。'.repeat(80) + '末尾完整内容。');
      await paragraph.getByRole('button', { name: '收起', exact: true }).click();
      await expect(paragraph.locator('p strong')).toBeVisible();
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });

  test(`${device}: expanded assistant code remains bounded and reaches the last line`, async () => {
    const { context, page, errors } = await open(viewport, '/');
    try {
      const block = page.locator('.chat-message.assistant .code-block-editor');
      await block.getByRole('button', { name: '展开', exact: true }).click();
      const pre = block.locator('pre');
      await screenshot(page, `${device}-code-expanded`);
      assert(await pre.evaluate(el => el.clientHeight <= innerHeight * 0.8 && el.scrollHeight > el.clientHeight), 'expanded code must remain a bounded reader');
      await pre.press('Control+End');
      await expect.poll(() => pre.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(2);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });

  test(`${device}: real assistant message keeps hierarchy, list markers and independent readers`, async () => {
    const { context, page, errors } = await open(viewport, '/');
    try {
      const message = page.locator('.chat-message.assistant');
      await expect(message.getByText('正文结尾可达。')).toBeVisible();
      await screenshot(page, `${device}-message`);
      // A grid on li removes native markers even when the HTML is a valid list.
      const item = message.locator('ol > li').first();
      assert.equal(await item.evaluate(el => getComputedStyle(el).display), 'list-item');
      await expect(message.getByRole('heading', { name: '调查结果', level: 2 })).toBeVisible();
      const metrics = await message.evaluate(el => {
        const p = el.querySelector('.message-body p');
        const h = el.querySelector('.message-body h2');
        return { line: parseFloat(getComputedStyle(p).lineHeight) / parseFloat(getComputedStyle(p).fontSize),
          heading: parseFloat(getComputedStyle(h).fontSize), body: parseFloat(getComputedStyle(p).fontSize),
          rootOverflow: document.documentElement.scrollWidth - innerWidth };
      });
      assert(metrics.line >= 1.6 && metrics.heading > metrics.body && metrics.rootOverflow <= 1, JSON.stringify(metrics));
      const table = message.getByRole('region', { name: /表格 1滚动区域/ });
      await table.scrollIntoViewIfNeeded();
      await table.focus(); await table.press('End');
      await table.evaluate(el => { el.scrollLeft = el.scrollWidth; });
      assert(await table.evaluate(el => getComputedStyle(el).overflowX === 'auto'
        && (el.scrollWidth <= el.clientWidth + 1 || el.scrollLeft > 0)));
      if (device === 'mobile') assert(await table.evaluate(el => el.scrollLeft > 0));
      assert(await table.locator('th').first().evaluate(el => getComputedStyle(el).backgroundColor !== 'rgba(0, 0, 0, 0)'));
      await table.evaluate(el => { el.scrollLeft = 0; });
      const block = message.locator('.code-block-editor');
      const pre = block.locator('pre');
      assert(await pre.evaluate(el => el.scrollWidth > el.clientWidth));
      await block.getByRole('button', { name: '换行', exact: true }).click();
      assert(await pre.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      await block.getByRole('button', { name: '不换行', exact: true }).click();
      await block.getByRole('button', { name: '展开', exact: true }).click();
      await expect(pre).toBeFocused();
      assert(await pre.evaluate(el => el.clientHeight <= innerHeight * 0.8 && el.scrollHeight > el.clientHeight), 'expanded code must remain a bounded reader');
      await pre.press('Control+End');
      await expect.poll(() => pre.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(2);
      await block.getByRole('button', { name: '复制 javascript 代码', exact: true }).click();
      await expect(block.getByRole('button', { name: '已复制 javascript 代码', exact: true })).toBeVisible();
      await block.getByRole('button', { name: '收起', exact: true }).click();
      await expect(message.getByText('正文结尾可达。')).toBeVisible();
      await screenshot(page, `${device}-message-readers`);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });

  test(`${device}: actual RunsPage assistant body renders ordinary results without a file preview`, async () => {
    const { context, page, errors } = await open(viewport, '/conversation');
    try {
      const message = page.locator('.chat-message.assistant').filter({ hasText: '正文结尾可达。' });
      const heading = message.getByRole('heading', { name: '调查结果', level: 2 });
      await expect(heading).toBeAttached();
      await heading.scrollIntoViewIfNeeded();
      await screenshot(page, `${device}-runs-message-top`);
      assert.equal(await message.locator('ol > li').first().evaluate(el => getComputedStyle(el).display), 'list-item');
      assert.equal(await message.locator('li .message-paragraph-tools').count(), 0);
      const table = message.getByRole('table');
      await table.locator('th').first().scrollIntoViewIfNeeded();
      await screenshot(page, `${device}-runs-message-table`);
      const region = message.getByRole('region', { name: /表格 1滚动区域/ });
      if (await region.evaluate(el => el.scrollWidth > el.clientWidth + 1)) {
        await region.focus();
        await region.press('ArrowRight');
        await expect.poll(() => region.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
        await screenshot(page, `${device}-runs-message-table-scrolled`);
      }
      const block = message.locator('.code-block-editor');
      const pre = block.locator('pre');
      const source = await pre.locator('code').textContent();
      assert(await pre.evaluate(el => el.scrollWidth > el.clientWidth));
      await block.getByRole('button', { name: '换行', exact: true }).click();
      await expect(block.getByRole('button', { name: '不换行', exact: true })).toHaveAttribute('aria-pressed', 'true');
      assert(await pre.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      await screenshot(page, `${device}-runs-message-code-wrapped`);
      await block.getByRole('button', { name: '不换行', exact: true }).click();
      assert(await pre.evaluate(el => el.scrollWidth > el.clientWidth));
      await block.getByRole('button', { name: '展开', exact: true }).click();
      await expect(pre).toBeFocused();
      await pre.press('Control+End');
      await expect.poll(() => pre.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(2);
      await screenshot(page, `${device}-runs-message-code`);
      await block.getByRole('button', { name: '复制 javascript 代码', exact: true }).click();
      await expect(block.getByRole('button', { name: '已复制 javascript 代码', exact: true })).toBeVisible();
      // Windows system clipboard normalizes line endings; source bytes are covered by component tests.
      const copied = await page.evaluate(() => navigator.clipboard.readText());
      assert.equal(copied.replace(/\r\n/g, '\n'), source);
      await block.getByRole('button', { name: '收起', exact: true }).click();
      await expect(block.getByRole('button', { name: '展开', exact: true })).toHaveAttribute('aria-expanded', 'false');
      await message.getByText('正文结尾可达。').scrollIntoViewIfNeeded();
      await expect(message.getByText('正文结尾可达。')).toBeVisible();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await expect(page.getByRole('alert')).toHaveCount(0);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });

  for (const count of [16, 40]) test(`${device}: actual RunDetailPage ${count} fields, Markdown and long JSON`, async () => {
    const { context, page, errors } = await open(viewport, `/detail?count=${count}`);
    try {
      await page.getByRole('button', { name: /Agent 工作席/ }).click();
      const drawer = page.getByRole('dialog', { name: 'Agent 工作席详情' });
      await drawer.getByRole('button', { name: /LAYOUT_ACTIVITY/ }).click();
      await drawer.getByRole('button', { name: /^活动：/ }).click();
      const dialog = page.getByRole('dialog', { name: '活动详情' });
      await expect(dialog).toBeVisible();
      const dl = dialog.locator('dl');
      const rows = await dl.locator('dd').evaluateAll(items => items.map(el => {
        const r = el.getBoundingClientRect(), child = el.firstElementChild.getBoundingClientRect();
        return { height: r.height, clipped: child.bottom > r.bottom + 1, overflow: getComputedStyle(el).overflowY };
      }));
      assert(rows.length >= count);
      assert(rows.every(row => row.height >= 20 && !row.clipped && row.overflow === 'visible'), JSON.stringify(rows));
      const tail = dialog.getByText('TAIL_REACHABLE', { exact: true });
      await tail.scrollIntoViewIfNeeded(); await expect(tail).toBeVisible();
      assert(await dl.evaluate(el => el.scrollTop > 0));
      await dl.evaluate(el => { el.scrollTop = 0; });
      await screenshot(page, `${device}-detail-${count}`);
      // The real value renderer includes highlighting, toolbar and Markdown ancestors.
      const json = dialog.locator('.code-block-editor').filter({ has: page.locator('code[data-language="json"]') });
      await json.getByRole('button', { name: '展开', exact: true }).click();
      const pre = json.locator('pre');
      await expect(pre).toBeFocused(); await pre.press('Control+End');
      await expect.poll(() => pre.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(2);
      assert(await pre.evaluate(el => el.clientHeight > innerHeight / 2 && el.scrollTop > 0));
      await screenshot(page, `${device}-detail-${count}-expanded`);
      const expandedWidth = await json.evaluate(el => ({ width: el.getBoundingClientRect().width,
        available: el.closest('dl').clientWidth }));
      assert(expandedWidth.width >= expandedWidth.available - 2,
        `expanded JSON must fill the detail sheet: ${JSON.stringify(expandedWidth)}`);
      await json.getByRole('button', { name: '收起', exact: true }).click();
      await expect(tail).toBeAttached();
      const code = dialog.locator('.message-body .code-block-editor');
      await code.getByRole('button', { name: '展开', exact: true }).click();
      const codePre = code.locator('pre');
      await expect(codePre).toBeFocused(); await codePre.press('Control+End');
      const reader = await code.evaluate(el => {
        const rect = el.getBoundingClientRect(), list = el.closest('dl').getBoundingClientRect();
        const button = el.querySelector('button'), br = button.getBoundingClientRect();
        const hit = document.elementFromPoint(br.x + br.width / 2, br.y + br.height / 2);
        return { width: rect.width, available: list.width, fits: rect.right <= list.right + 1,
          toolbar: hit === button || button.contains(hit) };
      });
      assert(reader.width >= reader.available - 2 && reader.fits && reader.toolbar, JSON.stringify(reader));
      assert(await codePre.evaluate(el => el.clientHeight > innerHeight / 2 && el.scrollTop > 0));
      await expect.poll(() => codePre.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(2);
      await screenshot(page, `${device}-detail-${count}-markdown-expanded`);
      await code.getByRole('button', { name: '收起', exact: true }).click();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await dialog.getByRole('button', { name: '关闭', exact: true }).click();
      await expect(dialog).toHaveCount(0);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}
