'use strict';

// Trusted test preload: force the former child-first startup to collide.
const http = require('node:http');
const fs = require('node:fs');
const processes = require('node:child_process');
const servers = [];
const children = [];
let childReady = false;
const writeHead = http.ServerResponse.prototype.writeHead;
http.ServerResponse.prototype.writeHead = function (...args) {
  fs.appendFileSync('startup-responses.jsonl', JSON.stringify({
    pid: process.pid, childReady,
  }) + '\n');
  return writeHead.apply(this, args);
};
const createServer = http.createServer;
http.createServer = function (...args) {
  const server = createServer(...args);
  servers.push(server);
  server.on('error', error => {
    if (error.code !== 'EADDRINUSE') throw error;
    // The failing regression must not leave its own listener orphaned.
    for (const child of children) child.kill();
    process.exitCode = 73;
  });
  return server;
};
const spawn = processes.spawn;
processes.spawn = function (command, args, options) {
  const port = Number(process.env.PORT);
  const bound = servers.some(server => server.address()?.port === port);
  fs.appendFileSync('startup-order.jsonl', JSON.stringify({pid: process.pid, bound}) + '\n');
  const selected = [...args];
  if (!bound) {
    selected[1] = selected[1].replace(".listen(0,'127.0.0.1'", `.listen(${port},'127.0.0.1'`);
  }
  const child = spawn(command, selected, options);
  children.push(child);
  const emit = child.stdout.emit;
  child.stdout.emit = function (event, ...values) {
    if (event === 'data') {
      // Hold the handshake briefly so an ungated primary response is observable.
      setTimeout(() => {
        childReady = true;
        emit.call(this, event, ...values);
      }, 150);
      return true;
    }
    return emit.call(this, event, ...values);
  };
  return child;
};
