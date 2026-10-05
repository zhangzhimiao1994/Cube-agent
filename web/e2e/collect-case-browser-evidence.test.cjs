'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawnSync, spawn } = require('node:child_process');
const { startCaseBrowserFixture } = require('./case-browser-fixture.cjs');
const collectorPath = path.join(__dirname, 'collect-case-browser-evidence.cjs');

async function readReference(root, ref) {
  assert.deepEqual(Object.keys(ref).sort(), ['path', 'sha256', 'size_bytes']);
  assert(!ref.path.includes('\\') && !ref.path.includes('..') && !path.isAbsolute(ref.path));
  const bytes = await fs.readFile(path.join(root, ref.path));
  assert.equal(bytes.length, ref.size_bytes);
  assert.equal(createHash('sha256').update(bytes).digest('hex'), ref.sha256);
  return ref.path.endsWith('.json') ? JSON.parse(bytes) : bytes;
}
async function allFiles(root) {
  const result = [];
  for (const entry of await fs.readdir(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) result.push(...await allFiles(target));
    else result.push(target);
  }
  return result;
}
async function owned(t, mode, callback) {
  const fixture = await startCaseBrowserFixture({ mode });
  const intendedParent = path.resolve(__dirname, '../../..');
  const root = await fs.mkdtemp(path.join(intendedParent, 'r7b-browser-contract-'));
  t.after(async () => {
    await fixture.close();
    const pinned = path.resolve(root);
    assert.equal(path.dirname(pinned), intendedParent);
    assert(path.basename(pinned).startsWith('r7b-browser-contract-'));
    for (const target of [intendedParent, pinned]) {
      const info = await fs.lstat(target);
      assert(info.isDirectory() && !info.isSymbolicLink());
      const actual = await fs.realpath(target);
      assert.equal(process.platform === 'win32' ? actual.toLowerCase() : actual,
        process.platform === 'win32' ? target.toLowerCase() : target);
    }
    await fs.rm(pinned, { recursive: true, force: true });
  });
  await callback(fixture, root);
  for (const file of await allFiles(root)) {
    if (!file.endsWith('.json')) continue;
    const data = await fs.readFile(file, 'utf8');
    for (const secret of fixture.secrets) assert(!data.includes(secret), 'persisted secret');
    assert(!data.includes('?cap='), 'persisted capability URL');
  }
}

test('independent entrypoint exports the collector', async () => {
  // RED: the authorized independent collector does not yet exist.
  assert.equal(typeof require(collectorPath).collectCaseBrowserEvidence, 'function');
});

