import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { findPython } from '../lib/python.mjs';
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const data = path.join(root, '.runtime', 'engine-tests');
await mkdir(data, { recursive: true }); await mkdir(path.join(root, 'engine', 'build'), { recursive: true });
const child = spawn(findPython(root), ['-m', 'unittest', 'discover', '-s', 'tests', '-v'], {
  cwd: path.join(root, 'engine'), stdio: 'inherit', windowsHide: true,
  env: { ...process.env, LORA1_DATA_DIR: data, LORA1_CLOUD_SYNC_DISABLED: '1', DATABASE_URL: 'sqlite:///' + path.join(data, 'test-default.db').replaceAll('\\','/'), COOKIES_FILE: path.join(data, 'cookies.txt'), DOWNLOADS_DIR: path.join(data, 'downloads') },
});
child.on('error', error => { console.error(error.message); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code || 0; });
