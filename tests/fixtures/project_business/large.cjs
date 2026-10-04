'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {spawn} = require('node:child_process');

const fault = fs.existsSync('fault.txt') ? fs.readFileSync('fault.txt', 'utf8').trim() : '';
const data = process.env.DATA_DIR;
fs.mkdirSync(data, {recursive: true});
const file = path.join(data, 'orders.json');
const state = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {
  catalog: [], stock: {}, stocked: {}, reservations: [], orders: [], fulfillment: [], audit: [],
};
if (fault === 'volatile_inventory') state.stock = {...state.stocked};
if (fault === 'lost_inventory') state.stock = {};
function save() {
  fs.writeFileSync(file + '.tmp', JSON.stringify(state));
  fs.renameSync(file + '.tmp', file);
}
function audit(entity_id, action) {
  state.audit.push({id: randomUUID(), entity_id, action});
}
function summary() {
  const report = {
    orders: {
      total: state.orders.length,
      authorized_count: state.orders.filter(row => row.payment_state === 'authorized').length,
    },
    inventory: {
      reserved_units: state.reservations.reduce((total, row) => total + row.quantity, 0),
    },
    fulfillment: {
      total: state.fulfillment.length,
      cancelled_count: state.fulfillment.filter(row => row.status === 'cancelled').length,
    },
  };
  if (fault === 'static_report') {
    return {orders: {total: 0, authorized_count: 0}, inventory: {reserved_units: 0},
      fulfillment: {total: 0, cancelled_count: 0}};
  }
  if (fault.startsWith('report_value:')) {
    const [, field, value] = fault.split(':');
    const [section, metric] = field.split('.');
    report[section][metric] = JSON.parse(value);
  }
  return report;
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
    res.end(JSON.stringify(payload));
  };
  const error = (status, code, message) => send(status, {error: {code, message}});
  if (url.pathname === '/catalog/items') {
    if (req.method === 'GET') return send(200, {items: state.catalog});
    if (req.method === 'POST') {
      const item = {...body, id: randomUUID()};
      state.catalog.push(item); save(); return send(201, item);
    }
  }
  if (req.method === 'POST' && url.pathname === '/inventory/stock') {
    state.stock[body.sku] = (state.stock[body.sku] || 0) + body.quantity;
    state.stocked[body.sku] = (state.stocked[body.sku] || 0) + body.quantity;
    save(); return send(201, {...body, id: randomUUID()});
  }
  if (req.method === 'POST' && url.pathname === '/inventory/reservations') {
    const available = state.stock[body.sku] || 0;
    if (body.quantity > available) {
      if (fault === 'failed_request_deducts') {
        state.stock[body.sku] = Math.max(0, available - body.quantity); save();
      }
      if (fault === 'failed_empty_deducts' && available <= 0) {
        state.stock[body.sku] = available - body.quantity; save();
      }
      return error(409, 'INSUFFICIENT_STOCK', 'Not enough stock');
    }
    if (fault !== 'no_deduction') state.stock[body.sku] = available - body.quantity;
    const reservation = {...body, id: randomUUID()};
    state.reservations.push(reservation); save(); return send(201, reservation);
  }
  if (req.method === 'POST' && url.pathname === '/orders') {
    if (state.orders.some(row => row.client_request_id === body.client_request_id)) {
      return error(409, 'DUPLICATE_ORDER', 'Request already submitted');
    }
    const order = {...body, id: randomUUID(), status: 'created', payment_state: 'pending'};
    if (fault === 'implicit_order_reservation') {
      if (body.lines.some(line => (state.stock[line.sku] || 0) < line.quantity)) {
        return error(409, 'INSUFFICIENT_STOCK', 'Not enough stock for order');
      }
      for (const line of body.lines) {
        state.stock[line.sku] -= line.quantity;
        state.reservations.push({...line, id: randomUUID(), order_id: order.id});
      }
    }
    state.orders.push(order); audit(order.id, 'order.created'); save(); return send(201, order);
  }
  if (parts[0] === 'orders' && parts.length >= 2) {
    const order = state.orders.find(row => row.id === parts[1]);
    if (!order) return error(404, 'NOT_FOUND', 'Order not found');
    if (req.method === 'GET' && parts.length === 2) return send(200, order);
    if (req.method === 'POST' && parts[2] === 'payment') {
      order.payment_state = body.state;
      audit(order.id, 'payment.authorized'); save(); return send(200, order);
    }
  }
  if (req.method === 'POST' && url.pathname === '/fulfillment/jobs') {
    const job = {...body, id: randomUUID(), status: 'pending'};
    state.fulfillment.push(job); save(); return send(201, job);
  }
  if (parts[0] === 'fulfillment' && parts[1] === 'jobs' && parts.length === 3) {
    const job = state.fulfillment.find(row => row.id === parts[2]);
    if (!job) return error(404, 'NOT_FOUND', 'Fulfillment job not found');
    if (req.method === 'GET') return send(200, job);
    if (req.method === 'PATCH') {
      if (job.status === 'cancelled' && body.status === 'completed') {
        return error(409, 'CANCELLED_JOB', 'Cancelled jobs cannot complete');
      }
      job.status = body.status; save(); return send(200, job);
    }
  }
  if (req.method === 'GET' && url.pathname === '/audit') {
    let items = state.audit.filter(row => row.entity_id === url.searchParams.get('entity_id'));
    if (fault === 'audit_unfiltered') items = state.audit;
    if (fault === 'audit_wrong_entity') {
      items = items.map(row => ({...row, entity_id: 'unrelated-order'}));
    }
    if (fault === 'audit_no_action') items = items.map(({action, ...row}) => row);
    if (fault === 'audit_missing_created') items = items.filter(row => row.action !== 'order.created');
    if (fault === 'audit_missing_payment') {
      items = items.filter(row => row.action !== 'payment.authorized');
    }
    return send(200, {items});
  }
  if (req.method === 'GET' && url.pathname === '/admin/reports/summary') {
    return send(200, summary());
  }
  return error(404, 'NOT_FOUND', 'Unknown endpoint');
});

// A real descendant listener also has to disappear when npm's process tree is stopped.
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