for (const mode of ['success', 'self-contained', 'root-array', 'fractional-frame', 'slow-ready']) {
  test(`real Chromium ${mode}: both viewport/frame PNGs and server readback`, { timeout: 60000 }, async t => {
    await owned(t, mode, async (fixture, root) => {
      const { collectCaseBrowserEvidence } = require(collectorPath);
      const descriptors = await collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root,
        ...(mode === 'slow-ready' ? { limits: { timeout: 2000 }, startupTimeout: 5000 } : {}) });
      assert.deepEqual(Object.keys(descriptors).sort(), ['desktop', 'mobile']);
      for (const [device, descriptor] of Object.entries(descriptors)) {
        assert.deepEqual(Object.keys(descriptor).sort(), ['bundle_file', 'checks', 'evidence_ref', 'observed_at', 'passed', 'schema_version']);
        assert.equal(descriptor.schema_version, 2);
        assert.equal(descriptor.passed, true);
        assert.equal(descriptor.evidence_ref, descriptor.bundle_file.path);
        const bundle = await readReference(root, descriptor.bundle_file);
        assert.deepEqual(Object.keys(bundle).sort(), ['captured_at', 'device', 'files', 'preview_identity', 'schema_version', 'scope', 'viewport']);
        assert.deepEqual(bundle.scope, fixture.scope);
        assert.equal(bundle.device, device);
        assert.deepEqual(bundle.viewport, device === 'desktop' ? { width: 1440, height: 960 } : { width: 390, height: 844 });
        assert.deepEqual(Object.keys(bundle.files).sort(), ['business', 'cleanup', 'frame_png', 'provenance', 'render', 'viewport_png']);
        const values = {};
        for (const [role, ref] of Object.entries(bundle.files)) values[role] = await readReference(root, ref);
        assert.equal(values.viewport_png.readUInt32BE(16), bundle.viewport.width);
        assert.equal(values.viewport_png.readUInt32BE(20), bundle.viewport.height);
        assert.equal(values.frame_png.readUInt32BE(16), values.render.frame.width);
        assert.equal(values.frame_png.readUInt32BE(20), values.render.frame.height);
        assert.equal(values.render.content_url_sha256, values.cleanup.revocation.content_url_sha256);
        assert.equal(values.render.assets.length, mode === 'self-contained' ? 0 : 2);
        assert.deepEqual(values.render.console_errors, []);
        assert.deepEqual(values.render.page_errors, []);
        assert.equal(values.business.mutation.body.success, true);
        assert.equal(values.business.readback.body.success, true);
        assert.equal(values.business.mutation.body.record.id, values.business.readback.body.record.id);
        assert.equal(values.business.readback.body.record.value, values.business.unique_value);
        assert.equal(values.business.readback.after_reload, true);
        assert.equal(values.business.readback.cache, 'no-store');
        assert(fixture.state.records.some(r => r.value === values.business.unique_value));
        const beforeRecords = Array.isArray(values.business.before.body) ? values.business.before.body : values.business.before.body.records;
        assert(beforeRecords.every(r => r.value !== values.business.unique_value));
        assert.deepEqual(Object.keys(values.business.selectors).sort(),
          ['before_records', 'before_success', 'id_field', 'mutation_record', 'readback_record', 'success', 'value_field']);
        assert.deepEqual(values.business.selectors.before_success, mode === 'root-array' ? null : ['success']);
        if (mode !== 'root-array') assert.equal(values.business.before.body.success, true);
        if (mode === 'fractional-frame') {
          const bounds = values.render.frame.bounds;
          assert(!Number.isInteger(bounds.x) && !Number.isInteger(bounds.width));
          assert.equal(values.render.frame.width, Math.ceil(bounds.x + bounds.width) - Math.floor(bounds.x));
          assert.equal(values.render.frame.height, Math.ceil(bounds.y + bounds.height) - Math.floor(bounds.y));
        }
        assert.equal(values.cleanup.cleanup_record.cleanup_receipt.status, 'confirmed');
        assert.equal(values.cleanup.revocation.status, 404);
        const times = [values.provenance.captured_at, bundle.captured_at, values.render.observed_at,
          values.business.before.observed_at, values.business.mutation.observed_at, values.business.readback.observed_at,
          values.cleanup.cleanup_record.cleanup_receipt.requested_at, values.cleanup.cleanup_record.cleanup_receipt.observed_at,
          values.cleanup.revocation.observed_at, descriptor.observed_at].map(Date.parse);
        for (let i = 1; i < times.length; i++) assert(times[i] >= times[i - 1]);
        assert(times[4] > times[3] && times[5] > times[4]);
      }
      assert.equal(fixture.state.previews.length, 2);
      assert(fixture.state.previews.every(p => p.stopped));
      assert.deepEqual(fixture.state.revokedRequests.map(url => createHash('sha256').update(url).digest('hex')),
        fixture.state.previews.map(p => createHash('sha256').update(p.preview_url).digest('hex')));
      assert.equal(fixture.state.events.filter(e => e === 'login').length, 2);
      assert.equal(fixture.state.events.filter(e => e === 'project').length, 2);
      assert.equal(fixture.state.events.filter(e => e === 'history').length, 2);
      assert.equal(fixture.state.events.filter(e => e === 'conversation').length, 2);
      const python = path.resolve(__dirname, process.platform === 'win32' ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python');
      const validated = spawnSync(python, ['-c',
        'import json,sys; from pathlib import Path; from agent_hub.harness.browser_evidence import validate_case_browser_bundle; d=json.load(sys.stdin); [validate_case_browser_bundle(v,evidence_root=Path(d["root"]),expected_scope=d["scope"],validated_manifest=d["manifest"],device=k) for k,v in d["descriptors"].items()]; print("both bundles validated")'],
      { input: JSON.stringify({ root, scope: fixture.scope, manifest: fixture.validatedManifest, descriptors }),
        env: { ...process.env, PYTHONPATH: path.resolve(__dirname, '../../src') }, encoding: 'utf8' });
      assert.equal(validated.status, 0, validated.stderr || String(validated.error || 'validator failed'));
      assert.equal(validated.stdout.trim(), 'both bundles validated');
    });
  });
}

