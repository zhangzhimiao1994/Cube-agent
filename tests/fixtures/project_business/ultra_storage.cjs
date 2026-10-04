'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const fault = fs.readFileSync('fault.txt', 'utf8').trim();
const data = process.env.DATA_DIR;
const originFile = path.join(data, 'origin.json');
if (!fs.existsSync(originFile)) {
  fs.writeFileSync(originFile, JSON.stringify({data, code: process.cwd()}));
}
const origin = JSON.parse(fs.readFileSync(originFile, 'utf8'));
const relocated = origin.data !== data;
let storage = data;
if (fault === 'home') storage = process.env.HOME;
if (fault === 'cwd') storage = process.cwd();
if (fault === 'global_tmp') storage = process.platform === 'win32' ? path.dirname(process.cwd()) : '/tmp';
if (fault === 'old_absolute') storage = origin.data;
const file = path.join(storage, 'portfolio.jsonl');
const state = {programs: [], projects: [], budgets: [], staffing: [], risks: [], milestones: []};
if (fs.existsSync(file)) {
  for (const line of fs.readFileSync(file, 'utf8').trim().split('\n')) {
    if (line) {
      const {collection, record} = JSON.parse(line);
      state[collection].push(record);
    }
  }
}
if (relocated && fault === 'foreign_loss') {
  state.projects = state.projects.filter(p => p.program_id === state.programs[0].id);
}
if (relocated && fault === 'stale_aggregate') state.budgets.pop();
const modules = ['budgets', 'staffing', 'risks', 'milestones'];
const compare = (a, b) => String(a.id) < String(b.id) ? -1 : String(a.id) > String(b.id) ? 1 : 0;
function persist(collection, record) {
  state[collection].push(record);
  fs.appendFileSync(file, JSON.stringify({collection, record}) + '\n');
}
function readModel(project) {
  const records = module => state[module].filter(row => row.project_id === project.id);
  return {
    project_id: project.id, program_id: project.program_id, name: project.name,
    budget_total: records('budgets').reduce((n, r) => n + r.amount, 0),
    staffing_allocation: records('staffing').reduce((n, r) => n + r.allocation, 0),
    risk_count: records('risks').length, milestone_count: records('milestones').length,
  };
}
let childPort;
const server = http.createServer(async (req, res) => {
  let raw = '';
  for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : {};
  const url = new URL(req.url, 'http://127.0.0.1');
  const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  const send = (status, payload) => {
    res.writeHead(status, {'Content-Type': 'application/json'});
    res.end(JSON.stringify(payload));
  };
  if (req.method === 'GET' && url.pathname === '/__fixture') {
    return send(200, {pid: process.pid, childPid: child.pid, childPort,
      port: Number(process.env.PORT), code: process.cwd(), data,
      home: process.env.HOME, tmp: process.env.TMP, cache: process.env.npm_config_cache});
  }
  if (parts.length === 1 && ['programs', 'projects'].includes(parts[0])) {
    const collection = parts[0];
    if (req.method === 'GET' && collection === 'programs') return send(200, {items: state.programs});
    if (req.method === 'POST') {
      if (fault === 'slow_program' && collection === 'programs') {
        res.writeHead(201, {'Content-Type': 'application/json'});
        const timer = setInterval(() => res.write(' '), 25);
        res.on('close', () => clearInterval(timer));
        return;
      }
      const index = state[collection].length + 1;
      let id = collection === 'programs' ? 'program-' + index : String(index);
      const invalidPrefix = 'b_invalid_' + collection + ':';
      if (String(body.name).startsWith('B ') && fault.startsWith(invalidPrefix)) {
        id = JSON.parse(fault.slice(invalidPrefix.length));
      }
      const record = {...body, id};
      persist(collection, record);
      return send(201, record);
    }
  }
  if (parts[0] === 'projects' && parts.length >= 2) {
    const project = state.projects.find(p => String(p.id) === parts[1]);
    if (!project) return send(404, {error: {code: 'NOT_FOUND', message: 'Missing project'}});
    if (req.method === 'GET' && parts.length === 2) return send(200, project);
    if (req.method === 'POST' && modules.includes(parts[2])) {
      const collection = parts[2];
      const record = {...body, id: collection + '-' + (state[collection].length + 1), project_id: project.id};
      persist(collection, record);
      return send(201, record);
    }
  }
  if (req.method === 'GET' && url.pathname === '/portfolio/read-model') {
    const selected = state.projects.filter(p => String(p.program_id) === url.searchParams.get('program_id')).sort(compare);
    const offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0));
    const limit = Math.max(1, Math.min(100, Number(url.searchParams.get('limit') ?? 100)));
    return send(200, {items: selected.slice(offset, offset + limit).map(readModel)});
  }
  return send(404, {error: {code: 'NOT_FOUND', message: 'Unknown endpoint'}});
});
const child = spawn(process.execPath, ['-e',
  "require('node:http').createServer(()=>{}).listen(0,'127.0.0.1',function(){" +
  "console.log(this.address().port)});process.on('SIGTERM',()=>{});"],
  {stdio: ['ignore', 'pipe', 'ignore']});
child.stdout.once('data', value => {
  childPort = Number(value.toString());
  server.listen(Number(process.env.PORT), '127.0.0.1');
});
