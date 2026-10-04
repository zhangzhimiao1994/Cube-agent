'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {spawn} = require('node:child_process');

const fault = fs.existsSync('fault.txt') ? fs.readFileSync('fault.txt', 'utf8').trim() : '';
const data = process.env.DATA_DIR;
fs.mkdirSync(data, {recursive: true});
const file = path.join(data, 'portfolio.json');
const modules = ['milestones', 'budgets', 'staffing', 'risks'];
const state = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {
  programs: [], projects: [], dependencies: [], approvals: [],
  milestones: [], budgets: [], staffing: [], risks: [],
};
function save() {
  const persisted = {...state};
  for (const module of modules) {
    if (fault === 'volatile_' + module) persisted[module] = [];
  }
  fs.writeFileSync(file + '.tmp', JSON.stringify(persisted));
  fs.renameSync(file + '.tmp', file);
}
const rowsFor = (module, project) => state[module].filter(row => row.project_id === project.id);
function readModel(project) {
  const row = {
    project_id: project.id, program_id: project.program_id, name: project.name,
    budget_total: rowsFor('budgets', project).reduce((total, item) => total + item.amount, 0),
    staffing_allocation: rowsFor('staffing', project).reduce(
      (total, item) => total + item.allocation, 0),
    risk_count: rowsFor('risks', project).length,
    milestone_count: rowsFor('milestones', project).length,
  };
  if (fault === 'read_model_fake_aggregate') {
    row.budget_total = rowsFor('budgets', project).length ? 125000 : 0;
  }
  if (fault === 'read_model_first_budget') {
    row.budget_total = rowsFor('budgets', project)[0]?.amount ?? 0;
  }
  if (fault === 'read_model_first_staffing') {
    row.staffing_allocation = rowsFor('staffing', project)[0]?.allocation ?? 0;
  }
  if (fault === 'read_model_presence_risks') {
    row.risk_count = rowsFor('risks', project).length ? 1 : 0;
  }
  if (fault === 'read_model_presence_milestones') {
    row.milestone_count = rowsFor('milestones', project).length ? 1 : 0;
  }
  return row;
}
const csvCell = value => '"' + String(value).replaceAll('"', '""') + '"';

const server = http.createServer(async (req, res) => {
  let raw = '';
  for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : {};
  const url = new URL(req.url, 'http://127.0.0.1');
  const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  const send = (status, payload, csv = false) => {
    fs.appendFileSync('requests.jsonl', JSON.stringify({
      pid: process.pid, method: req.method, url: req.url, body, status, payload,
    }) + '\n');
    res.writeHead(status, {'Content-Type': csv ? 'text/csv; charset=utf-8' : 'application/json'});
    res.end(csv ? payload : JSON.stringify(payload));
  };
  const error = (status, code, message) => send(status, {error: {code, message}});
  const selectedProjects = () => state.projects.filter(
    project => project.program_id === url.searchParams.get('program_id'));
  if (req.method === 'GET' && url.pathname === '/analytics/portfolio.csv') {
    const header = 'project_id,program_id,name\r\n';
    if (fault === 'csv_static') {
      return send(200, header + 'fixed-project,fixed-program,Customer Migration\r\n', true);
    }
    let projects = fault === 'csv_unfiltered' ? state.projects : selectedProjects();
    if (fault === 'csv_duplicate' && projects.length) projects = [...projects, projects[0]];
    let csv = header + projects.map(project => [project.id, project.program_id, project.name]
      .map(csvCell).join(',')).join('\r\n') + '\r\n';
    if (fault === 'csv_bad_quote') {
      csv = header + projects.map(project =>
        `${project.id},${project.program_id},"${project.name}`).join('\r\n');
    }
    return send(200, csv, true);
  }
  if (req.method === 'GET' && url.pathname === '/portfolio/read-model') {
    if (fault === 'read_model_static') {
      return send(200, {items: [{project_id: 'fixed-project', program_id: 'fixed-program',
        name: 'Customer Migration', budget_total: 125000, staffing_allocation: 0.5,
        risk_count: 1, milestone_count: 1}]});
    }
    const projects = fault === 'read_model_unfiltered' ? state.projects : selectedProjects();
    return send(200, {items: projects.map(readModel)});
  }
  if (parts.length === 1 && ['programs', 'projects'].includes(parts[0])) {
    if (req.method === 'GET') return send(200, {items: state[parts[0]]});
    if (req.method === 'POST') {
      const record = {...body, id: randomUUID()};
      state[parts[0]].push(record); save(); return send(201, record);
    }
  }
  if (parts[0] === 'projects' && parts.length === 3 && modules.includes(parts[2])) {
    const project = state.projects.find(row => row.id === parts[1]);
    if (!project) return error(404, 'NOT_FOUND', 'Project not found');
    const module = parts[2];
    if (req.method === 'GET') {
      let items = rowsFor(module, project);
      if (fault === 'wrong_project_' + module) {
        items = items.map(row => ({...row, project_id: 'unrelated-project'}));
      }
      if (fault === 'wrong_fields_' + module) {
        const field = {milestones: 'due_at', budgets: 'amount',
          staffing: 'allocation', risks: 'severity'}[module];
        items = items.map(row => ({...row, [field]: 'corrupted'}));
      }
      return send(200, {items});
    }
    if (req.method === 'POST') {
      const record = {...body, id: randomUUID(), project_id: project.id};
      if (fault !== 'drop_' + module) state[module].push(record);
      save(); return send(201, record);
    }
  }
  if (req.method === 'POST' && url.pathname === '/dependencies') {
    if (![body.from_project_id, body.to_project_id].every(
      id => state.projects.some(project => project.id === id))) {
      return error(409, 'INVALID_DEPENDENCY', 'Dependency must link existing projects');
    }
    const dependency = {...body, id: randomUUID()};
    state.dependencies.push(dependency); save(); return send(201, dependency);
  }
  if (req.method === 'POST' && url.pathname === '/approvals') {
    const approval = {...body, id: randomUUID(), decision: 'pending'};
    state.approvals.push(approval); save(); return send(201, approval);
  }
  if (parts[0] === 'approvals' && parts.length === 2 && req.method === 'PATCH') {
    if (body.role !== 'portfolio_admin') return error(403, 'FORBIDDEN', 'Admin role required');
    const approval = state.approvals.find(row => row.id === parts[1]);
    if (!approval) return error(404, 'NOT_FOUND', 'Approval not found');
    approval.decision = body.decision; save(); return send(200, approval);
  }
  if (req.method === 'POST' && url.pathname === '/access/check') {
    return send(200, {allowed: body.role === 'portfolio_admin'});
  }
  if (req.method === 'GET' && parts.length === 2 &&
      ['programs', 'projects', 'dependencies', 'approvals'].includes(parts[0])) {
    const record = state[parts[0]].find(row => row.id === parts[1]);
    if (record) return send(200, record);
  }
  return error(404, 'NOT_FOUND', 'Unknown endpoint');
});

const child = spawn(process.execPath, ['-e',
  "require('node:http').createServer(()=>{}).listen(0,'127.0.0.1',function(){" +
  "console.log(this.address().port)});process.on('SIGTERM',()=>{});"],
  {stdio: ['ignore', 'pipe', 'ignore']});
child.stdout.once('data', value => {
  fs.appendFileSync('launches.jsonl', JSON.stringify({
    pid: process.pid, childPid: child.pid, port: Number(process.env.PORT),
    childPort: Number(value.toString()), data,
  }) + '\n');
  server.listen(Number(process.env.PORT), '127.0.0.1');
});
