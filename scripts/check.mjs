import { readdir } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const walk = async dir => (await Promise.all((await readdir(dir, { withFileTypes: true })).map(e => e.isDirectory() ? walk(path.join(dir, e.name)) : path.join(dir, e.name)))).flat();
const files = ['server.mjs', ...(await Promise.all(['public','lib','scripts','tests'].map(d => walk(path.join(root, d))))).flat().filter(f => /\.(mjs|js)$/.test(f))];
for (const file of files) { const result = spawnSync(process.execPath, ['--check', file], { cwd: root, encoding: 'utf8', windowsHide: true }); if (result.status !== 0) { process.stderr.write(result.stderr); process.exit(1); } }
console.log(`${files.length} JavaScript modules passed syntax checks.`);