test('preview ready hook awaits both device handshakes after provenance and before business', { timeout: 60000 }, async t => {
  await owned(t, 'success', async (fixture, root) => {
    const calls = [];
    const { collectCaseBrowserEvidence } = require(collectorPath);
    const descriptors = await collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root,
      onPreviewReady: async payload => {
        assert.deepEqual(Object.keys(payload).sort(), ['device', 'previewIdentity']);
        const preview = fixture.state.previews.at(-1);
        assert.deepEqual(payload.previewIdentity, preview.identity);
        assert.equal(fixture.state.events.at(-1), 'provenance');
        assert(!preview.stopped);
        const requests = fixture.state.events.filter(e => /^(GET|POST):/.test(e)).length;
        await new Promise(resolve => setTimeout(resolve, 80));
        assert.equal(fixture.state.events.filter(e => /^(GET|POST):/.test(e)).length, requests);
        assert.equal(fixture.state.mutations.length, calls.length);
        calls.push(payload.device);
        // The operator cannot alter the collector's pinned cleanup/bundle identity.
        payload.previewIdentity.preview_id = 'operator-local-copy';
      } });
    assert.deepEqual(calls, ['desktop', 'mobile']);
    for (const device of calls) assert.equal(descriptors[device].passed, true);
    assert.equal(fixture.state.mutations.length, 2);
    assert(fixture.state.previews.every(p => p.stopped));
  });
});

test('preview ready hook failure never starts business and still performs strict cleanup', { timeout: 60000 }, async t => {
  await owned(t, 'success', async (fixture, root) => {
    const calls = [];
    const { collectCaseBrowserEvidence } = require(collectorPath);
    await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root,
      onPreviewReady: async ({ device }) => { calls.push(device); throw new Error(fixture.env.CASE_BROWSER_PASSWORD); } }), error => {
      assert.equal(error.code, 'collection_failed');
      assert.deepEqual(error.failures.map(f => f.error_code), ['preview_ready_hook_failed', 'preview_ready_hook_failed']);
      assert(error.failures.every(f => f.cleanup_error_code === null && f.cleanup_file));
      return true;
    });
    assert.deepEqual(calls, ['desktop', 'mobile']);
    assert.equal(fixture.state.mutations.length, 0);
    assert(!fixture.state.events.some(e => /^(GET|POST):/.test(e)));
    assert(fixture.state.previews.every(p => p.stopped));
    assert.equal(fixture.state.events.filter(e => e === 'cleanup').length, 2);
    assert.equal(fixture.state.revokedRequests.length, 2);
    assert(fixture.state.revokedCookies.every(cookie => cookie.includes(fixture.secrets[1])));
    assert(!(await allFiles(root)).some(f => path.basename(f) === 'bundle.json'));
  });
});

test('preview ready hook rejects a nonfunction before resource allocation', async t => {
  await owned(t, 'success', async (fixture, root) => {
    const { collectCaseBrowserEvidence } = require(collectorPath);
    await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root, onPreviewReady: 'not-a-function' }),
      { code: 'invalid_preview_ready_hook' });
    assert.equal(fixture.state.events.length, 0);
  });
});

test('owner start scope mismatch rejects evidence but cleans both actual UI-created previews', { timeout: 60000 }, async t => {
  await owned(t, 'success', async (fixture, root) => {
    const { collectCaseBrowserEvidence } = require(collectorPath);
    const scope = { ...fixture.scope, project_id: 'review-other-project' };
    await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, scope, evidenceRoot: root }), error => {
      assert.equal(error.code, 'collection_failed');
      assert.deepEqual(error.failures.map(f => f.error_code), ['start_scope_mismatch', 'start_scope_mismatch']);
      assert(error.failures.every(f => f.cleanup_error_code === null && f.cleanup_file));
      return true;
    });
    assert.equal(fixture.state.previews.length, 2);
    assert(fixture.state.previews.every(p => p.stopped));
    assert.equal(fixture.state.events.filter(e => e === 'stop').length, 2);
    assert.equal(fixture.state.events.filter(e => e === 'cleanup').length, 2);
    assert.equal(fixture.state.revokedRequests.length, 2);
    assert.equal(fixture.state.mutations.length, 0);
    assert(!(await allFiles(root)).some(f => path.basename(f) === 'bundle.json'));
  });
});

test('configured credential in a deeply nested JSON key rejects before any business evidence write', { timeout: 60000 }, async t => {
  await owned(t, 'success', async (fixture, root) => {
    fixture.state.records.push({ id: 'existing', value: 'old', nested: [{ diagnostic: {
      [`diagnostic-${fixture.env.CASE_BROWSER_PASSWORD}`]: 'ordinary' } }] });
    const { collectCaseBrowserEvidence } = require(collectorPath);
    await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root }), error => {
      assert.equal(error.code, 'collection_failed');
      assert.deepEqual(error.failures.map(f => f.error_code), ['secret_in_observation', 'secret_in_observation']);
      assert(error.failures.every(f => f.cleanup_error_code === null));
      return true;
    });
    assert.equal(fixture.state.mutations.length, 0);
    assert(fixture.state.previews.every(p => p.stopped));
    assert.equal(fixture.state.revokedRequests.length, 2);
    assert(!(await allFiles(root)).some(f => ['business.json', 'bundle.json'].includes(path.basename(f))));
  });
});

