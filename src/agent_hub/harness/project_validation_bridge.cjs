'use strict';

// This child is untrusted. Python accepts transitions and measures HTTP responses.
const http = require('node:http');
const net = require('node:net');
const dgram = require('node:dgram');
const childProcess = require('node:child_process');
const {syncBuiltinESMExports} = require('node:module');
const {pathToFileURL} = require('node:url');
const {resolve} = require('node:path');
const {AsyncLocalStorage} = require('node:async_hooks');
const readline = require('node:readline');

const MAX_FRAME = 65536;
const write = process.stdout.write.bind(process.stdout);
const originalListen = net.Server.prototype.listen;
const callContext = new AsyncLocalStorage();
const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
let active = null;
let api = null;
let app = null;
let server = null;
let challenge = null;
let violation = null;
const inventory = new Map();
const stores = new Map();
const reporting = new Map();
const readers = new Map();
const observedReturnKinds = new Map();

function send(frame) {
  const line = JSON.stringify(frame) + '\n';
  if (Buffer.byteLength(line) > MAX_FRAME) throw new Error('bridge frame too large');
  write(line);
}

function forbidden() {
  violation = 'module attempted a listener or child process side effect';
  throw new Error(violation);
}

// Only the bridge starts a listener. Guard both CommonJS and builtin ESM imports.
net.Server.prototype.listen = forbidden;
dgram.Socket.prototype.bind = forbidden;
for (const key of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
  childProcess[key] = forbidden;
}
syncBuiltinESMExports();

function emit(event) {
  if (!active) throw new Error('event outside command');
  const seq = ++active.seq;
  return new Promise((resolveReply, reject) => {
    active.pending.set(seq, {resolve: resolveReply, reject});
    send({kind: 'event', id: active.id, seq, event});
  });
}

function requiredMethods(value, names) {
  if (!value || names.some(name => typeof value[name] !== 'function')) {
    throw new Error('module factory returned missing methods: ' + names.join(','));
  }
  return value;
}

function controlledStore(name) {
  let queue = Promise.resolve();
  return {
    transact(update) {
      const context = callContext.getStore();
      if (!context) throw new Error('transaction outside inventory call');
      const pending = queue.then(async () => {
        const base = await emit({type: 'store_begin', store: name, call: context.call});
        const draft = base.state;
        let result;
        try {
          result = update(draft);
          if (result && typeof result.then === 'function') {
            Promise.resolve(result).catch(() => {});
            throw new Error('store updater must be synchronous');
          }
        } catch (error) {
          await emit({type: 'store_abort', store: name, call: context.call, token: base.token});
          throw error;
        }
        await emit({type: 'store_commit', store: name, call: context.call, token: base.token,
          state: draft, response: result});
        return result;
      });
      queue = pending.catch(() => {});
      context.pending.push(pending);
      return pending;
    },
  };
}

async function inventoryCall(command) {
  const instance = inventory.get(command.instance);
  if (!instance || !['addStock', 'reserve'].includes(command.method)) {
    throw new Error('invalid inventory invocation');
  }
  const context = {call: command.call, pending: []};
  return callContext.run(context, async () => {
    const result = await instance[command.method](command.args);
    await Promise.all(context.pending);
    return result;
  });
}

function mapFactory(value, wrap) {
  return value && typeof value.then === 'function' ? Promise.resolve(value).then(wrap) : wrap(value);
}

function injectedResult(method, kinds, invoke) {
  if (challenge && challenge.method === method) {
    const response = structuredClone(challenge.response);
    // Request-scoped factories reuse the kind observed by ordinary HTTP calls.
    const asynchronous = kinds.get(method) ?? observedReturnKinds.get(method);
    return asynchronous ? Promise.resolve(response) : response;
  }
  const result = invoke();
  const asynchronous = Boolean(result && typeof result.then === 'function');
  kinds.set(method, asynchronous);
  observedReturnKinds.set(method, asynchronous);
  return result;
}

function injectedInventory(options) {
  return mapFactory(api.createInventory(options), value => {
    requiredMethods(value, ['addStock', 'reserve']);
    const kinds = new Map();
    const wrapper = Object.create(value);
    for (const method of ['addStock', 'reserve']) {
      Object.defineProperty(wrapper, method, {
        value(args) {
          return injectedResult(method, kinds, () => value[method](args));
        },
        enumerable: true,
      });
    }
    return wrapper;
  });
}

