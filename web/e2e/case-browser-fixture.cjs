'use strict';

// An owned operator contract fixture, never a deployed application acceptance claim.
const http = require('node:http');
const { randomUUID, createHash } = require('node:crypto');

const sha = value => createHash('sha256').update(value).digest('hex');
const utc = () => new Date().toISOString();
const resources = ['attachment', 'unit_build', 'unit_install', 'unit_probe', 'unit_start',
  'cgroup_build', 'cgroup_install', 'cgroup_probe', 'cgroup_start', 'mount_unit', 'work_path',
  'owned_directory', 'capability', 'snapshot'];

async function startCaseBrowserFixture({ mode = 'success' } = {}) {
  const scope = { execution_identity: { tenant_id: randomUUID(), user_id: randomUUID(),
    execution_id: randomUUID(), base_url: '' }, case_id: 'operator-contract-case', project_id: 'fixture-project',
  conversation_id: 'fixture-conversation', run_id: 'fixture-run', workspace_session_id: 'fixture-workspace' };
  const token = `ram-auth-${randomUUID()}`;
  const capability = `ram-cap-${randomUUID()}`;
  const env = { CASE_BROWSER_USERNAME: 'operator-fixture-user', CASE_BROWSER_PASSWORD: `password-${randomUUID()}` };
  const scenario = {
    login: { path: '/login', username: '[aria-label="Username"]', password: '[aria-label="Password"]',
      submit: 'button[type="submit"]', ready: '#projects', usernameEnv: 'CASE_BROWSER_USERNAME', passwordEnv: 'CASE_BROWSER_PASSWORD' },
    navigation: [ { kind: 'project', selector: '#projects' }, { kind: 'history', selector: '#history' },
      { kind: 'conversation', selector: '#conversation' }, { kind: 'preview', selector: '#preview' } ],
    preview: { frame: '#website', ready: '#records-title' },
    render: { controls: ['#records-title', '#record-value', '#save-record'] },
    business: { input: '#record-value', submit: '#save-record', mutationMethod: 'POST',
      beforePath: 'api/records', mutationPath: 'api/records', readbackPath: 'api/record',
      selectors: { before_records: ['records'], before_success: ['success'], mutation_record: ['record'], readback_record: ['record'],
        success: ['success'], id_field: 'id', value_field: 'value' } },
  };
  if (mode === 'root-array') { scenario.business.selectors.before_records = []; scenario.business.selectors.before_success = null; }
  const state = { previews: [], events: [], records: [], revokedRequests: [], revokedCookies: [], mutations: [], externalRequests: 0 };
  const style = 'body{background:#eef4f0;color:#17251c}';
  const source = mode === 'blank' ? '<!doctype html><title>Blank</title><body></body>' : `<!doctype html><title>Owned records fixture</title>${mode === 'self-contained' ? '' : '<link rel="stylesheet" href="style.css">'}<style>body{margin:16px;font:16px Arial}input{width:220px;max-width:80%;padding:6px}button{padding:6px}h1{font-size:24px}
    ${mode === 'invisible' ? 'body{background:white}h1,input,button{opacity:0}' : ''}${mode === 'clipped' ? '#save-record{transform:translateX(2000px)}' : ''}${mode === 'overflow' ? 'body{min-width:2000px}' : ''}</style>
    <h1 id="records-title">Records</h1><input id="record-value" aria-label="Record value"><button id="save-record">Save record</button><div id="result"></div>
    ${mode === 'missing-image' ? '<img src="missing.png">' : ''}${mode === 'external-asset' ? '<img src="https://external.invalid/image.png">' : ''}<script>
    let seq=0;const pending=new Map();window.addEventListener('message',e=>{if(e.source!==parent||e.data.kind!=='fixture-response')return;pending.get(e.data.id)?.(e.data.body);pending.delete(e.data.id)});
    async function app(method,target,body){const id=++seq;const p=new Promise(r=>pending.set(id,r));parent.postMessage({kind:'fixture-request',id,payload:{method,target,headers:[['content-type','application/json']],body_base64:body?btoa(JSON.stringify(body)):''}},'*');const b=await p;return JSON.parse(atob(b.body_base64))}
    document.querySelector('#save-record').onclick=async()=>{const value=document.querySelector('#record-value').value;const b=await app('POST','/api/records',{value});document.querySelector('#result').textContent=b.record?.value||'Failed'};
    ${mode === 'console-error' ? "console.error('owned app error');" : ''}${mode === 'page-error' ? "throw new Error('owned crash');" : ''}
    </script>`;
  const validatedManifest = { 'index.html': [Buffer.byteLength(source), sha(source)],
    ...(mode === 'self-contained' ? {} : { 'style.css': [Buffer.byteLength(style), sha(style)] }) };
  const manifestHash = sha(JSON.stringify(validatedManifest));
  const manifestBytes = Object.values(validatedManifest).reduce((sum, entry) => sum + entry[0], 0);

  const json = (res, body, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(body)); };
  const html = (res, text) => { res.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store' }); res.end(text); };
  const read = async req => { const chunks = []; for await (const chunk of req) chunks.push(chunk); return chunks.length ? JSON.parse(Buffer.concat(chunks)) : {}; };
  const auth = req => req.headers.authorization === `Bearer ${token}`;
  function cleanup(identity, requested) {
    let at = mode === 'cleanup-equality' ? state.cleanupTime : utc();
    if (mode === 'cleanup-equality') requested = at;
    let observed = at;
    if (mode === 'microsecond-reversed-cleanup') {
      requested = at.replace(/Z$/, '500Z'); observed = at.replace(/Z$/, '600Z'); at = at.replace(/Z$/, '400Z');
    }
    const covered = identity.kind === 'static' ? ['serving_thread', 'listener', 'accepted_threads', 'accepted_sockets', 'proxy_operations', 'proxy_sockets', 'capability', 'snapshot'] : resources;
    const observations = covered.map(resource => {
      const unit = resource.startsWith('unit_') || resource === 'mount_unit';
      const result = unit ? 'inactive' : resource.startsWith('cgroup_') ? 'empty'
        : ['attachment', 'serving_thread'].includes(resource) ? 'exited' : resource === 'capability' ? 'revoked'
          : ['listener', 'accepted_sockets', 'proxy_sockets'].includes(resource) ? 'closed'
            : ['accepted_threads', 'proxy_operations'].includes(resource) ? 'drained' : 'absent';
      return { resource, observer: identity.kind === 'dynamic' && resources.indexOf(resource) < 12 ? 'broker' : 'manager', observed_at: at,
        result, reason_code: 'observed', load_state: unit ? 'not-found' : null,
        active_state: unit ? 'inactive' : null, main_pid: resource.startsWith('unit_') ? 0 : null,
        identity_match: unit ? true : null };
    });
    if (mode === 'unknown-cleanup') { observations[0].result = 'unknown'; observations[0].reason_code = 'observation_failed'; }
    if (mode === 'contradictory-cleanup') observations[0].result = 'present';
    if (mode === 'stale-cleanup') observations[0].observed_at = '2000-01-01T00:00:00.000Z';
    const receiptIdentity = mode === 'foreign-cleanup' ? { ...identity, preview_id: randomUUID() } : identity;
    return { identity, cleanup_receipt: { schema_version: 1, identity: receiptIdentity,
      observation_id: randomUUID(), requested_at: requested, observed_at: observed, reason: 'explicit',
      status: mode === 'unknown-cleanup' ? 'unknown' : 'confirmed', coverage: identity.kind === 'dynamic' ? 'dynamic-systemd-tmpfs-v1' : 'static-loopback-v1',
      observations, unobserved: identity.kind === 'dynamic' ? ['private_network_namespace', 'private_port', 'other_mount_namespaces'] : [] },
    retention_expires_at: new Date(Date.now() + 60000).toISOString() };
  }
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://fixture.invalid');
      if (url.pathname === '/favicon.ico') { res.writeHead(204); return res.end(); }
      if (url.pathname === '/login') return html(res, `<!doctype html><title>Operator Login</title><form><input aria-label="Username"><input aria-label="Password" type="password"><button type="submit">Login</button></form><script>
        document.querySelector('form').onsubmit=async e=>{e.preventDefault();const r=await fetch('/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.querySelector('[aria-label="Username"]').value,password:document.querySelector('[aria-label="Password"]').value})});const b=await r.json();if(r.ok){sessionStorage.setItem('agent_hub_access_token',b.access_token);location.href='/';}};</script>`);
      if (url.pathname === '/api/v1/auth/login') {
        const body = await read(req);
        if (body.username !== env.CASE_BROWSER_USERNAME || body.password !== env.CASE_BROWSER_PASSWORD) return json(res, {}, 401);
        state.events.push('login');
        return json(res, { access_token: token, token_type: 'bearer', principal: scope.execution_identity });
      }
      if (url.pathname === '/') return html(res, `<!doctype html><title>Owned operator workbench</title><style>body{margin:12px;font:16px Arial}button{padding:8px}iframe{display:block;border:0;width:100%;height:620px;margin-top:8px}#website[hidden]{display:none}${mode === 'fractional-frame' ? 'iframe{margin-left:.75px;width:calc(100% - .5px);height:620.25px}' : ''}</style>
        <button id="projects">Fixture project</button><button id="history" hidden>History</button><button id="conversation" hidden>Fixture conversation</button><button id="preview" hidden>Website preview</button><iframe id="website" sandbox="allow-scripts allow-forms" hidden></iframe><script>
        const auth=()=>({'Authorization':'Bearer '+sessionStorage.getItem('agent_hub_access_token'),'Content-Type':'application/json'});
        projects.onclick=()=>{historyButton.hidden=false;fetch('/operator/project',{headers:auth()})};const historyButton=document.querySelector('#history');
        historyButton.onclick=()=>{conversation.hidden=false;fetch('/operator/history',{headers:auth()})};conversation.onclick=()=>{preview.hidden=false;fetch('/operator/conversation',{headers:auth()})};
        preview.onclick=async()=>{const r=await fetch('/api/v1/web-previews/start',{method:'POST',headers:auth(),body:JSON.stringify(${JSON.stringify({ project_id: scope.project_id, conversation_id: scope.conversation_id, workspace_session_id: scope.workspace_session_id })})});const b=await r.json();website.hidden=false;website.src=b.preview_url;window.previewId=b.id;};
        window.addEventListener('message',async e=>{if(e.source!==website.contentWindow||e.origin!=='null'||e.data.kind!=='fixture-request')return;const r=await fetch('/api/v1/web-previews/'+window.previewId+'/app-request',{method:'POST',headers:auth(),body:JSON.stringify(e.data.payload)});const b=await r.json();website.contentWindow.postMessage({kind:'fixture-response',id:e.data.id,body:b},'*');});</script>`);
      if (!auth(req) && !url.pathname.includes('/content/')) return json(res, {}, 401);
      if (url.pathname === '/api/v1/auth/me') return json(res, { ...scope.execution_identity, username: env.CASE_BROWSER_USERNAME, role: 'user', permissions: ['run:create', 'run:read'] });
      if (url.pathname.startsWith('/operator/')) { state.events.push(url.pathname.split('/').pop()); return json(res, {}); }
      if (url.pathname === '/api/v1/web-previews/start') {
        const request = await read(req);
        const id = randomUUID();
        const identity = { preview_id: id, kind: mode === 'static' ? 'static' : 'dynamic', tenant_id: scope.execution_identity.tenant_id,
          user_id: scope.execution_identity.user_id, project_id: request.project_id, conversation_id: request.conversation_id,
          workspace_session_id: request.workspace_session_id, runtime_handle: mode === 'static' ? null : randomUUID().replaceAll('-', ''),
          source: { scheme: mode === 'static' ? 'preview-static-tree-v1' : 'preview-broker-tree-v2', sha256: sha(source) }, display_root: '.', display_entrypoint: 'index.html' };
        const preview = { id, identity, preview_url: `/api/v1/web-previews/${id}/content/?cap=${capability}`,
          status: 'ready', cleanup_url: `/api/v1/web-previews/${id}/cleanup`, cleanup_receipt: null, application_transport: true,
          lease_expires_at: new Date(Date.now() + 60000).toISOString() };
        state.previews.push({ ...preview, stopped: false, captured_at: utc() });
        state.events.push('preview');
        if (mode === 'slow-ready') await new Promise(resolve => setTimeout(resolve, 2500));
        res.setHeader('Set-Cookie', `fixture_cap=${capability}; Path=/api/v1/web-previews/${id}/content/; HttpOnly; SameSite=Strict`);
        const reply = mode === 'bad-identity' ? { ...preview, identity: { ...identity, source: { ...identity.source, sha256: 'bad' } } }
          : mode === 'foreign-owner-start' ? { ...preview, identity: { ...identity, user_id: randomUUID() } }
            : mode === 'invalid-start-id' ? { ...preview, id: 'invalid' }
              : mode === 'invalid-start-url' ? { ...preview, preview_url: 'https://foreign.invalid/preview/' } : preview;
        return json(res, reply, 201);
      }
      const match = url.pathname.match(/^\/api\/v1\/web-previews\/([^/]+)(.*)$/);
      const preview = match && state.previews.find(p => p.id === match[1]);
      if (!preview) return json(res, {}, 404);
      const suffix = match[2];
      if (suffix === '' && req.method === 'DELETE') {
        preview.stopped = true; preview.cleanup = cleanup(preview.identity, utc()); state.events.push('stop');
        res.setHeader('Set-Cookie', `fixture_cap=; Max-Age=0; Path=/api/v1/web-previews/${preview.id}/content/; HttpOnly; SameSite=Strict`);
        return json(res, mode === 'stop-error' ? { error: 'transport_failed' } : { ...preview, status: 'stopped' }, mode === 'stop-error' ? 503 : 200);
      }
      if (suffix === '/cleanup') { state.events.push('cleanup'); return json(res, preview.cleanup || { identity: preview.identity, cleanup_receipt: null, retention_expires_at: null }); }
      if (suffix === '/provenance') {
        state.events.push('provenance');
        if (mode === 'missing-provenance') return json(res, {}, 503);
        return json(res, { schema_version: 1, identity: preview.identity, captured_at: preview.captured_at,
          snapshot_manifest: { schema_version: 1, algorithm: 'workspace-manifest-sha256-v1', selection_policy: mode === 'static' ? 'static-display-root-v1' : 'dynamic-staged-session-v1',
            manifest_sha256: mode === 'wrong-source' ? '0'.repeat(64) : manifestHash, file_count: Object.keys(validatedManifest).length, total_bytes: manifestBytes } });
      }
      if (suffix.startsWith('/content/')) {
        if (preview.stopped) {
          if (suffix === '/content/' && req.headers['cache-control'] === 'no-store') {
            state.revokedRequests.push(req.url); state.revokedCookies.push(req.headers.cookie || '');
          }
          return json(res, {}, req.headers.cookie?.includes(capability) ? (mode === 'revocation-200' ? 200 : 404) : 401);
        }
        if (suffix === '/content/style.css') { res.writeHead(mode === 'broken-asset' ? 404 : 200, { 'Content-Type': 'text/css' }); return res.end(style); }
        if (suffix === '/content/missing.png') { res.writeHead(404, { 'Content-Type': 'image/png' }); return res.end(); }
        return html(res, source);
      }
      if (suffix === '/app-request' && req.method === 'POST') {
        const payload = await read(req);
        if (preview.stopped) return json(res, {}, 404);
        state.events.push(`${payload.method}:${payload.target}`);
        let body;
        if (payload.method === 'POST' && payload.target === '/api/records') {
          const input = JSON.parse(Buffer.from(payload.body_base64, 'base64'));
          const record = { id: mode === 'numeric-id' ? state.records.length + 1 : randomUUID(), value: input.value };
          state.mutations.push(record);
          if (mode !== 'echo') state.records.push(record);
          body = { success: mode !== 'failed-200', record };
          if (mode === 'secret-body') body.password = env.CASE_BROWSER_PASSWORD;
        } else if (payload.method === 'GET' && payload.target.startsWith('/api/records')) {
          const unique = new URL(payload.target, 'http://fixture.invalid').searchParams.get('evidence_nonce');
          body = { success: mode !== 'before-failed-200', records: mode === 'preexisting' ? [{ id: 'existing', value: `r7b-${unique}` }]
            : mode === 'nested-preexisting' ? [{ id: 'existing', value: 'old', nested: { description: `prefix-r7b-${unique}` } }]
              : mode === 'uuid-key-preexisting' ? [{ id: 'existing', value: 'old', nested: { [`prefix-${unique}`]: 'old' } }]
                : mode === 'uuid-value-preexisting' ? [{ id: 'existing', value: 'old', nested: { description: `prefix-${unique}` } }]
                  : mode === 'before-invalid-id' ? [{ id: true, value: 'old' }]
                    : mode === 'before-missing-value' ? [{ id: 'existing' }] : state.records };
          if (mode === 'before-missing-success') delete body.success;
          if (mode === 'before-unknown-success') body.success = null;
          if (mode === 'root-array') body = body.records;
          if (mode === 'oversized-body') body.padding = 'x'.repeat(400000);
        } else if (payload.method === 'GET' && payload.target.startsWith('/api/record?')) {
          body = { success: mode !== 'readback-failed-200', record: state.records.at(-1) || { id: 'absent', value: 'not-written' } };
          state.onReadback?.();
        } else return json(res, {}, 422);
        const bodyText = mode === 'duplicate-body' ? '{"records":[],"records":[]}' : JSON.stringify(body);
        return json(res, { status_code: 200, headers: [['content-type', 'application/json']], body_base64: Buffer.from(bodyText).toString('base64') });
      }
      return json(res, {}, 404);
    } catch { json(res, { error: 'fixture_contract_error' }, 500); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const baseURL = `http://127.0.0.1:${server.address().port}`; scope.execution_identity.base_url = baseURL;
  return { baseURL, scope, validatedManifest, scenario, env, state,
    secrets: [token, capability, env.CASE_BROWSER_PASSWORD, env.CASE_BROWSER_USERNAME],
    close: () => new Promise((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); server.closeAllConnections(); }) };
}

module.exports = { startCaseBrowserFixture };
