import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { once } from 'node:events';

let proxy, upstream, origin, lastRequest, wsHeaders;
const listen = async server => { server.listen(0, '127.0.0.1'); await once(server, 'listening'); return `http://127.0.0.1:${server.address().port}`; };
function request(path, { method = 'GET', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(origin + path, { method, headers }, res => { const data = []; res.on('data', d => data.push(d)); res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(data) })); });
    req.on('error', reject); if (body) req.write(body); req.end();
  });
}
before(async () => {
  upstream = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    lastRequest = { method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks) };
    if (req.url === '/api/file') { res.writeHead(206, { 'content-type': 'video/mp4', 'content-range': 'bytes 0-3/8' }); return res.end(Buffer.from([0, 1, 2, 255])); }
    res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ ok: true }));
  });
  upstream.on('upgrade', (req, socket) => {
    wsHeaders = req.headers;
    const accept = createHash('sha1').update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const payload = Buffer.from('{"type":"connected"}'); socket.write(Buffer.concat([Buffer.from([0x81, payload.length]), payload]));
    socket.on('data', () => socket.end());
  });
  process.env.LORA1_ENGINE_URL = await listen(upstream);
  process.env.LORA1_AUTH_TOKEN = 'test-server-token';
  ({ server: proxy } = await import('../server.mjs'));
  origin = await listen(proxy);
});
after(async () => { proxy.closeAllConnections(); upstream.closeAllConnections(); proxy.close(); upstream.close(); });

test('serves the application and deep links with a restrictive content policy', async () => {
  const result = await request('/settings/downloads');
  assert.equal(result.status, 200); assert.match(result.body.toString(), /<title>Vydlora/);
  assert.match(result.headers['content-security-policy'], /frame-ancestors 'none'/);
  assert.equal((await request('/missing-font.woff2')).status, 404);
});

test('local workspace opens directly and retired account and billing routes are unavailable', async () => {
  const home = await request('/');
  assert.equal(home.status,200);
  assert.match(home.body.toString(),/src="\/app.js"/);
  assert.doesNotMatch(home.body.toString(),/account.css|boot.js/);
  for(const page of ['/signin','/signup','/pricing','/forgot-password','/reset-password']) {
    const reply=await request(page);assert.equal(reply.status,302);assert.equal(reply.headers.location,'/');
  }
  for(const route of ['/auth/signup','/billing/checkout','/__vydlora/platform/session'])assert.equal((await request(route)).status,404);
  const settings=await request('/settings.js');assert.doesNotMatch(settings.body.toString(),/Account & billing|accountContext|accountSettings|daily_bytes/);
});
test('forwards JSON mutations, encodings and query strings while isolating credentials', async () => {
  const body = JSON.stringify({ nickname: '<creator> & 日本語' });
  const result = await request('/api/users/42?mode=all', { method: 'PATCH', headers: { 'content-type': 'application/json', 'x-lora1-token': 'untrusted', cookie: 'private-cookie=hidden' }, body });
  assert.equal(result.status, 200); assert.equal(lastRequest.method, 'PATCH'); assert.equal(lastRequest.url, '/api/users/42?mode=all'); assert.equal(lastRequest.body.toString(), body);
  assert.equal(lastRequest.headers['x-lora1-token'], 'test-server-token'); assert.equal(lastRequest.headers.cookie, undefined);
});
test('preserves media ranges and binary responses for playback and downloads', async () => {
  const result = await request('/api/file', { headers: { range: 'bytes=0-3' } });
  assert.equal(result.status, 206); assert.equal(result.headers['content-range'], 'bytes 0-3/8'); assert.deepEqual([...result.body], [0, 1, 2, 255]); assert.equal(lastRequest.headers.range, 'bytes=0-3');
});
test('blocks cross-site requests and DNS-rebinding hosts', async () => {
  assert.equal((await request('/api/settings', { method: 'PUT', headers: { origin: 'https://other.example' }, body: '{}' })).status, 403);
  assert.equal((await request('/api/ping', { headers: { host: 'attacker.example' } })).status, 403);
  assert.equal((await request('/api/ping', { headers: { 'sec-fetch-site': 'cross-site' } })).status, 403);
});

test('absolute and protocol-relative request targets cannot redirect private engine credentials', async () => {
  for (const target of ['http://169.254.169.254/api/ping', '//attacker.example/api/ping']) {
    const result = await new Promise((resolve, reject) => {
      const req = http.request(origin, { path: target }, res => { res.resume(); res.on('end',()=>resolve(res.statusCode)); });
      req.on('error',reject); req.end();
    });
    assert.equal(result,400);
  }
});
test('connection details never reveal the backend token and reject arbitrary remote destinations', async () => {
  const result = await request('/__vydlora/connection');
  const data = JSON.parse(result.body); assert.equal(data.token_set, true); assert.equal(data.token, undefined); assert.doesNotMatch(result.body.toString(), /test-server-token/);
  assert.equal((await request('/__vydlora/connection', { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ base_url: 'http://169.254.169.254' }) })).status, 400);
  const preview = await request('/__vydlora/connection/test', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ base_url: process.env.LORA1_ENGINE_URL, token: 'candidate-token' }) });
  assert.equal(preview.status, 200); assert.equal(lastRequest.headers['x-lora1-token'], 'candidate-token');
  await request('/api/ping'); assert.equal(lastRequest.headers['x-lora1-token'], 'test-server-token', 'Testing a connection does not replace the saved token');
});
test('proxies live WebSocket updates with the backend token', async () => {
  const ws = new WebSocket(origin.replace('http:', 'ws:') + '/ws');
  const message = await new Promise((resolve, reject) => { ws.onmessage = e => resolve(e.data); ws.onerror = reject; });
  assert.deepEqual(JSON.parse(message), { type: 'connected' }); assert.equal(wsHeaders['x-lora1-token'], 'test-server-token'); ws.close(); await once(ws, 'close');
});
test('reports an unavailable backend as an actionable error', async () => {
  upstream.closeAllConnections(); await new Promise(resolve => upstream.close(resolve));
  const result = await request('/api/ping'); const error = JSON.parse(result.body);
  assert.equal(result.status, 502); assert.equal(error.backend_unavailable, true); assert.match(error.detail, /Start the engine/);
});
