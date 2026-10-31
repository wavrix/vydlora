import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { findPython, environmentPython } from '../lib/python.mjs';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const args = process.argv.slice(2);
const pythonOption = args.indexOf('--python');
const python = findPython(root, pythonOption >= 0 ? args[pythonOption + 1] : undefined, { allowSystem: true });
const run = (file, parameters, env = process.env) => new Promise((resolve, reject) => {
  const child = spawn(file, parameters, { cwd: root, stdio: 'inherit', windowsHide: true, env });
  child.on('error', reject);
  child.on('exit', code => code === 0 ? resolve() : reject(new Error(`Setup stopped with exit code ${code}.`)));
});
await mkdir(path.join(root, '.runtime'), { recursive: true });
console.log('Preparing the project-local Lora 1 engine environment…');
const local = environmentPython(root);
if (!existsSync(local)) await run(python, ['-m', 'venv', path.join(root, 'engine', '.venv')]);
await run(local, ['-m', 'pip', 'install', '-r', path.join(root, 'engine', 'requirements.txt')]);
if (!args.includes('--skip-browser')) {
  const env = { ...process.env };
  if (process.platform === 'win32' && env.LOCALAPPDATA && !env.PLAYWRIGHT_BROWSERS_PATH) env.PLAYWRIGHT_BROWSERS_PATH = path.join(env.LOCALAPPDATA, 'ms-playwright');
  await run(local, ['-m', 'playwright', 'install', ...(args.includes('--with-browser-deps') ? ['--with-deps'] : []), 'chromium'], env);
}
console.log('Lora 1 is installed. Start Vydlora with npm start.');
