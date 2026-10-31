import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { esc, bytes, button } from '../public/ui.js';
const storage = new Map();
before(() => { Object.defineProperty(globalThis, 'localStorage', { value: { getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) }, configurable: true }); if (!globalThis.crypto) globalThis.crypto = webcrypto; });
test('escapes untrusted creator names and error messages before HTML rendering', () => {
  assert.equal(esc('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  assert.match(button('<delete>', 'x" onclick="bad'), /data-action="x&quot; onclick=&quot;bad"/);
  assert.match(button('Preview', 'preview'), /type="button"/);
  assert.equal(bytes(1024), '1.0 KB');
});
test('PINs are salted, hashed and current-PIN authenticated; failed attempts survive reload', async () => {
  const security = await import('../public/security.js');
  await security.setPin('4826', '4826', ''); assert.equal(security.hasPin(), true);
  const record = storage.get('vydlora.security.v1'); assert.doesNotMatch(record, /4826/);
  assert.equal(await security.verifyPin('4826'), true);
  await assert.rejects(security.setPin('7159', '7159', '0000'), /Incorrect/);
  for (let i = 0; i < 4; i++) await assert.rejects(security.verifyPin('0000'));
  await assert.rejects(security.verifyPin('4826'), /Try again/);
  const saved = JSON.parse(storage.get('vydlora.security.v1')); assert.ok(saved.retryAfter > Date.now());
  saved.retryAfter = 0; storage.set('vydlora.security.v1', JSON.stringify(saved));
  await security.removePin('4826'); assert.equal(security.hasPin(), false);
});
test('custom shortcuts reject ambiguous duplicate key bindings', async () => {
  const security = await import('../public/security.js');
  assert.throws(() => security.saveShortcuts({ search: 'Control+k', add: 'Control+k' }), /unique/);
  security.saveShortcuts({ ...security.shortcutDefaults, search: 'Alt+k' }); assert.equal(security.shortcuts().search, 'Alt+k');
});
