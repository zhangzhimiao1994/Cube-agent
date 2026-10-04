'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

const fault = fs.readFileSync('fault.txt', 'utf8').trim();
const data = process.env.DATA_DIR;
const file = path.join(data, 'portfolio.jsonl');
const modules = ['budgets', 'staffing', 'risks', 'milestones'];
const state = {programs: [], projects: [], budgets: [], staffing: [], risks: [], milestones: []};
const restarted = fs.existsSync(file);
if (restarted) {
  for (const line of fs.readFileSync(file, 'utf8').trim().split('\n')) {
    if (line) {
      const {collection, record} = JSON.parse(line);
      state[collection].push(record);
    }
  }
}
const compare = (a, b) => String(a.id) < String(b.id) ? -1 : String(a.id) > String(b.id) ? 1 : 0;
if (restarted && fault === 'restart_tail_loss') {
  const target = state.programs[0].id;
  const last = state.projects.filter(p => p.program_id === target).sort(compare).at(-1);
  state.projects = state.projects.filter(p => p !== last);
}
let updated = false;
function persist(collection, record) {
  state[collection].push(record);
  fs.appendFileSync(file, JSON.stringify({collection, record}) + '\n');
}
function readModel(project) {
  const records = module => state[module].filter(row => row.project_id === project.id);
  let budgets = records('budgets');
  if (fault === 'stale_updated_aggregate' && updated) budgets = budgets.slice(0, -1);
  const row = {
    project_id: project.id, program_id: project.program_id, name: project.name,
    budget_total: budgets.reduce((n, r) => n + r.amount, 0),
    staffing_allocation: records('staffing').reduce((n, r) => n + r.allocation, 0),
    risk_count: records('risks').length, milestone_count: records('milestones').length,
  };
  if (fault === 'wrong_name') row.name = 'not the submitted name';
  if (fault === 'boolean_aggregate') row.risk_count = Boolean(row.risk_count);
  return row;
}

const server = http.createServer(async (req, res) => {
  let raw = '';
  for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : {};
  const url = new URL(req.url, 'http://127.0.0.1');
  const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  const send = (status, payload) => {
    fs.appendFileSync('requests.jsonl', JSON.stringify({
      pid: process.pid, method: req.method, url: req.url, body, status, payload,
    }) + '\n');
    res.writeHead(status, {'Content-Type': 'application/json'});
    if (fault === 'slow_program' && req.method === 'POST' && url.pathname === '/programs') {
      const timer = setInterval(() => res.write(' '), 25);
      res.on('close', () => clearInterval(timer));
      return;
    }
    res.end(JSON.stringify(payload));
  };
  if (parts.length === 1 && ['programs', 'projects'].includes(parts[0])) {
    const collection = parts[0];
    if (req.method === 'GET') return send(200, {items: state[collection]});
    if (req.method === 'POST') {
      const index = state[collection].length + 1;
      let id = collection === 'programs' ? 'program-' + index : String(index);
      if (fault === 'mixed_ids' && collection === 'projects' && index % 2 === 0) id = index;
      if (collection === 'projects' && fault.startsWith('invalid_id:')) {
        id = JSON.parse(fault.slice('invalid_id:'.length));
      }
      if (fault === 'duplicate_id' && collection === 'projects') id = 'same-id';
      if (fault === 'duplicate_program' && collection === 'programs') id = 'same-program';
      const record = {...body, id};
      persist(collection, record);
      return send(201, record);
    }
  }
  if (req.method === 'POST' && parts[0] === 'projects' && modules.includes(parts[2])) {
    const project = state.projects.find(p => String(p.id) === parts[1]);
    if (!project) return send(404, {error: {code: 'NOT_FOUND', message: 'Missing project'}});
    const collection = parts[2];
    const record = {...body, id: collection + '-' + (state[collection].length + 1),
      project_id: project.id};
    persist(collection, record);
    if (restarted && collection === 'budgets') updated = true;
    return send(201, record);
  }
  if (req.method === 'GET' && url.pathname === '/portfolio/read-model') {
    const program = url.searchParams.get('program_id');
    const selected = state.projects.filter(p => String(p.program_id) === program).sort(compare);
    let offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0));
    let limit = Math.max(1, Math.min(100, Number(url.searchParams.get('limit') ?? 100)));
    if (fault === 'ignore_offset') offset = 0;
    if (fault === 'ignore_limit') limit = 100;
    let source = fault === 'first100' ? selected.slice(0, 100) : selected;
    if (fault === 'filter_after_page') source = [...state.projects].sort(compare);
    let page = source.slice(offset, offset + limit);
    if (fault === 'filter_after_page') page = page.filter(p => String(p.program_id) === program);
    if (fault === 'duplicate' && page.length > 1) page[1] = page[0];
    if (fault === 'missing_tail' && offset >= selected.length) page = selected.slice(-1);
    return send(200, {items: page.map(readModel)});
  }
  return send(404, {error: {code: 'NOT_FOUND', message: 'Unknown endpoint'}});
});

const child = spawn(process.execPath, ['-e',
  "require('node:http').createServer(()=>{}).listen(0,'127.0.0.1',function(){" +
  "console.log(this.address().port)});process.on('SIGTERM',()=>{});"],
  {stdio: ['ignore', 'pipe', 'ignore']});
child.stdout.once('data', value => {
  fs.appendFileSync('launches.jsonl', JSON.stringify({
    pid: process.pid, childPid: child.pid, port: Number(process.env.PORT),
    childPort: Number(value.toString()), data, home: process.env.HOME,
    tmp: process.env.TMP, cache: process.env.npm_config_cache,
  }) + '\n');
  server.listen(Number(process.env.PORT), '127.0.0.1');
});
