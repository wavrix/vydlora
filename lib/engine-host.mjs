import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { findPython } from './python.mjs';
import { engineAuthHeaders } from './engine-protocol.mjs';

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
export class EngineHost {
  constructor({ root, url, token = '', secure = true }) {
    this.root = root; this.url = new URL(url); this.token = token; this.secure = secure;
    this.child = null;
  }
  get owned() { return Boolean(this.child && this.child.exitCode === null); }
  async responds() {
    try { const result = await fetch(new URL('/api/ping', this.url), { headers: engineAuthHeaders(this.token), signal: AbortSignal.timeout(1500) }); return result.ok || result.status === 401; }
    catch { return false; }
  }
  async start() {
    if (await this.responds()) return { managed: false };
    if (!['localhost', '127.0.0.1', '[::1]'].includes(this.url.hostname) || this.url.protocol !== 'http:') return { managed: false };
    const python = findPython(this.root);
    if (this.secure && !this.token) this.token = randomBytes(32).toString('hex');
    const data = path.resolve(this.root, process.env.LORA1_DATA_DIR || '.runtime/lora1');
    await mkdir(data, { recursive: true });
    this.child = spawn(python, [path.join(this.root, 'engine', 'bootstrap.py')], {
      cwd: path.join(this.root, 'engine'), stdio: ['ignore', 'inherit', 'inherit'], windowsHide: true,
      env: { ...process.env, LORA1_DATA_DIR: data, LORA1_HOST: this.url.hostname.replace(/^\[|\]$/g, ''), LORA1_PORT: this.url.port || '80', LORA1_AUTH_TOKEN: this.token, LORA1_MANAGED: '1' },
    });
    let failure;
    this.child.once('error', error => { failure = error; });
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      if (failure || this.child.exitCode !== null) throw new Error(`Lora 1 could not start. ${failure?.message || 'Run npm run setup and check the engine output.'}`);
      if (await this.responds()) return { managed: true };
      await pause(250);
    }
    await this.stop();
    throw new Error('Lora 1 did not become ready. Check the engine output and try again.');
  }
  async stop() {
    const child = this.child;
    if (!child || child.exitCode !== null) return;
    try { await fetch(new URL('/__engine/shutdown', this.url), { method: 'POST', headers: engineAuthHeaders(this.token), signal: AbortSignal.timeout(2000) }); } catch { /* Process may already be exiting. */ }
    const deadline = Date.now() + 10000;
    while (child.exitCode === null && Date.now() < deadline) await pause(100);
    if (child.exitCode === null) child.kill();
    this.child = null;
  }
  async restart() {
    if (!this.owned) throw new Error('This Lora 1 engine is managed separately. Restart its process to open the selected account.');
    await this.stop();
    return this.start();
  }
}
