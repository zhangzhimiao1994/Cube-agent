'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');

function observe(dataDir, file, value) {
  // Windows fixture tests keep audit copies after owned DATA_DIR cleanup.
  const directory = process.platform === 'win32' ? process.cwd() : dataDir;
  fs.appendFileSync(path.join(directory, file), JSON.stringify(value) + '\n');
}

function conflict() {
  return {status: 409, body: {error: {code: 'INSUFFICIENT_STOCK', message: 'Not enough stock'}}};
}

function createInventory({store}) {
  return {
    addStock({sku, quantity}) {
      return store.transact(draft => {
        draft.stock[sku] = (draft.stock[sku] || 0) + quantity;
        return {status: 201, body: {id: randomUUID(), sku, quantity}};
      });
    },
    reserve({sku, quantity, reason}) {
      return store.transact(draft => {
        if ((draft.stock[sku] || 0) < quantity) {
          return conflict();
        }
        draft.stock[sku] -= quantity;
        const row = {id: randomUUID(), sku, quantity, reason};
        draft.reservations.push(row);
        return {status: 201, body: row};
      });
    },
  };
}

function createReporting({read}) {
  return {
    async snapshot() {
      const rows = await read();
      return {
        orders: {total: rows.orders.length,
          authorized_count: rows.orders.filter(row => row.payment_state === 'authorized').length},
        inventory: {
          reserved_units: rows.reservations.reduce((sum, row) => sum + row.quantity, 0),
        },
        fulfillment: {total: rows.fulfillment.length,
          cancelled_count: rows.fulfillment.filter(row => row.status === 'cancelled').length},
      };
    },
  };
}

async function createApp({dataDir, inventoryFactory = createInventory,
  reportingFactory = createReporting}) {
  fs.mkdirSync(dataDir, {recursive: true});
  observe(dataDir, 'app-observations.jsonl', {dataDir});
  let state = {stock: {}, reservations: []};
  let queue = Promise.resolve();
  const rows = {orders: [], reservations: [], fulfillment: []};
  const store = {
    transact(update) {
      const pending = queue.then(() => {
        const draft = structuredClone(state);
        const result = update(draft);
        if (result && typeof result.then === 'function') throw new Error('async updater');
        state = draft;
        rows.reservations = state.reservations;
        return result;
      });
      queue = pending.catch(() => {});
      return pending;
    },
  };
  const inventory = await inventoryFactory({store});
  const reporting = await reportingFactory({read: () => rows});
  let reportCache;
  return {
    async handler(req, res) {
      let raw = '';
      for await (const chunk of req) raw += chunk;
      const body = raw ? JSON.parse(raw) : {};
      const send = (status, payload) => {
        observe(dataDir, 'app-observations.jsonl', {port: req.socket.localPort});
        observe(dataDir, 'http-observations.jsonl', {
          method: req.method, path: req.url, status, body: payload,
        });
        res.writeHead(status, {'Content-Type': 'application/json'});
        res.end(JSON.stringify(payload));
      };
      if (req.method === 'POST' && req.url === '/catalog/items') {
        return send(201, {...body, id: randomUUID()});
      }
      if (req.method === 'POST' && req.url === '/inventory/stock') {
        const result = await inventory.addStock(body);
        return send(result.status, result.body);
      }
      if (req.method === 'POST' && req.url === '/inventory/reservations') {
        const result = await inventory.reserve(body);
        return send(result.status, result.body);
      }
      if (req.method === 'GET' && req.url === '/admin/reports/summary') {
        const result = await reporting.snapshot();
        return send(200, result);
      }
      return send(404, {error: {code: 'NOT_FOUND', message: 'Unknown endpoint'}});
    },
    async close() {
      observe(dataDir, 'app-observations.jsonl', {closed: true});
    },
  };
}

module.exports = {createInventory, createReporting, createApp};