test('startup timeout is independently bounded before browser allocation', async t => {
  await owned(t, 'success', async (fixture, root) => {
    const { collectCaseBrowserEvidence } = require(collectorPath);
    for (const startupTimeout of [0, 300001, true]) {
      await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root, startupTimeout }),
        { code: 'invalid_startup_timeout' });
    }
    assert.equal(fixture.state.events.length, 0);
  });
});

test('adversarial equal-clock cleanup observations cannot earn a passed bundle', { timeout: 60000 }, async t => {
  await owned(t, 'cleanup-equality', async (fixture, root) => {
    const RealDate = global.Date;
    const timers = [];
    let frozen = null;
    // Deliberate invalid contract data only: bind receipt time to actual readback.
    global.Date = class extends RealDate {
      constructor(...args) { super(...(args.length ? args : [frozen ?? RealDate.now()])); }
      static now() { return frozen ?? RealDate.now(); }
    };
    fixture.state.onReadback = () => {
      frozen = RealDate.now(); fixture.state.cleanupTime = new RealDate(frozen).toISOString();
      timers.push(setTimeout(() => { frozen = null; }, 2000));
    };
    try {
      const { collectCaseBrowserEvidence } = require(collectorPath);
      await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root }), error => {
        assert.equal(error.code, 'collection_failed');
        assert(error.failures.every(f => f.cleanup_error_code === 'cleanup_stale_observation'));
        return true;
      });
      assert(fixture.state.previews.every(p => p.stopped));
      assert.equal(fixture.state.revokedRequests.length, 2);
      assert(!(await allFiles(root)).some(f => path.basename(f) === 'bundle.json'));
    } finally { global.Date = RealDate; timers.forEach(clearTimeout); }
  });
});

test('adversarial microsecond cleanup ordering is not flattened into millisecond success', { timeout: 60000 }, async t => {
  await owned(t, 'microsecond-reversed-cleanup', async (fixture, root) => {
    const { collectCaseBrowserEvidence } = require(collectorPath);
    await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root }), { code: 'collection_failed' });
    assert(fixture.state.previews.every(p => p.stopped));
    assert.equal(fixture.state.revokedRequests.length, 2);
    assert(!(await allFiles(root)).some(f => path.basename(f) === 'bundle.json'));
  });
});

for (const mode of ['foreign-owner-start', 'invalid-start-id', 'invalid-start-url']) {
  test(`${mode}: unresolved cleanup is explicit and never deletes a foreign owner`, { timeout: 60000 }, async t => {
    await owned(t, mode, async (fixture, root) => {
      const { collectCaseBrowserEvidence } = require(collectorPath);
      await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root }), error => {
        assert.equal(error.code, 'collection_failed');
        assert(error.failures.every(f => f.cleanup_error_code === 'cleanup_unresolved'));
        return true;
      });
      assert.equal(fixture.state.previews.length, 2);
      assert.equal(fixture.state.events.filter(e => e === 'stop').length, mode === 'invalid-start-url' ? 2 : 0);
      assert.equal(fixture.state.events.filter(e => e === 'cleanup').length, mode === 'invalid-start-url' ? 2 : 0);
      assert.equal(fixture.state.revokedRequests.length, 0);
    });
  });
}

