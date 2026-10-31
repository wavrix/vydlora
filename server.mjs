import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import tls from 'node:tls';
import { readFile, stat } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEnvironment } from './lib/environment.mjs';
import { EngineHost } from './lib/engine-host.mjs';
import { engineAuthHeaders, ENGINE_TOKEN_HEADER, LEGACY_TOKEN_HEADER } from './lib/engine-protocol.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
await loadEnvironment(root);

let backend = new URL(process.env.LORA1_ENGINE_URL || 'http://127.0.0.1:8000');
let token = process.env.LORA1_AUTH_TOKEN || '';
const port = Number(process.env.PORT || 5173);
const publicDir = path.join(root, 'public');
let engineHost;
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.woff2': 'font/woff2', '.ico': 'image/x-icon' };

export function validateBackend(value, current = backend) {
  const next = new URL(value);
  if (!['http:', 'https:'].includes(next.protocol) || next.username || next.password || next.search || next.hash || !['', '/'].includes(next.pathname)) throw new Error('Enter an HTTP or HTTPS engine origin, without a path or credentials.');
  const local = ['127.0.0.1', 'localhost', '[::1]'].includes(next.hostname);
  if (!local && next.origin !== current.origin) throw new Error('For a remote engine, set LORA1_ENGINE_URL in .env and restart Vydlora.');
  return next;
}

