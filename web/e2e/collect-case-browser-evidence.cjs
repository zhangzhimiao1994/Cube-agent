'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { chromium } = require('@playwright/test');

const VIEWPORTS = { desktop: { width: 1440, height: 960 }, mobile: { width: 390, height: 844 } };
const DEFAULT_STARTUP_TIMEOUT = 300000;
const DEFAULT_LIMITS = { timeout: 12000, assets: 128, assetBytes: 4 * 1024 * 1024,
  totalAssetBytes: 16 * 1024 * 1024, jsonBytes: 256 * 1024, records: 512, text: 16384, errors: 32 };
const BROKER = ['attachment', 'unit_build', 'unit_install', 'unit_probe', 'unit_start',
  'cgroup_build', 'cgroup_install', 'cgroup_probe', 'cgroup_start', 'mount_unit', 'work_path', 'owned_directory'];
const UNOBSERVED = ['private_network_namespace', 'private_port', 'other_mount_namespaces'];
const EXCLUDED = new Set(['node_modules', '.git', '.preview-staging', '.venv', '.npmrc', '.ssh', '.aws', '.codex']);
const DIGEST = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const sha = value => createHash('sha256').update(value).digest('hex');
const utc = () => new Date().toISOString();
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);

class CollectorError extends Error {
  constructor(code, extra = {}) { super(code); this.name = 'CollectorError'; this.code = code; Object.assign(this, extra); }
}
function need(condition, code) { if (!condition) throw new CollectorError(code); }
function exact(value, keys, code) {
  need(isObject(value) && Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k)), code);
}
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (isObject(value)) return `{${Object.keys(value).sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b))).map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  need(value === null || typeof value === 'string' || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value)), 'invalid_json');
  return JSON.stringify(value);
}
const same = (a, b) => canonical(a) === canonical(b);
function timestamp(value, code) {
  need(typeof value === 'string' && value.length <= 40 && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value)), code);
  return Date.parse(value);
}
function timestampMicros(value, code) {
  const milliseconds = timestamp(value, code);
  const fraction = value.match(/\.(\d{1,6})(?:Z|[+-]\d{2}:\d{2})$/)?.[1] || '';
  return BigInt(milliseconds) * 1000n + BigInt(fraction.padEnd(6, '0').slice(3));
}
async function after(previous) {
  while (Date.now() <= Date.parse(previous)) await new Promise(resolve => setTimeout(resolve, 2));
  return utc();
}
function text(value, limit, code) { need(typeof value === 'string' && value.trim().length > 0 && value.length <= limit && !value.includes('\0'), code); return value; }
function validRecordId(value) {
  return (typeof value === 'string' && value.trim().length > 0 && value.length <= 512 && !value.includes('\0'))
    || (typeof value === 'number' && Number.isSafeInteger(value) && value > 0);
}
function relative(value, code, allowQuery = false) {
  text(value, 512, code);
  need(/^[A-Za-z0-9_./-]+$/.test(value.split('?')[0]), code);
  need(!value.startsWith('/') && !value.includes('\\') && !value.includes('#') && /^[\x21-\x7e]+$/.test(value), code);
  need(allowQuery || !value.includes('?'), code);
  let member = value.split('?')[0];
  for (let i = 0; i < 5; i++) {
    need(!member.includes(':') && !member.includes('\\') && !/[\x00-\x20\x7f]/.test(member)
      && member.split('/').every(p => p && p !== '.' && p !== '..'), code);
    let decoded;
    try { decoded = decodeURIComponent(member); } catch { throw new CollectorError(code); }
    if (decoded === member) return value;
    need(decoded.split('/').length === member.split('/').length, code);
    member = decoded;
  }
  throw new CollectorError(code);
}

// JSON.parse discards duplicate keys. Inspect structure before accepting network bytes.
function strictJSON(bytes, maxBytes) {
  need(bytes.length <= maxBytes, 'json_budget_exhausted');
  const source = bytes.toString('utf8');
  need(Buffer.from(source).equals(bytes), 'invalid_json');
  let offset = 0;
  const whitespace = () => { while (/\s/.test(source[offset] || '') && offset < source.length) offset++; };
  function string() {
    const start = offset++;
    while (offset < source.length) {
      if (source[offset] === '\\') { offset += 2; continue; }
      if (source[offset++] === '"') return JSON.parse(source.slice(start, offset));
    }
    throw new CollectorError('invalid_json');
  }
  function value(depth = 0) {
    need(depth <= 32, 'json_budget_exhausted'); whitespace();
    if (source[offset] === '"') return string();
    if (source[offset] === '{') {
      offset++; whitespace(); const keys = new Set();
      if (source[offset] === '}') { offset++; return; }
      while (offset < source.length) {
        need(source[offset] === '"', 'invalid_json'); const key = string();
        need(!keys.has(key), 'duplicate_json_key'); keys.add(key); whitespace();
        need(source[offset++] === ':', 'invalid_json'); value(depth + 1); whitespace();
        const delimiter = source[offset++]; if (delimiter === '}') return;
        need(delimiter === ',', 'invalid_json'); whitespace();
      }
    } else if (source[offset] === '[') {
      offset++; whitespace(); if (source[offset] === ']') { offset++; return; }
      while (offset < source.length) {
        value(depth + 1); whitespace(); const delimiter = source[offset++];
        if (delimiter === ']') return; need(delimiter === ',', 'invalid_json');
      }
    } else {
      const match = source.slice(offset).match(/^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/);
      need(match, 'invalid_json'); const primitive = JSON.parse(match[0]);
      need(typeof primitive !== 'number' || Number.isFinite(primitive), 'invalid_json'); offset += match[0].length; return;
    }
    throw new CollectorError('invalid_json');
  }
  try { value(); whitespace(); need(offset === source.length, 'invalid_json'); return JSON.parse(source); }
  catch (error) { throw error instanceof CollectorError ? error : new CollectorError('invalid_json'); }
}
function select(body, keys) {
  let current = body;
  for (const key of keys) { need(isObject(current) && Object.hasOwn(current, key), 'missing_json_selector'); current = current[key]; }
  return current;
}
function insert(root, keys, value) {
  let current = root;
  for (const key of keys.slice(0, -1)) { if (!Object.hasOwn(current, key)) current[key] = Object.create(null); need(isObject(current[key]), 'overlapping_json_selectors'); current = current[key]; }
  current[keys.at(-1)] = value;
}
function minimalBody(body, selectors, role, limits) {
  const recordKeys = selectors[role === 'before' ? 'before_records' : role === 'mutation' ? 'mutation_record' : 'readback_record'];
  const record = select(body, recordKeys);
  const fields = r => {
    need(isObject(r) && Object.hasOwn(r, selectors.value_field), 'invalid_business_record');
    need(validRecordId(r[selectors.id_field]), 'invalid_business_record');
    text(r[selectors.value_field], 512, 'invalid_business_record');
    const result = { [selectors.value_field]: r[selectors.value_field] };
    if (Object.hasOwn(r, selectors.id_field)) result[selectors.id_field] = r[selectors.id_field];
    return result;
  };
  let selected;
  if (role === 'before') {
    need(Array.isArray(record) && record.length <= limits.records && record.every(isObject), 'records_budget_exhausted');
    if (selectors.before_success === null) need(recordKeys.length === 0 && Array.isArray(body), 'before_status_unproven');
    else need(isObject(body) && select(body, selectors.before_success) === true, 'before_status_unproven');
    record.forEach(fields); selected = record;
  }
  else selected = fields(record);
  if (recordKeys.length === 0) return selected;
  const result = Object.create(null); insert(result, recordKeys, selected);
  if (role !== 'before') insert(result, selectors.success, select(body, selectors.success));
  else insert(result, selectors.before_success, true);
  return result;
}
function secretsGuard(secrets) {
  return value => {
    const scan = item => {
      if (typeof item === 'string') {
        need(!secrets.some(secret => secret && item.includes(secret)), 'secret_in_observation');
        need(!/\b(?:Bearer\s|https?:\/\/[^\s]*[?&](?:token|cap|key)=)/i.test(item), 'secret_in_observation');
      } else if (Array.isArray(item)) item.forEach(scan);
      else if (isObject(item)) for (const [key, child] of Object.entries(item)) {
        need(!/^(?:password|passwd|secret|credentials?|authorization|cookie|access_token|refresh_token|api_key|capability|token)$/i.test(key), 'secret_in_observation');
        scan(key); scan(child);
      }
    };
    // Resource name "capability" is a public enum value, not a credential field.
    scan(value); return value;
  };
}
function identity(value, scope, principal) {
  exact(value, ['preview_id', 'kind', 'tenant_id', 'user_id', 'project_id', 'conversation_id',
    'workspace_session_id', 'runtime_handle', 'source', 'display_root', 'display_entrypoint'], 'invalid_identity');
  need(UUID.test(value.preview_id) && UUID.test(value.tenant_id) && (value.user_id === null || UUID.test(value.user_id)), 'invalid_identity');
  need(value.kind === 'dynamic' || value.kind === 'static', 'invalid_identity');
  for (const key of ['project_id', 'conversation_id', 'workspace_session_id']) need(value[key] === scope[key], 'identity_scope_mismatch');
  for (const key of ['tenant_id', 'user_id']) need(value[key] === principal[key] && principal[key] === scope.execution_identity[key], 'identity_owner_mismatch');
  exact(value.source, ['scheme', 'sha256'], 'invalid_identity');
  need(DIGEST.test(value.source.sha256) && value.source.scheme === (value.kind === 'dynamic' ? 'preview-broker-tree-v2' : 'preview-static-tree-v1'), 'invalid_identity');
  need(value.kind === 'dynamic' ? /^[0-9a-f]{32}$/.test(value.runtime_handle) : value.runtime_handle === null, 'invalid_identity');
  if (value.display_root !== '.') relative(value.display_root, 'invalid_identity');
  relative(value.display_entrypoint, 'invalid_identity');
  return value;
}
function validateProvenance(value, expectedIdentity, manifest) {
  exact(value, ['schema_version', 'identity', 'snapshot_manifest', 'captured_at'], 'invalid_provenance');
  need(value.schema_version === 1 && same(value.identity, expectedIdentity), 'provenance_identity_mismatch');
  need(timestamp(value.captured_at, 'invalid_provenance') <= Date.now(), 'future_provenance');
  const snapshot = value.snapshot_manifest;
  exact(snapshot, ['schema_version', 'algorithm', 'selection_policy', 'manifest_sha256', 'file_count', 'total_bytes'], 'invalid_provenance');
  need(snapshot.schema_version === 1 && snapshot.algorithm === 'workspace-manifest-sha256-v1'
    && DIGEST.test(snapshot.manifest_sha256) && Number.isInteger(snapshot.file_count) && Number.isInteger(snapshot.total_bytes), 'invalid_provenance');
  const dynamic = expectedIdentity.kind === 'dynamic';
  need(snapshot.selection_policy === (dynamic ? 'dynamic-staged-session-v1' : 'static-display-root-v1'), 'invalid_provenance');
  const selected = Object.create(null);
  const prefix = expectedIdentity.display_root === '.' ? '' : `${expectedIdentity.display_root}/`;
  for (const [name, meta] of Object.entries(manifest)) {
    if (dynamic && name.split('/').some(p => EXCLUDED.has(p) || p === '.env' || p.startsWith('.env.'))) continue;
    if (!dynamic && !name.startsWith(prefix)) continue;
    selected[dynamic ? name : name.slice(prefix.length)] = meta;
  }
  const entries = Object.entries(selected);
  need(entries.length > 0 && entries.length <= (dynamic ? 4096 : 10000), 'source_budget_exhausted');
  if (!dynamic) need(Object.hasOwn(selected, expectedIdentity.display_entrypoint), 'source_entrypoint_missing');
  const total = entries.reduce((sum, [, meta]) => sum + meta[0], 0);
  need(total <= (dynamic ? 32 : 128) * 1024 * 1024, 'source_budget_exhausted');
  need(snapshot.file_count === entries.length && snapshot.total_bytes === total && snapshot.manifest_sha256 === sha(canonical(selected)), 'source_manifest_mismatch');
}
function validateCleanup(record, expectedIdentity, lastObservation) {
  exact(record, ['identity', 'cleanup_receipt', 'retention_expires_at'], 'invalid_cleanup');
  need(same(record.identity, expectedIdentity), 'cleanup_identity_mismatch');
  if (record.retention_expires_at !== null) timestamp(record.retention_expires_at, 'invalid_cleanup');
  const receipt = record.cleanup_receipt;
  exact(receipt, ['schema_version', 'identity', 'observation_id', 'requested_at', 'observed_at',
    'reason', 'status', 'coverage', 'observations', 'unobserved'], 'invalid_cleanup');
  need(receipt.schema_version === 1 && same(receipt.identity, expectedIdentity) && UUID.test(receipt.observation_id), 'cleanup_identity_mismatch');
  need(receipt.status === 'confirmed' && ['explicit', 'expired', 'replaced', 'disconnect', 'shutdown', 'recovery'].includes(receipt.reason), 'cleanup_unconfirmed');
  const requested = timestampMicros(receipt.requested_at, 'invalid_cleanup');
  const observed = timestampMicros(receipt.observed_at, 'invalid_cleanup');
  const previous = timestampMicros(lastObservation, 'invalid_cleanup');
  need(requested >= previous && observed >= requested && observed <= BigInt(Date.now()) * 1000n, 'cleanup_time_mismatch');
  const dynamic = expectedIdentity.kind === 'dynamic';
  const required = [...(dynamic ? BROKER : ['serving_thread', 'listener', 'accepted_threads', 'accepted_sockets', 'proxy_operations', 'proxy_sockets']), 'capability', 'snapshot'];
  need(receipt.coverage === (dynamic ? 'dynamic-systemd-tmpfs-v1' : 'static-loopback-v1') && same(receipt.unobserved, dynamic ? UNOBSERVED : [])
    && Array.isArray(receipt.observations) && same(receipt.observations.map(f => f.resource), required), 'cleanup_coverage_mismatch');
  for (const fact of receipt.observations) {
    exact(fact, ['resource', 'observer', 'observed_at', 'result', 'reason_code', 'load_state', 'active_state', 'main_pid', 'identity_match'], 'invalid_cleanup');
    const at = timestampMicros(fact.observed_at, 'invalid_cleanup');
    need(at >= requested && at > previous && at <= observed, 'cleanup_stale_observation');
    need(fact.observer === (BROKER.includes(fact.resource) ? 'broker' : 'manager') && fact.reason_code === 'observed', 'invalid_cleanup');
    const unit = fact.resource.startsWith('unit_') || fact.resource === 'mount_unit';
    const allowed = unit ? (fact.resource === 'mount_unit' ? ['inactive'] : ['inactive', 'failed'])
      : fact.resource.startsWith('cgroup_') ? ['absent', 'empty'] : fact.resource === 'work_path' ? ['absent', 'not_mountpoint']
        : ['attachment', 'serving_thread'].includes(fact.resource) ? ['exited'] : fact.resource === 'capability' ? ['revoked']
          : ['listener', 'accepted_sockets', 'proxy_sockets'].includes(fact.resource) ? ['closed']
            : ['accepted_threads', 'proxy_operations'].includes(fact.resource) ? ['drained'] : ['absent'];
    need(allowed.includes(fact.result), 'cleanup_unconfirmed');
    if (unit) need(['loaded', 'not-found'].includes(fact.load_state) && fact.active_state === fact.result && fact.identity_match === true
      && (fact.load_state !== 'not-found' || fact.result === 'inactive') && fact.main_pid === (fact.resource === 'mount_unit' ? null : 0), 'invalid_cleanup');
    else need([fact.load_state, fact.active_state, fact.main_pid, fact.identity_match].every(v => v === null), 'invalid_cleanup');
  }
}
function validateInput(options) {
  const { scope, scenario, validatedManifest, baseURL, evidenceRoot } = options;
  need(options.onPreviewReady === undefined || typeof options.onPreviewReady === 'function', 'invalid_preview_ready_hook');
  const startupTimeout = options.startupTimeout ?? DEFAULT_STARTUP_TIMEOUT;
  need(Number.isSafeInteger(startupTimeout) && startupTimeout > 0 && startupTimeout <= DEFAULT_STARTUP_TIMEOUT, 'invalid_startup_timeout');
  let origin;
  try { origin = new URL(baseURL); } catch { throw new CollectorError('invalid_origin'); }
  need(['http:', 'https:'].includes(origin.protocol) && !origin.username && !origin.password && !origin.search && !origin.hash && origin.pathname === '/', 'invalid_origin');
  exact(scope, ['execution_identity', 'case_id', 'project_id', 'conversation_id', 'run_id', 'workspace_session_id'], 'invalid_scope');
  exact(scope.execution_identity, ['execution_id', 'base_url', 'tenant_id', 'user_id', ...(Object.hasOwn(scope.execution_identity || {}, 'model_profile') ? ['model_profile'] : [])], 'invalid_scope');
  need(UUID.test(scope.execution_identity.tenant_id) && UUID.test(scope.execution_identity.user_id), 'invalid_scope');
  text(scope.execution_identity.execution_id, 512, 'invalid_scope');
  need(new URL(scope.execution_identity.base_url).origin === origin.origin, 'scope_origin_mismatch');
  if (Object.hasOwn(scope.execution_identity, 'model_profile')) need(isObject(scope.execution_identity.model_profile), 'invalid_scope');
  for (const key of ['case_id', 'project_id', 'conversation_id', 'run_id', 'workspace_session_id']) text(scope[key], 128, 'invalid_scope');
  need(isObject(validatedManifest) && Object.keys(validatedManifest).length > 0 && Object.keys(validatedManifest).length <= 10000, 'invalid_manifest');
  for (const [name, meta] of Object.entries(validatedManifest)) {
    text(name, 512, 'invalid_manifest');
    need(!name.includes('\\') && !name.includes(':') && !/[\x00-\x1f\x7f]/.test(name)
      && Buffer.from(name).toString() === name && name.split('/').every(p => p && p !== '.' && p !== '..')
      && !['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(name.split('/')[0].trim().toUpperCase()), 'invalid_manifest');
    need(Array.isArray(meta) && meta.length === 2 && Number.isSafeInteger(meta[0]) && meta[0] >= 0 && DIGEST.test(meta[1]), 'invalid_manifest');
  }
  need(typeof evidenceRoot === 'string' && path.isAbsolute(evidenceRoot), 'invalid_evidence_root');
  exact(scenario, ['login', 'navigation', 'preview', 'render', 'business'], 'invalid_scenario');
  exact(scenario.login, ['path', 'username', 'password', 'submit', 'ready', 'usernameEnv', 'passwordEnv'], 'invalid_scenario');
  need(/^\/[A-Za-z0-9/_-]*$/.test(scenario.login.path) && !scenario.login.path.startsWith('//'), 'invalid_scenario');
  for (const key of ['username', 'password', 'submit', 'ready', 'usernameEnv', 'passwordEnv']) text(scenario.login[key], 512, 'invalid_scenario');
  need(Array.isArray(scenario.navigation) && scenario.navigation.length >= 4 && scenario.navigation.length <= 24
    && scenario.navigation.at(-1).kind === 'preview' && ['project', 'history', 'conversation'].every(kind => scenario.navigation.some(step => step.kind === kind)), 'invalid_scenario');
  for (const step of scenario.navigation) {
    exact(step, ['kind', 'selector'], 'invalid_scenario');
    need(['project', 'history', 'conversation', 'workspace', 'artifact', 'preview'].includes(step.kind), 'invalid_scenario');
    text(step.selector, 512, 'invalid_scenario');
  }
  need(scenario.navigation.filter(step => step.kind === 'preview').length === 1, 'invalid_scenario');
  need(isObject(scenario.preview) && Object.keys(scenario.preview).every(k => ['frame', 'ready', 'open'].includes(k)), 'invalid_scenario');
  text(scenario.preview.frame, 512, 'invalid_scenario'); text(scenario.preview.ready, 512, 'invalid_scenario');
  if (scenario.preview.open) { exact(scenario.preview.open, ['selector', 'mode'], 'invalid_scenario'); text(scenario.preview.open.selector, 512, 'invalid_scenario'); need(['popup', 'same-page'].includes(scenario.preview.open.mode), 'invalid_scenario'); }
  exact(scenario.render, ['controls'], 'invalid_scenario');
  need(Array.isArray(scenario.render.controls) && scenario.render.controls.length > 0 && scenario.render.controls.length <= 64, 'invalid_scenario');
  scenario.render.controls.forEach(v => text(v, 512, 'invalid_scenario'));
  const business = scenario.business;
  exact(business, ['input', 'submit', 'mutationMethod', 'beforePath', 'mutationPath', 'readbackPath', 'selectors'], 'invalid_scenario');
  text(business.input, 512, 'invalid_scenario'); text(business.submit, 512, 'invalid_scenario');
  need(['POST', 'PUT', 'PATCH'].includes(business.mutationMethod), 'invalid_scenario');
  for (const key of ['beforePath', 'mutationPath', 'readbackPath']) relative(business[key], 'invalid_scenario');
  const selectors = business.selectors;
  exact(selectors, ['before_records', 'before_success', 'mutation_record', 'readback_record', 'success', 'id_field', 'value_field'], 'invalid_scenario');
  for (const key of ['before_records', 'mutation_record', 'readback_record', 'success']) {
    need(Array.isArray(selectors[key]) && selectors[key].length <= 16 && (key === 'before_records' || selectors[key].length > 0), 'invalid_scenario');
    for (const member of selectors[key]) { text(member, 128, 'invalid_scenario'); need(!['__proto__', 'constructor', 'prototype'].includes(member), 'invalid_scenario'); }
  }
  if (selectors.before_success === null) need(selectors.before_records.length === 0, 'invalid_scenario');
  else {
    need(Array.isArray(selectors.before_success) && selectors.before_success.length > 0 && selectors.before_success.length <= 16
      && selectors.before_records.length > 0, 'invalid_scenario');
    for (const key of selectors.before_success) { text(key, 128, 'invalid_scenario'); need(!['__proto__', 'constructor', 'prototype'].includes(key), 'invalid_scenario'); }
  }
  for (const key of ['id_field', 'value_field']) text(selectors[key], 128, 'invalid_scenario');
  need(selectors.id_field !== selectors.value_field, 'invalid_scenario');
  need(scenario.render.controls.includes(business.submit), 'invalid_scenario');
  const limits = { ...DEFAULT_LIMITS };
  for (const [key, value] of Object.entries(options.limits || {})) {
    need(Object.hasOwn(limits, key) && Number.isSafeInteger(value) && value >= (key === 'assets' ? 0 : 1) && value <= limits[key], 'invalid_limits'); limits[key] = value;
  }
  const env = options.env || process.env;
  const credentials = { username: env[scenario.login.usernameEnv], password: env[scenario.login.passwordEnv] };
  text(credentials.username, 512, 'missing_credentials'); text(credentials.password, 4096, 'missing_credentials');
  return { ...options, scope: structuredClone(scope), validatedManifest: structuredClone(validatedManifest), scenario: structuredClone(scenario),
    baseURL: origin.origin, evidenceRoot: path.resolve(evidenceRoot), credentials, limits, startupTimeout };
}
async function directory(target) {
  let cursor = path.parse(target).root;
  for (const part of target.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    let info;
    try { info = await fs.lstat(cursor); }
    catch (error) { if (error.code !== 'ENOENT') throw error; await fs.mkdir(cursor, { mode: 0o700 }); info = await fs.lstat(cursor); }
    need(info.isDirectory() && !info.isSymbolicLink(), 'evidence_directory_alias');
    const real = await fs.realpath(cursor);
    need(process.platform === 'win32' ? real.toLowerCase() === cursor.toLowerCase() : real === cursor, 'evidence_directory_alias');
  }
}
async function writer(root, directoryName, guard) {
  const dir = path.join(root, directoryName); await directory(dir);
  return async (name, value) => {
    relative(name, 'invalid_evidence_path');
    const bytes = Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(guard(value)));
    need(bytes.length <= 8 * 1024 * 1024, 'file_budget_exhausted');
    const target = path.join(dir, name); await directory(dir);
    await fs.writeFile(target, bytes, { flag: 'wx', mode: 0o600 });
    const before = await fs.lstat(target); const observed = await fs.readFile(target); const final = await fs.lstat(target);
    need(before.isFile() && !before.isSymbolicLink() && before.nlink === 1 && before.ino === final.ino
      && before.dev === final.dev && before.size === final.size && before.mtimeMs === final.mtimeMs && before.ctimeMs === final.ctimeMs
      && observed.equals(bytes), 'evidence_file_changed');
    await directory(dir);
    return { path: `${directoryName}/${name}`, size_bytes: bytes.length, sha256: sha(bytes) };
  };
}
async function responseJSON(response, limits) {
  const length = response.headers()['content-length'];
  need(!length || Number(length) <= limits.jsonBytes, 'json_budget_exhausted');
  return strictJSON(await response.body(), limits.jsonBytes);
}
function appBody(envelope, limits) {
  exact(envelope, ['status_code', 'headers', 'body_base64'], 'invalid_proxy_response');
  need(Number.isInteger(envelope.status_code) && envelope.status_code >= 200 && envelope.status_code < 300, 'app_http_failed');
  need(Array.isArray(envelope.headers) && envelope.headers.length <= 64 && envelope.headers.every(p => Array.isArray(p) && p.length === 2 && p.every(v => typeof v === 'string')), 'invalid_proxy_response');
  text(envelope.body_base64, Math.ceil(limits.jsonBytes / 3) * 4, 'json_budget_exhausted');
  const bytes = Buffer.from(envelope.body_base64, 'base64');
  need(bytes.toString('base64') === envelope.body_base64, 'invalid_proxy_response');
  return { status: envelope.status_code, body: strictJSON(bytes, limits.jsonBytes) };
}
function boundedObservers(context, origin, limits, secrets) {
  let prefix = null; let fatal = null; let byteCount = 0;
  const assets = new Map(); const pending = new Set(); const consoleErrors = []; const pageErrors = [];
  const fail = code => { fatal ||= code; };
  context.on('serviceworker', () => fail('unsupported_service_worker'));
  const run = task => { const promise = task.catch(error => fail(error instanceof CollectorError ? error.code : 'asset_observation_failed')); pending.add(promise); promise.finally(() => pending.delete(promise)); };
  const contentPrefix = request => {
    const url = new URL(request.url());
    const match = url.pathname.match(/^\/api\/v1\/web-previews\/[0-9a-f-]{36}\/content\//);
    return url.origin === origin && match ? `${origin}${match[0]}` : null;
  };
  const relevant = request => contentPrefix(request) && ['stylesheet', 'script', 'image', 'font', 'media', 'other'].includes(request.resourceType());
  context.on('request', request => {
    if (!relevant(request)) return;
    if (assets.size >= limits.assets) { fail('asset_budget_exhausted'); return; }
    try {
      const url = new URL(request.url());
      need(!prefix || contentPrefix(request) === prefix, 'foreign_preview_asset');
      const member = url.pathname.slice(new URL(contentPrefix(request)).pathname.length);
      relative(member, 'unsafe_asset_path');
      need(!url.search, 'unbound_asset_query');
      assets.set(request, { path: member, resource_type: request.resourceType(), status: null, loaded: false });
    } catch (error) { fail(error.code || 'unsafe_asset_path'); }
  });
  context.on('response', response => {
    const request = response.request();
    if (!assets.has(request)) return;
    assets.get(request).status = response.status();
    run((async () => {
      need((response.status() >= 200 && response.status() < 300) || response.status() === 304, 'asset_http_failed');
      const length = response.headers()['content-length'];
      need(!length || Number(length) <= limits.assetBytes, 'asset_budget_exhausted');
      const bytes = await response.body(); byteCount += bytes.length;
      need(bytes.length <= limits.assetBytes && byteCount <= limits.totalAssetBytes, 'asset_budget_exhausted');
    })());
  });
  context.on('requestfinished', request => { if (assets.has(request)) assets.get(request).loaded = true; });
  context.on('requestfailed', request => { if (relevant(request)) fail('asset_request_failed'); });
  context.on('page', page => {
    page.on('console', message => {
      if (message.type() !== 'error') return;
      if (consoleErrors.length >= limits.errors) { fail('error_budget_exhausted'); return; }
      const label = ['401', '403', '404', '500', 'cors', 'sandbox', 'refused', 'unsafe', 'referenceerror', 'typeerror'].find(word => message.text().toLowerCase().includes(word)) || 'other';
      consoleErrors.push(`console_${label}`);
    });
    page.on('pageerror', error => {
      if (pageErrors.length >= limits.errors) fail('error_budget_exhausted');
      else pageErrors.push(['TypeError', 'ReferenceError', 'SyntaxError'].includes(error.name) ? error.name : 'Error');
    });
  });
  return {
    bind(url) {
      const content = new URL(url); prefix = `${content.origin}${content.pathname}`;
      for (const request of assets.keys()) if (contentPrefix(request) !== prefix) fail('foreign_preview_asset');
    },
    async install() {
      await context.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.origin !== origin || url.username || url.password) { fail('external_request_blocked'); return route.abort(); }
        return route.continue();
      });
      if (typeof context.routeWebSocket === 'function') await context.routeWebSocket('**/*', socket => { fail('unsupported_websocket'); socket.close(); });
    },
    async snapshot() {
      await Promise.all([...pending]); need(!fatal, fatal);
      need(consoleErrors.length === 0, `browser_errors_${[...new Set(consoleErrors)].join('_')}`);
      need(pageErrors.length === 0, `browser_errors_${[...new Set(pageErrors)].join('_')}`);
      for (const asset of assets.values()) need(asset.loaded && ((asset.status >= 200 && asset.status < 300) || asset.status === 304), 'asset_load_incomplete');
      const result = { assets: [...assets.values()], console_errors: consoleErrors, page_errors: pageErrors };
      secretsGuard(secrets)(result); return result;
    },
  };
}
async function pixels(page, png, expected) {
  const result = await page.evaluate(async encoded => {
    const image = new Image();
    await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; image.src = `data:image/png;base64,${encoded}`; });
    const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
    const bytes = context.getImageData(0, 0, image.width, image.height).data;
    let sum = 0; let squared = 0; let nonWhite = 0;
    for (let i = 0; i < bytes.length; i += 4) {
      const alpha = bytes[i + 3] / 255;
      const value = ((bytes[i] + bytes[i + 1] + bytes[i + 2]) / 3) * alpha + 255 * (1 - alpha);
      sum += value; squared += value * value; if (value < 245) nonWhite++;
    }
    const count = bytes.length / 4;
    return { width: image.width, height: image.height, deviation: Math.sqrt(Math.max(0, squared / count - (sum / count) ** 2)), nonWhite: nonWhite / count };
  }, png.toString('base64'));
  need(result.width === expected.width && result.height === expected.height && result.deviation > 0.5 && result.nonWhite > 0.001, 'blank_or_invalid_png');
}
function inside(bounds, viewport) {
  need(bounds && ['x', 'y', 'width', 'height'].every(k => Number.isFinite(bounds[k])) && bounds.width > 0 && bounds.height > 0
    && bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= viewport.width && bounds.y + bounds.height <= viewport.height, 'clipped_render');
}
async function render(page, element, scenario, viewport, scope, previewIdentity, urlHash, observation, guard, limits) {
  const handle = await element.elementHandle(); const frame = await handle.contentFrame(); need(frame, 'preview_frame_missing');
  await frame.locator(scenario.preview.ready).waitFor({ state: 'visible' });
  await frame.waitForLoadState('networkidle');
  const bounds = await element.boundingBox(); inside(bounds, viewport);
  const visible_controls = [];
  for (const selector of scenario.render.controls) {
    const control = frame.locator(selector); need(await control.count() === 1 && await control.isVisible(), 'semantic_control_missing');
    const controlBounds = await control.boundingBox(); inside(controlBounds, viewport);
    need(controlBounds.x >= bounds.x && controlBounds.y >= bounds.y && controlBounds.x + controlBounds.width <= bounds.x + bounds.width
      && controlBounds.y + controlBounds.height <= bounds.y + bounds.height, 'clipped_render');
    const label = await control.evaluate(el => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim());
    visible_controls.push({ selector, text: text(label, 512, 'semantic_control_missing'), bounds: controlBounds });
  }
  const layout = await frame.evaluate(() => ({ text: document.body.innerText.trim(), width: innerWidth, height: innerHeight,
    scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight,
    imagesLoaded: [...document.images].every(img => img.complete && img.naturalWidth > 0), fontsLoaded: document.fonts.status === 'loaded' }));
  need(layout.scrollWidth <= layout.width && layout.scrollHeight <= layout.height, 'overflow_render');
  need(layout.imagesLoaded && layout.fontsLoaded, 'asset_load_incomplete');
  const title = text(await frame.title(), 512, 'blank_render');
  const content_text = text(layout.text, limits.text, 'blank_render');
  guard({ title, content_text, visible_controls });
  guard(await page.locator('body').innerText());
  const viewportPng = await page.screenshot({ type: 'png', fullPage: false, animations: 'disabled' });
  const clip = { x: Math.floor(bounds.x), y: Math.floor(bounds.y),
    width: Math.ceil(bounds.x + bounds.width) - Math.floor(bounds.x),
    height: Math.ceil(bounds.y + bounds.height) - Math.floor(bounds.y) };
  const framePng = await page.screenshot({ type: 'png', clip, animations: 'disabled' });
  const dimensions = { width: framePng.readUInt32BE(16), height: framePng.readUInt32BE(20) };
  need(clip.width === dimensions.width && clip.height === dimensions.height, 'frame_pixel_geometry_mismatch');
  await pixels(page, viewportPng, viewport); await pixels(page, framePng, dimensions);
  const observations = await observation.snapshot();
  return { frame, viewportPng, framePng, value: { schema_version: 1, scope, preview_identity: previewIdentity,
    device: null, viewport, observed_at: utc(), content_url_sha256: urlHash, title, content_text,
    visible_controls, frame: { bounds, ...dimensions }, ...observations } };
}

async function collectDevice(browser, options, device, collectionDirectory) {
  const { scenario, scope, validatedManifest, baseURL, credentials, limits } = options;
  const viewport = VIEWPORTS[device];
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, acceptDownloads: false });
  context.setDefaultTimeout(limits.timeout); context.setDefaultNavigationTimeout(limits.timeout);
  const secrets = [credentials.username, credentials.password]; const guard = secretsGuard(secrets);
  const write = await writer(options.evidenceRoot, `${collectionDirectory}/${device}`, guard);
  const observation = boundedObservers(context, baseURL, limits, secrets);
  let previewIdentity = null; let cleanupIdentity = null; let contentURL = null; let auth = null; let lastObservation = utc();
  let ownedPreviewId = null; let oldCookieHeader = ''; let startPending = null; let startIssued = false;
  let collected = null; let failure = null; let cleanupFailure = null; let cleanupFile = null; let stage = 'browser_setup';
  const api = async (method, target, body) => {
    const response = await context.request.fetch(`${baseURL}${target}`, { method, headers: { Authorization: `Bearer ${auth}`,
      'Cache-Control': 'no-store', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { data: body } : {}), maxRedirects: 0, timeout: limits.timeout });
    return response;
  };
  try {
    await observation.install();
    let page = await context.newPage();
    stage = 'login_navigation'; await page.goto(`${baseURL}${scenario.login.path}`);
    const loginResponse = page.waitForResponse(r => r.url() === `${baseURL}/api/v1/auth/login` && r.request().method() === 'POST');
    loginResponse.catch(() => {});
    stage = 'login_form'; await page.locator(scenario.login.username).fill(credentials.username);
    await page.locator(scenario.login.password).fill(credentials.password);
    stage = 'login_submit'; await page.locator(scenario.login.submit).click();
    stage = 'login_response'; need((await loginResponse).ok(), 'login_failed');
    await page.locator(scenario.login.ready).waitFor({ state: 'visible' });
    // The real UI stores its token in this ephemeral browser's RAM session only.
    auth = await page.evaluate(() => sessionStorage.getItem('agent_hub_access_token'));
    text(auth, 8192, 'login_failed'); secrets.push(auth);
    const currentUser = await api('GET', '/api/v1/auth/me'); need(currentUser.ok(), 'login_failed');
    const principal = await responseJSON(currentUser, limits);
    need(isObject(principal) && ['tenant_id', 'user_id'].every(k => principal[k] === scope.execution_identity[k]), 'login_owner_mismatch');
    stage = 'product_navigation';
    for (const step of scenario.navigation.slice(0, -1)) await page.locator(step.selector).click();
    page.on('request', request => {
      if (request.url() === `${baseURL}/api/v1/web-previews/start` && request.method() === 'POST') startIssued = true;
    });
    startPending = page.waitForResponse(r => r.url() === `${baseURL}/api/v1/web-previews/start` && r.request().method() === 'POST',
      { timeout: options.startupTimeout }).then(async started => {
      need(started.ok(), 'preview_start_failed');
      const preview = await responseJSON(started, limits);
      need(UUID.test(preview.id), 'invalid_preview');
      need(isObject(preview.identity) && preview.identity.preview_id === preview.id
        && ['tenant_id', 'user_id'].every(key => preview.identity[key] === principal[key]), 'identity_owner_mismatch');
      // Cleanup is bound to this owner's created preview before acceptance scope checks.
      ownedPreviewId = preview.id;
      const knownPrefix = `/api/v1/web-previews/${preview.id}/content/`;
      need(typeof preview.preview_url === 'string' && [knownPrefix, `${baseURL}${knownPrefix}`].some(prefix =>
        preview.preview_url === prefix || preview.preview_url.startsWith(`${prefix}?`)), 'invalid_content_url');
      const candidateURL = new URL(preview.preview_url, baseURL);
      need(candidateURL.origin === baseURL && candidateURL.pathname === `/api/v1/web-previews/${preview.id}/content/`
        && !candidateURL.username && !candidateURL.password && !candidateURL.hash, 'invalid_content_url');
      contentURL = candidateURL.href;
      secrets.push(contentURL); for (const value of candidateURL.searchParams.values()) secrets.push(value);
      const cookies = await context.cookies(contentURL); cookies.forEach(cookie => secrets.push(cookie.value));
      oldCookieHeader = cookies.map(cookie => `${cookie.name}=${cookie.value}`).join('; ');
      const createdScope = { ...scope };
      for (const key of ['project_id', 'conversation_id', 'workspace_session_id']) createdScope[key] = text(preview.identity[key], 128, 'invalid_identity');
      cleanupIdentity = identity(preview.identity, createdScope, principal);
      const request = strictJSON(started.request().postDataBuffer(), limits.jsonBytes);
      need(['project_id', 'conversation_id', 'workspace_session_id'].every(k => request[k] === scope[k]), 'start_scope_mismatch');
      previewIdentity = identity(preview.identity, scope, principal);
      need(preview.id === previewIdentity.preview_id && preview.status === 'ready', 'invalid_preview');
      observation.bind(contentURL); return preview;
    });
    startPending.catch(() => {});
    stage = 'preview_start'; await page.locator(scenario.navigation.at(-1).selector).click();
    const preview = await startPending;
    if (scenario.preview.open) {
      const open = scenario.preview.open;
      if (open.mode === 'popup') { const opened = context.waitForEvent('page'); await page.locator(open.selector).click(); page = await opened; }
      else await page.locator(open.selector).click();
    }
    stage = 'preview_frame'; const element = page.locator(scenario.preview.frame); await element.waitFor({ state: 'visible' });
    need(await element.count() === 1 && await element.evaluate(el => el.tagName === 'IFRAME'), 'preview_frame_missing');
    need(await element.getAttribute('sandbox') !== null && !(await element.getAttribute('sandbox')).split(/\s+/).includes('allow-same-origin'), 'unsafe_preview_frame');
    need(await element.evaluate(el => el.src) === contentURL, 'content_url_mismatch');
    for (const cookie of await context.cookies()) secrets.push(cookie.value);
    stage = 'provenance_get'; const provenanceResponse = await api('GET', `/api/v1/web-previews/${preview.id}/provenance`);
    need(provenanceResponse.ok() && /(?:^|,)\s*no-store(?:,|$)/i.test(provenanceResponse.headers()['cache-control'] || ''), 'provenance_unavailable');
    const provenance = await responseJSON(provenanceResponse, limits); validateProvenance(provenance, previewIdentity, validatedManifest); guard(provenance);
    need(previewIdentity.kind === 'dynamic' && preview.application_transport === true, 'static_business_ineligible');
    const captured_at = utc();
    stage = 'render_capture'; const rendered = await render(page, element, scenario, viewport, scope, previewIdentity, sha(contentURL), observation, guard, limits);
    rendered.value.device = device;
    if (options.onPreviewReady) {
      stage = 'preview_ready_hook';
      try { await options.onPreviewReady({ device, previewIdentity: structuredClone(previewIdentity) }); }
      catch { throw new CollectorError('preview_ready_hook_failed'); }
    }
    const nonce = randomUUID(); const unique_value = `r7b-${nonce}`;
    const business = scenario.business;
    async function fresh(role, appPath) {
      const target = `${appPath}?evidence_nonce=${role === 'before' ? nonce : randomUUID()}`;
      const response = await api('POST', `/api/v1/web-previews/${preview.id}/app-request`, {
        method: 'GET', target: `/${target}`, headers: [['accept', 'application/json']], body_base64: '' });
      need(response.ok(), 'proxy_http_failed');
      const decoded = appBody(await responseJSON(response, limits), limits);
      guard(decoded.body);
      return { method: 'GET', path: appPath, observed_at: utc(), status: decoded.status,
        body: minimalBody(decoded.body, business.selectors, role, limits) };
    }
    stage = 'business_before'; const before = await fresh('before', business.beforePath);
    const records = select(before.body, business.selectors.before_records);
    const pendingValues = [...records];
    while (pendingValues.length) {
      const value = pendingValues.pop();
      if (typeof value === 'string') need(!value.includes(unique_value) && !value.includes(nonce), 'preexisting_business_value');
      else if (Array.isArray(value)) pendingValues.push(...value);
      else if (isObject(value)) for (const [key, child] of Object.entries(value)) pendingValues.push(key, child);
    }
    const input = rendered.frame.locator(business.input); const submit = rendered.frame.locator(business.submit);
    need(await input.count() === 1 && await input.isVisible() && await submit.count() === 1 && await submit.isVisible(), 'mutation_control_missing');
    stage = 'business_mutation'; await input.fill(unique_value); await after(before.observed_at);
    const mutationResponse = page.waitForResponse(response => {
      if (response.url() !== `${baseURL}/api/v1/web-previews/${preview.id}/app-request` || response.request().method() !== 'POST') return false;
      try { const payload = strictJSON(response.request().postDataBuffer(), limits.jsonBytes); return payload.method === business.mutationMethod && payload.target === `/${business.mutationPath}`; } catch { return false; }
    });
    await submit.click(); const changed = await mutationResponse; need(changed.ok(), 'proxy_http_failed');
    const sent = strictJSON(changed.request().postDataBuffer(), limits.jsonBytes);
    const sentBody = strictJSON(Buffer.from(sent.body_base64, 'base64'), limits.jsonBytes);
    need(select(sentBody, [business.selectors.value_field]) === unique_value, 'ui_mutation_input_mismatch');
    const decoded = appBody(await responseJSON(changed, limits), limits); guard(decoded.body);
    const mutation = { method: business.mutationMethod, path: business.mutationPath, observed_at: utc(), status: decoded.status,
      body: minimalBody(decoded.body, business.selectors, 'mutation', limits), input_value: unique_value,
      control: { selector: business.submit, visible: true } };
    need(select(mutation.body, business.selectors.success) === true, 'application_status_failed');
    const record = select(mutation.body, business.selectors.mutation_record);
    const recordId = record[business.selectors.id_field];
    need(isObject(record) && validRecordId(recordId)
      && record[business.selectors.value_field] === unique_value, 'mutation_record_mismatch');
    await after(mutation.observed_at);
    stage = 'application_frame_reload'; await element.evaluate(el => new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('frame_reload_timeout')), 12000);
      el.addEventListener('load', () => { clearTimeout(timeout); resolve(); }, { once: true }); el.src = el.src;
    }));
    const refreshed = await (await element.elementHandle()).contentFrame();
    await refreshed.locator(scenario.preview.ready).waitFor({ state: 'visible' }); await refreshed.waitForLoadState('networkidle');
    stage = 'business_readback'; const readback = { ...await fresh('readback', business.readbackPath), cache: 'no-store', after_reload: true };
    lastObservation = readback.observed_at;
    need(select(readback.body, business.selectors.success) === true, 'application_status_failed');
    const observedRecord = select(readback.body, business.selectors.readback_record);
    need(observedRecord[business.selectors.id_field] === record[business.selectors.id_field]
      && observedRecord[business.selectors.value_field] === unique_value, 'fresh_record_mismatch');
    const finalRender = await observation.snapshot();
    rendered.value.assets = finalRender.assets;
    collected = { captured_at, provenance, rendered, business: { schema_version: 1, scope, preview_identity: previewIdentity,
      unique_value, selectors: business.selectors, before, mutation, readback } };
  } catch (error) { failure = error instanceof CollectorError ? error.code : `${stage}_failed`; }
  finally {
    if (startPending) { try { await startPending; } catch {} }
    if (startIssued && (!ownedPreviewId || !contentURL)) cleanupFailure ||= 'cleanup_unresolved';
    if (ownedPreviewId && auth) {
      let record = null; let revocation = null;
      try { await after(lastObservation); const stopped = await api('DELETE', `/api/v1/web-previews/${ownedPreviewId}`); need(stopped.ok(), 'owner_stop_failed'); }
      catch (error) { cleanupFailure ||= error.code || 'owner_stop_failed'; }
      try {
        const response = await api('GET', `/api/v1/web-previews/${ownedPreviewId}/cleanup`);
        need(response.ok() && /(?:^|,)\s*no-store(?:,|$)/i.test(response.headers()['cache-control'] || ''), 'cleanup_unavailable');
        record = await responseJSON(response, limits); guard(record); need(cleanupIdentity, 'cleanup_identity_unresolved'); validateCleanup(record, cleanupIdentity, lastObservation);
      } catch (error) { cleanupFailure ||= error.code || 'cleanup_unavailable'; }
      try {
        need(contentURL, 'old_url_unavailable');
        const response = await context.request.get(contentURL, { headers: { 'Cache-Control': 'no-store', ...(oldCookieHeader ? { Cookie: oldCookieHeader } : {}) }, maxRedirects: 0, timeout: limits.timeout });
        revocation = { method: 'GET', content_url_sha256: sha(contentURL), observed_at: utc(), status: response.status() };
        need(revocation.status === 404, 'old_url_not_revoked');
      } catch (error) { cleanupFailure ||= error.code || 'revocation_unavailable'; }
      try { cleanupFile = await write('cleanup.json', { schema_version: 1, scope, preview_identity: cleanupIdentity, cleanup_record: record, revocation }); }
      catch (error) { cleanupFailure ||= error.code || 'cleanup_persistence_failed'; }
    }
    try { await context.close(); } catch { cleanupFailure ||= 'browser_close_failed'; }
  }
  if (failure || cleanupFailure || !collected) {
    const detail = { device, error_code: failure, cleanup_error_code: cleanupFailure, cleanup_file: cleanupFile };
    await write('failure.json', { schema_version: 1, scope, ...detail });
    throw new CollectorError('device_collection_failed', detail);
  }
  const files = {
    viewport_png: await write('viewport.png', collected.rendered.viewportPng),
    frame_png: await write('frame.png', collected.rendered.framePng),
    render: await write('render.json', collected.rendered.value), business: await write('business.json', collected.business),
    provenance: await write('provenance.json', collected.provenance), cleanup: cleanupFile,
  };
  const bundle_file = await write('bundle.json', { schema_version: 2, scope, device, viewport,
    preview_identity: previewIdentity, captured_at: collected.captured_at, files });
  return { schema_version: 2, passed: true, observed_at: utc(), evidence_ref: bundle_file.path,
    checks: { preview_rendered: true, preview_interaction: true, preview_revoked: true }, bundle_file };
}

async function collectCaseBrowserEvidence(input) {
  const options = validateInput(input); await directory(options.evidenceRoot);
  const collectionDirectory = `case-browser-${randomUUID()}`;
  const browser = await chromium.launch({ headless: true });
  const descriptors = {}; const failures = [];
  try {
    for (const device of Object.keys(VIEWPORTS)) {
      try { descriptors[device] = await collectDevice(browser, options, device, collectionDirectory); }
      catch (error) { failures.push({ device, error_code: error.error_code || error.code || 'device_collection_failed',
        cleanup_error_code: error.cleanup_error_code || null, cleanup_file: error.cleanup_file || null }); }
    }
  } finally { await browser.close(); }
  if (failures.length) throw new CollectorError('collection_failed', { failures });
  return descriptors;
}
async function main(args) {
  if (args.length === 1 && args[0] === '--help') {
    process.stdout.write('Usage: node collect-case-browser-evidence.cjs --base-url ORIGIN --scope SCOPE_JSON --scenario SCENARIO_JSON --evidence-root ABSOLUTE_DIRECTORY\n'); return;
  }
  const flags = Object.create(null);
  for (let i = 0; i < args.length; i += 2) {
    need(['--base-url', '--scope', '--scenario', '--evidence-root'].includes(args[i]) && args[i + 1] && !Object.hasOwn(flags, args[i]), 'invalid_cli'); flags[args[i]] = args[i + 1];
  }
  need(Object.keys(flags).length === 4, 'invalid_cli');
  const scopeInput = strictJSON(await fs.readFile(flags['--scope']), DEFAULT_LIMITS.jsonBytes);
  exact(scopeInput, ['scope', 'validated_manifest'], 'invalid_scope');
  const scenario = strictJSON(await fs.readFile(flags['--scenario']), DEFAULT_LIMITS.jsonBytes);
  const result = await collectCaseBrowserEvidence({ baseURL: flags['--base-url'], scope: scopeInput.scope,
    validatedManifest: scopeInput.validated_manifest, scenario, evidenceRoot: flags['--evidence-root'] });
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
if (require.main === module) main(process.argv.slice(2)).catch(error => {
  process.stderr.write(`${JSON.stringify({ code: error instanceof CollectorError ? error.code : 'collector_failed', ...(error.failures ? { failures: error.failures } : {}) })}\n`);
  process.exitCode = 1;
});
module.exports = { collectCaseBrowserEvidence, CollectorError };
