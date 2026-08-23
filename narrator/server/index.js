'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const express = require('express');

const SERVER_ROOT = __dirname;
const APP_ROOT = path.resolve(SERVER_ROOT, '..');
const AUDIO_ROOT = path.join(APP_ROOT, 'out');
const PAGE_PATH = path.join(SERVER_ROOT, 'index.html');
const PORT = Number.parseInt(process.env.PORT || '3000', 10);
const eventClients = new Set();
let activeRun = null;

const app = express();
app.use(express.json({ limit: '32kb' }));

function textValue(value, name) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 4096) {
    throw new Error(`${name} must be a non-empty string`);
  }
  if (value.includes('\0')) throw new Error(`${name} contains an invalid character`);
  return value;
}

function cliPath(value, name, extension) {
  const raw = textValue(value, name);
  const resolved = path.resolve(APP_ROOT, raw);
  if (extension && path.extname(resolved).toLowerCase() !== extension) {
    throw new Error(`${name} must be a ${extension} file`);
  }
  return resolved;
}

function integerValue(value, name, minimum, maximum) {
  const number = typeof value === 'number' ? value : Number(value);
  if (!Number.isInteger(number) || number < minimum || number > maximum) {
    throw new Error(`${name} must be an integer from ${minimum} to ${maximum}`);
  }
  return String(number);
}

function buildRunArgs(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('JSON body must be an object');
  }

  if (Array.isArray(body.args)) {
    if (body.args.length > 64) throw new Error('too many command arguments');
    return body.args.map((argument) => textValue(argument, 'args item'));
  }

  const book = body.book ?? body.input;
  if (book === undefined) throw new Error('book is required');

  const args = ['--book', cliPath(book, 'book', '.txt')];
  if (body.reference !== undefined && body.reference !== '') {
    args.push('--reference', cliPath(body.reference, 'reference', '.wav'));
  }
  if (body.gap_ms !== undefined && body.gap_ms !== '') {
    args.push('--gap-ms', integerValue(body.gap_ms, 'gap_ms', 0, 3600000));
  }
  if (body.concurrency !== undefined && body.concurrency !== '') {
    args.push('--concurrency', integerValue(body.concurrency, 'concurrency', 1, 64));
  }
  if (body.tagger !== undefined && body.tagger !== '') {
    const tagger = textValue(body.tagger, 'tagger');
    if (!['none', 'claude', 'codex'].includes(tagger)) {
      throw new Error('tagger must be none, claude, or codex');
    }
    args.push('--tagger', tagger);
  }
  if (body.chapters !== undefined && body.chapters !== '') {
    args.push('--chapters', textValue(body.chapters, 'chapters'));
  }
  return args;
}

function pythonExePath() {
  const candidates = process.platform === 'win32'
    ? [path.join(APP_ROOT, '.venv', 'Scripts', 'python.exe'), path.join(APP_ROOT, 'venv', 'Scripts', 'python.exe')]
    : [path.join(APP_ROOT, '.venv', 'bin', 'python'), path.join(APP_ROOT, 'venv', 'bin', 'python')];
  return candidates.find((candidate) => fs.existsSync(candidate)) || 'python';
}

function sendSseLine(response, line) {
  if (response.writableEnded) return;
  try {
    response.write(`data: ${line}\n\n`);
  } catch {
    eventClients.delete(response);
  }
}

function broadcast(line) {
  for (const response of eventClients) sendSseLine(response, line);
}

function attachRun(run) {
  const { child } = run;
  if (child.stdout) {
    const lines = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
    run.lines = lines;
    lines.on('line', broadcast);
  }
  if (child.stderr) child.stderr.on('data', (chunk) => process.stderr.write(chunk));
  child.once('error', (error) => {
    process.stderr.write(`narrate.py failed to start: ${error.message}\n`);
    if (activeRun === run) activeRun = null;
  });
  child.once('close', () => {
    run.closed = true;
    if (run.lines) run.lines.close();
    if (activeRun === run) activeRun = null;
  });
}

function cancelRun(run) {
  if (run.closed) return Promise.resolve();
  if (run.cancelPromise) return run.cancelPromise;

  run.cancelPromise = new Promise((resolve) => {
    let settled = false;
    const forceTimer = setTimeout(() => {
      if (!run.closed) {
        try { run.child.kill(); } catch (error) { process.stderr.write(`cancel failed: ${error.message}\n`); }
      }
    }, 250);
    const releaseTimer = setTimeout(() => {
      if (!run.closed && activeRun === run) activeRun = null;
      finish();
    }, 1500);
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(forceTimer);
      clearTimeout(releaseTimer);
      resolve();
    };
    run.child.once('close', finish);
    run.cancelRequested = true;
    try { run.child.kill('SIGTERM'); } catch (error) { process.stderr.write(`cancel failed: ${error.message}\n`); }
  });
  return run.cancelPromise;
}

function safeAudioPath(value) {
  if (typeof value !== 'string' || value.length === 0 || value.includes('\0')) return null;
  const candidate = path.resolve(APP_ROOT, value);
  if (path.extname(candidate).toLowerCase() !== '.mp3') return null;
  let rootReal;
  let fileReal;
  try {
    rootReal = fs.realpathSync.native(AUDIO_ROOT);
    fileReal = fs.realpathSync.native(candidate);
  } catch {
    return null;
  }
  const relative = path.relative(rootReal, fileReal);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return null;
  return fileReal;
}

app.get('/', (request, response) => response.sendFile(PAGE_PATH));

app.get('/api/events', (request, response) => {
  response.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
  response.setHeader('Cache-Control', 'no-cache, no-transform');
  response.setHeader('Connection', 'keep-alive');
  response.flushHeaders();
  eventClients.add(response);
  response.write(': connected\n\n');
  response.on('close', () => eventClients.delete(response));
});

app.get('/api/audio', (request, response) => {
  const filePath = safeAudioPath(request.query.path);
  if (!filePath) return response.status(404).json({ error: 'audio file not found' });
  return response.sendFile(filePath);
});

app.post('/api/run', (request, response) => {
  if (activeRun) return response.status(409).json({ error: 'a run is already active' });

  let args;
  try {
    args = buildRunArgs(request.body);
  } catch (error) {
    return response.status(400).json({ error: error.message });
  }

  const python = pythonExePath();
  const spawnArgs = ['-u', 'narrate.py', 'run', ...args];
  let child;
  try {
    child = spawn(python, spawnArgs, {
      cwd: APP_ROOT,
      shell: false,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (error) {
    return response.status(500).json({ error: `could not start Python: ${error.message}` });
  }

  activeRun = { child, closed: false, cancelRequested: false, lines: null, cancelPromise: null };
  attachRun(activeRun);
  return response.status(202).json({ started: true });
});

app.post('/api/cancel', async (request, response) => {
  if (!activeRun) return response.json({ cancelled: false });
  await cancelRun(activeRun);
  return response.json({ cancelled: true });
});

function start(port = PORT) {
  return app.listen(port, () => console.log(`Narrator server listening on http://localhost:${port}`));
}

if (require.main === module) start();

module.exports = { app, start, buildRunArgs, pythonExePath, safeAudioPath };