function json(res, status, data) { res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' }); res.end(JSON.stringify(data)); }
function sameOrigin(req) {
  if (req.headers['sec-fetch-site'] === 'cross-site') return false;
  return !req.headers.origin || req.headers.origin === `http://${req.headers.host}`;
}
function localHost(req) { return ['127.0.0.1', 'localhost', '[::1]'].some(name => req.headers.host === `${name}:${req.socket.localPort}` || req.headers.host === name); }
async function body(req) {
  let value = '';
  for await (const chunk of req) { value += chunk; if (value.length > 8192) throw new Error('Request too large.'); }
  return JSON.parse(value || '{}');
}

export const server = http.createServer(async (req, res) => {
  try {
    if (!/^\/(?!\/)/.test(req.url || '')) return json(res, 400, { detail: 'Use a relative application path.' });
    const url = new URL(req.url, 'http://localhost');
    if (!localHost(req)) return json(res, 403, { detail: 'This hostname is not configured for Vydlora.' });
    if (!sameOrigin(req)) return json(res, 403, { detail: 'Cross-site requests are not allowed.' });
    if (url.pathname === '/__vydlora/engine/restart' && req.method === 'POST') {
      if (!engineHost?.owned || backend.origin !== engineHost.url.origin) return json(res, 409, { detail: 'Restart the separately managed Lora 1 process to open the selected account.' });
      await engineHost.restart();
      token = engineHost.token;
      return json(res, 200, { ok: true, detail: 'Lora 1 engine restarted.' });
    }
    if (url.pathname === '/__vydlora/connection/test' && req.method === 'POST') {
      const input = await body(req);
      const target = validateBackend(input.base_url);
      const candidateToken = input.token || token;
      try {
        const response = await fetch(new URL('/api/ping', target), { headers: engineAuthHeaders(candidateToken), signal: AbortSignal.timeout(7000) });
        const result = await response.json();
        return json(res, response.ok ? 200 : response.status, result);
      } catch { return json(res, 502, { detail: 'Could not connect to that Lora 1 engine. Check its address and make sure it is running.' }); }
    }
    if (url.pathname === '/__vydlora/connection') {
      if (req.method === 'PUT') {
        const input = await body(req);
        const next = validateBackend(input.base_url);
        if (input.token !== undefined) token = String(input.token);
        backend = next;
      } else if (req.method !== 'GET') return json(res, 405, { detail: 'Method not allowed.' });
      return json(res, 200, { base_url: backend.origin, token_set: Boolean(token), managed: Boolean(engineHost?.owned && engineHost.url.origin === backend.origin) });
    }
    if (url.pathname.startsWith('/api/') || url.pathname === '/openapi.json') {
      const connection = { url: backend, token };
      const target = new URL(url.pathname + url.search, connection.url);
      const headers = { ...req.headers, host: target.host };
      delete headers.origin;
      delete headers.referer;
      delete headers.cookie;
      delete headers[ENGINE_TOKEN_HEADER]; delete headers[LEGACY_TOKEN_HEADER];
      delete headers.authorization;
      Object.assign(headers, engineAuthHeaders(connection.token));
      const upstream = (target.protocol === 'https:' ? https : http).request(target, { method: req.method, headers }, response => {
        const outHeaders = { ...response.headers, 'cache-control': 'no-store' };
        delete outHeaders['set-cookie'];
        res.writeHead(response.statusCode, outHeaders);
        response.pipe(res);
      });
      upstream.on('error', () => { if (!res.headersSent) json(res, 502, { detail: 'Lora 1 is not available. Start the engine or check Connection in Settings.', backend_unavailable: true }); else res.destroy(); });
      req.on('aborted', () => upstream.destroy());
      res.on('close', () => { if (!res.writableEnded) upstream.destroy(); });
      req.pipe(upstream);
      return;
    }
    if (url.pathname === '/healthz' && req.method === 'GET') return json(res, 200, { ok: true });
    if (/^\/(auth|billing|__vydlora\/platform)(\/|$)/.test(url.pathname)) return json(res, 404, { detail: 'Not found.' });
    if (['/signin','/signup','/forgot-password','/reset-password','/pricing','/privacy','/terms'].includes(url.pathname)) { res.writeHead(302, { location: '/' }); return res.end(); }
    if (!['GET', 'HEAD'].includes(req.method)) return json(res, 405, { detail: 'Method not allowed.' });
    const filename = path.resolve(publicDir, `.${decodeURIComponent(url.pathname)}`);
    if (filename !== publicDir && !filename.startsWith(publicDir + path.sep)) return json(res, 403, { detail: 'Not allowed.' });
    let file = filename;
    try { if (!(await stat(file)).isFile()) file = path.join(publicDir, 'index.html'); }
    catch { if (path.extname(url.pathname)) return json(res, 404, { detail: 'File not found.' }); file = path.join(publicDir, 'index.html'); }
    res.writeHead(200, {
      'content-type': types[path.extname(file)] || 'application/octet-stream',
      'cache-control': file.includes(`${path.sep}assets${path.sep}`) ? 'public, max-age=86400' : 'no-cache',
      'x-content-type-options': 'nosniff',
      'referrer-policy': 'same-origin',
      'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    });
    if (req.method === 'HEAD') return res.end();
    createReadStream(file).pipe(res);
  } catch (error) { if (!res.headersSent) json(res, error.status || 400, { detail: error.message, code: error.code || undefined }); else res.destroy(); }
});

server.on('upgrade', async (req, socket, head) => {
  if (req.url !== '/ws' || !(localHost(req) && sameOrigin(req))) return socket.destroy();
  const connection = { url: backend, token };
  const target = connection.url;
  const options = { host: target.hostname.replace(/^\[|\]$/g, ''), port: target.port || (target.protocol === 'https:' ? 443 : 80) };
  const upstream = target.protocol === 'https:' ? tls.connect({ ...options, servername: options.host }) : net.connect(options);
  upstream.once(target.protocol === 'https:' ? 'secureConnect' : 'connect', () => {
    const headers = { ...req.headers, host: target.host };
    delete headers.origin; delete headers.cookie; delete headers[ENGINE_TOKEN_HEADER]; delete headers[LEGACY_TOKEN_HEADER];
    delete headers.authorization;
    Object.assign(headers, engineAuthHeaders(connection.token));
    upstream.write(`GET /ws HTTP/1.1\r\n${Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join('\r\n')}\r\n\r\n`);
    if (head.length) upstream.write(head);
    socket.pipe(upstream).pipe(socket);
  });
  upstream.on('error', () => socket.destroy());
  socket.on('error', () => upstream.destroy());
  socket.on('close', () => upstream.destroy());
});

export async function startServer() {
  if (server.listening) return server;
  if (process.env.LORA1_MANAGE_ENGINE !== '0') {
    engineHost = new EngineHost({ root, url: backend, token });
    try { await engineHost.start(); token = engineHost.token; }
    catch (error) { console.error(error.message); }
  }
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, '127.0.0.1', () => { server.off('error', reject); console.log(`Vydlora       http://127.0.0.1:${port}\nLora 1 engine ${backend.origin}`); resolve(); });
    });
  } catch (error) { await engineHost?.stop(); throw error; }
  let closing = false;
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {
    if (closing) return; closing = true;
    await engineHost?.stop();
    server.closeAllConnections(); server.close(); process.exit(0);
  });
  return server;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await startServer();