for (const mode of ['echo', 'preexisting', 'failed-200', 'readback-failed-200', 'blank', 'console-error',
  'page-error', 'broken-asset', 'missing-image', 'unknown-cleanup', 'contradictory-cleanup',
  'foreign-cleanup', 'stale-cleanup', 'missing-provenance', 'wrong-source', 'secret-body', 'duplicate-body', 'revocation-200',
  'nested-preexisting', 'invisible', 'clipped', 'overflow', 'oversized-body', 'external-asset', 'static', 'bad-identity',
  'before-failed-200', 'before-missing-success', 'before-unknown-success', 'before-invalid-id', 'before-missing-value',
  'uuid-key-preexisting', 'uuid-value-preexisting']) {
  test(`rejects ${mode}; always stops and GETs cleanup plus exact old URL`, { timeout: 60000 }, async t => {
    await owned(t, mode, async (fixture, root) => {
      const { collectCaseBrowserEvidence } = require(collectorPath);
      await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root, ...(mode === 'blank' ? { limits: { timeout: 2000 } } : {}) }), error => {
        assert.equal(error.code, 'collection_failed');
        assert.deepEqual(error.failures.map(f => f.device), ['desktop', 'mobile']);
        assert(!String(error).includes(fixture.env.CASE_BROWSER_PASSWORD));
        return true;
      });
      assert.equal(fixture.state.previews.length, 2);
      assert(fixture.state.previews.every(p => p.stopped));
      assert.equal(fixture.state.events.filter(e => e === 'cleanup').length, 2);
      assert.deepEqual(fixture.state.revokedRequests.map(url => createHash('sha256').update(url).digest('hex')),
        fixture.state.previews.map(p => createHash('sha256').update(p.preview_url).digest('hex')));
      assert(fixture.state.revokedCookies.every(cookie => cookie.includes(fixture.secrets[1])), 'original capability cookie must accompany revocation GET');
      assert(!(await allFiles(root)).some(f => f.endsWith('/bundle.json') || f.endsWith('\\bundle.json')));
    });
  });
}

test('stop response error still checks historical receipt and revocation', { timeout: 60000 }, async t => {
  await owned(t, 'stop-error', async (fixture, root) => {
    const { collectCaseBrowserEvidence } = require(collectorPath);
    await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root }), { code: 'collection_failed' });
    assert.equal(fixture.state.events.filter(e => e === 'cleanup').length, 2);
    assert.equal(fixture.state.revokedRequests.length, 2);
    assert(fixture.state.previews.every(p => p.stopped));
  });
});

test('invalid or arbitrary targets fail before browser/network allocation', async t => {
  await owned(t, 'success', async (fixture, root) => {
    const { collectCaseBrowserEvidence } = require(collectorPath);
    for (const target of ['https://foreign.invalid/records', '../records', '//foreign/records', 'api/%252e%252e/records']) {
      const scenario = structuredClone(fixture.scenario);
      scenario.business.beforePath = target;
      await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, scenario, evidenceRoot: root }), { code: 'invalid_scenario' });
    }
    assert.equal(fixture.state.events.length, 0);
  });
});

test('asset observation budget exhaustion fails rather than dropping proof', { timeout: 60000 }, async t => {
  await owned(t, 'success', async (fixture, root) => {
    const { collectCaseBrowserEvidence } = require(collectorPath);
    await assert.rejects(() => collectCaseBrowserEvidence({ ...fixture, evidenceRoot: root, limits: { assets: 0 } }), { code: 'collection_failed' });
    assert(fixture.state.previews.every(p => p.stopped));
    assert.equal(fixture.state.revokedRequests.length, 2);
  });
});

test('CLI help has no side effects and documents explicit inputs', () => {
  const result = spawnSync(process.execPath, [collectorPath, '--help'], { encoding: 'utf8' });
  assert.equal(result.status, 0);
  for (const option of ['--scope', '--scenario', '--evidence-root', '--base-url']) assert(result.stdout.includes(option));
});

test('real CLI collects both devices with env-only login and numeric record IDs', { timeout: 60000 }, async t => {
  await owned(t, 'numeric-id', async (fixture, root) => {
    const scopeFile = path.join(root, 'scope-input.json'); const scenarioFile = path.join(root, 'scenario-input.json');
    await fs.writeFile(scopeFile, JSON.stringify({ scope: fixture.scope, validated_manifest: fixture.validatedManifest }), { flag: 'wx' });
    await fs.writeFile(scenarioFile, JSON.stringify(fixture.scenario), { flag: 'wx' });
    const output = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [collectorPath, '--scope', scopeFile, '--scenario', scenarioFile,
        '--base-url', fixture.baseURL, '--evidence-root', root], { env: { ...process.env, ...fixture.env }, windowsHide: true });
      let stdout = ''; let stderr = '';
      child.stdout.on('data', bytes => { stdout += bytes; }); child.stderr.on('data', bytes => { stderr += bytes; });
      child.on('error', reject); child.on('close', code => { if (code !== 0) reject(new Error(stderr || 'CLI failed')); else resolve(stdout); });
    });
    const descriptors = JSON.parse(output);
    for (const device of ['desktop', 'mobile']) {
      const bundle = await readReference(root, descriptors[device].bundle_file);
      const business = await readReference(root, bundle.files.business);
      assert(Number.isSafeInteger(business.mutation.body.record.id) && business.mutation.body.record.id > 0);
      assert.equal(business.mutation.body.record.id, business.readback.body.record.id);
    }
    assert(fixture.state.previews.every(p => p.stopped));
  });
});