function injectedReporting(options) {
  return mapFactory(api.createReporting(options), value => {
    requiredMethods(value, ['snapshot']);
    const kinds = new Map();
    return Object.create(value, {
      snapshot: {
        value() {
          return injectedResult('snapshot', kinds, () => value.snapshot());
        },
        enumerable: true,
      },
    });
  });
}

async function dispatch(command) {
  if (violation) throw new Error(violation);
  switch (command.op) {
    case 'load': {
      if (api) throw new Error('entry already loaded');
      const loaded = await import(pathToFileURL(resolve(command.entry)).href);
      api = ['createInventory', 'createReporting', 'createApp'].every(
        name => typeof loaded[name] === 'function') ? loaded : loaded.default;
      requiredMethods(api, ['createInventory', 'createReporting', 'createApp']);
      // Let import-scheduled jobs run while listener/child guards are still active.
      await new Promise(resolveTick => setImmediate(resolveTick));
      if (violation) throw new Error(violation);
      return null;
    }
    case 'inventory_create': {
      if (inventory.has(command.instance)) throw new Error('duplicate inventory instance');
      if (!stores.has(command.store)) stores.set(command.store, controlledStore(command.store));
      const value = await api.createInventory({store: stores.get(command.store)});
      inventory.set(command.instance, requiredMethods(value, ['addStock', 'reserve']));
      return null;
    }
    case 'inventory_call':
      return inventoryCall(command);
    case 'inventory_pair':
      return Promise.all(command.calls.map(inventoryCall));
    case 'reporting_create': {
      if (reporting.has(command.instance)) throw new Error('duplicate reporting instance');
      const reader = {rows: null, reads: []};
      const value = await api.createReporting({read() {
        if (reader.rows === null) throw new Error('report read outside snapshot');
        const rows = structuredClone(reader.rows);
        reader.reads.push(rows);
        return rows;
      }});
      readers.set(command.instance, reader);
      reporting.set(command.instance, requiredMethods(value, ['snapshot']));
      return null;
    }
    case 'reporting_snapshot': {
      const reader = readers.get(command.instance);
      reader.rows = command.rows;
      reader.reads = [];
      const result = await reporting.get(command.instance).snapshot();
      for (const rows of reader.reads) {
        await emit({type: 'report_read', instance: command.instance, rows});
      }
      reader.rows = null;
      return result;
    }
    case 'app_start': {
      if (app || server) throw new Error('app already started');
      app = requiredMethods(await api.createApp({dataDir: process.env.DATA_DIR,
        inventoryFactory: injectedInventory, reportingFactory: injectedReporting}), ['handler', 'close']);
      server = http.createServer(app.handler.bind(app));
      await new Promise((resolveStart, reject) => {
        server.once('error', reject);
        originalListen.call(server, 0, '127.0.0.1', resolveStart);
      });
      return {port: server.address().port};
    }
    case 'app_challenge':
      challenge = command.challenge;
      return null;
    case 'app_close': {
      let error;
      try {
        if (app) await app.close();
      } catch (failure) {
        error = failure;
      } finally {
        if (server) {
          server.closeAllConnections();
          await new Promise((resolveClose, reject) => server.close(
            failure => failure ? reject(failure) : resolveClose()));
        }
      }
      app = null;
      server = null;
      if (error) throw error;
      return null;
    }
    default:
      throw new Error('unsupported bridge command');
  }
}

input.on('line', line => {
  try {
    if (Buffer.byteLength(line) + 1 > MAX_FRAME) throw new Error('input frame too large');
    const frame = JSON.parse(line);
    if (frame.kind === 'event_reply') {
      if (!active || frame.id !== active.id || !active.pending.has(frame.seq)) {
        throw new Error('unexpected event reply');
      }
      const pending = active.pending.get(frame.seq);
      active.pending.delete(frame.seq);
      pending.resolve(frame.data);
      return;
    }
    if (frame.kind !== 'request' || active || typeof frame.id !== 'string') {
      throw new Error('unexpected request frame');
    }
    active = {id: frame.id, seq: 0, pending: new Map()};
    Promise.resolve().then(() => dispatch(frame.command)).then(result => {
      if (violation) throw new Error(violation);
      if (active.pending.size) throw new Error('unfinished bridge events');
      send({kind: 'result', id: active.id, result});
      active = null;
    }).catch(error => {
      send({kind: 'result', id: frame.id, result: {bridge_error: String(error.message || error)}});
      active = null;
    });
  } catch (_) {
    process.exitCode = 1;
    input.close();
    process.stdin.destroy();
  }
});
