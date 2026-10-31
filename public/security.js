const key = 'vydlora.security.v1';
const iterations = 210000;
const encoder = new TextEncoder();
const to64 = bytes => btoa(String.fromCharCode(...bytes));
const from64 = value => Uint8Array.from(atob(value), c => c.charCodeAt(0));
function read() { const value = localStorage.getItem(key); if (!value) return null; const data = JSON.parse(value); if (!data.salt || !data.hash || data.iterations !== iterations) throw new Error('PIN settings could not be read.'); return data; }
async function hash(pin, salt) {
  const material = await crypto.subtle.importKey('raw', encoder.encode(pin), 'PBKDF2', false, ['deriveBits']);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', salt, iterations, hash: 'SHA-256' }, material, 256));
}
export const hasPin = () => Boolean(localStorage.getItem(key));
export async function verifyPin(pin) {
  const data = read(); if (!data) throw new Error('Set a PIN in Security settings first.');
  if (data.retryAfter > Date.now()) throw new Error(`Try again in ${Math.ceil((data.retryAfter - Date.now()) / 1000)} seconds.`);
  if (!/^\d{4}$/.test(pin)) throw new Error('Enter your four-digit PIN.');
  const actual = await hash(pin, from64(data.salt)), expected = from64(data.hash);
  let difference = actual.length ^ expected.length;
  for (let i = 0; i < actual.length; i++) difference |= actual[i] ^ expected[i];
  if (difference) { data.failures = (data.failures || 0) + 1; if (data.failures >= 5) { data.failures = 0; data.retryAfter = Date.now() + 30000; } localStorage.setItem(key, JSON.stringify(data)); throw new Error(data.retryAfter > Date.now() ? 'Too many attempts. Try again in 30 seconds.' : 'Incorrect PIN. Try again.'); }
  data.failures = 0; data.retryAfter = 0; localStorage.setItem(key, JSON.stringify(data)); return true;
}
export async function setPin(pin, confirmation, current) {
  if (!/^\d{4}$/.test(pin)) throw new Error('Use exactly four digits.');
  if (pin !== confirmation) throw new Error('The PINs do not match.');
  if (hasPin()) await verifyPin(current);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  localStorage.setItem(key, JSON.stringify({ iterations, salt: to64(salt), hash: to64(await hash(pin, salt)), failures: 0, retryAfter: 0 }));
}
export async function removePin(current) { await verifyPin(current); localStorage.removeItem(key); }
export const shortcutDefaults = { search: 'Control+k', add: 'Control+n', import: 'Control+i', export: 'Control+e', sidebar: 'Control+b', terminal: 'Control+`', refresh: 'Control+Shift+r', shortcuts: 'Control+/', lock: 'Control+Shift+l' };
export const shortcutNames = { search: 'Search workspace', add: 'Add creator', import: 'Import creators', export: 'Export creators', sidebar: 'Toggle sidebar', terminal: 'Open terminal', refresh: 'Refresh view', shortcuts: 'Keyboard shortcuts', lock: 'Lock Vydlora' };
export function shortcuts() { try { return { ...shortcutDefaults, ...JSON.parse(localStorage.getItem('vydlora.shortcuts') || '{}') }; } catch { return { ...shortcutDefaults }; } }
export function saveShortcuts(values) { const assigned = Object.values(values).filter(Boolean); if (new Set(assigned).size !== assigned.length) throw new Error('Each shortcut must be unique.'); localStorage.setItem('vydlora.shortcuts', JSON.stringify(values)); }
export function chord(event) { return [event.ctrlKey || event.metaKey ? 'Control' : '', event.altKey ? 'Alt' : '', event.shiftKey ? 'Shift' : '', event.key.toLowerCase()].filter(Boolean).join('+'); }
