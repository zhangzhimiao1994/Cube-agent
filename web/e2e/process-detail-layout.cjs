'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, before, after } = require('node:test');
const { chromium, expect } = require('@playwright/test');

let browser;
const css = fs.readFileSync(path.join(__dirname, '../src/styles.css'), 'utf8');
before(async () => {
  browser = await chromium.launch({ headless: true, chromiumSandbox: true,
    ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) });
});
after(async () => { if (browser) await browser.close(); });

for (const [device, viewport] of [
  ['desktop', { width: 1280, height: 900 }], ['mobile', { width: 390, height: 744 }],
]) for (const count of [16, 40]) {
  test(`${device}: ${count} activity fields remain readable and scroll as one list`, async () => {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    try {
      const code = Array.from({ length: 80 }, (_, i) => `print(${i})`).join('\n');
      const rows = Array.from({ length: count }, (_, i) => `<dt>详情: field_${i}</dt><dd>${i % 3 === 0
        ? `<div class="bounded-text-block is-collapsed"><div class="bounded-text-block-header"><span>json</span><div><button type="button" aria-expanded="false">展开</button><button>复制</button></div></div><pre tabindex="0"><code>${code}</code></pre></div>`
        : `<span>${i === count - 1 ? 'TAIL_REACHABLE' : '完整内容 agent recorded observation notice ' + i}</span>`}</dd>`).join('');
      await page.setContent(`<!doctype html><html lang="zh"><head><meta charset="utf-8"><style>${css}</style></head><body><div class="process-detail-modal-backdrop"><section class="process-detail-modal" role="dialog" aria-label="活动详情"><div class="process-detail-modal-header"><div><span class="eyebrow">过程记录</span><h4>活动</h4></div><button class="secondary-action">关闭</button></div><dl>${rows}</dl></section></div></body></html>`);
      const normal = await page.locator('dl').evaluate((dl) => ({
        client: dl.clientHeight, scroll: dl.scrollHeight,
        values: [...dl.querySelectorAll('dd')].map((dd) => {
          const rect = dd.getBoundingClientRect();
          const child = dd.firstElementChild.getBoundingClientRect();
          return { height: rect.height, child: child.height, clipped: child.bottom > rect.bottom + 1,
            overflow: getComputedStyle(dd).overflowY };
        }),
        rootOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      }));
      assert(normal.client > 150 && normal.scroll > normal.client, 'detail list must scroll');
      assert(normal.values.every((row) => row.height >= 20 && !row.clipped),
        `values compressed or clipped: ${JSON.stringify(normal.values)}`);
      assert(normal.values.every((row) => row.overflow === 'visible'), 'only list/body should scroll');
      assert(normal.rootOverflow <= 1);
      await page.locator('dd').last().scrollIntoViewIfNeeded();
      const tail = await page.locator('dd').last().evaluate((dd) => {
        const row = dd.getBoundingClientRect(), list = dd.parentElement.getBoundingClientRect();
        return { visible: row.top >= list.top - 1 && row.bottom <= list.bottom + 1,
          scrolled: dd.parentElement.scrollTop > 0 };
      });
      assert(tail.visible && tail.scrolled, 'last value cannot be reached');
      if (process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR) {
        fs.mkdirSync(process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR, { recursive: true });
        await page.screenshot({ path: path.join(process.env.AGENT_HUB_LAYOUT_EVIDENCE_DIR,
          `${device}-${count}-fields.png`) });
      }
      await page.locator('dl').evaluate((dl) => {
        dl.scrollTop = 0;
        const block = dl.querySelector('.bounded-text-block');
        block.classList.replace('is-collapsed', 'is-expanded');
        block.querySelector('button').setAttribute('aria-expanded', 'true');
      });
      const pre = page.locator('.bounded-text-block.is-expanded pre');
      await pre.focus();
      await pre.press('Control+End');
      await expect.poll(() => pre.evaluate((el) =>
        el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(2);
      const expanded = await pre.evaluate((el) => {
        const rect = el.getBoundingClientRect(), dd = el.closest('dd').getBoundingClientRect();
        const button = el.parentElement.querySelector('button'), br = button.getBoundingClientRect();
        const hit = document.elementFromPoint(br.left + br.width / 2, br.top + br.height / 2);
        return { height: rect.height, fits: rect.bottom <= dd.bottom + 1,
          scrolled: el.scrollTop > 0, end: el.scrollHeight - el.scrollTop - el.clientHeight,
          toolbar: hit === button || button.contains(hit) };
      });
      assert(expanded.height > viewport.height / 2 && expanded.fits && expanded.scrolled
        && expanded.end <= 2 && expanded.toolbar, `expanded reader clipped: ${JSON.stringify(expanded)}`);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}
