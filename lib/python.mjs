import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';

export function environmentPython(root) {
  return path.join(root, 'engine', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
}

export function findPython(root, requested, { allowSystem = false } = {}) {
  const candidates = [requested || process.env.LORA1_PYTHON, environmentPython(root)].filter(Boolean);
  if (allowSystem) {
    if (process.platform === 'win32') {
      for (const name of ['python', 'python3']) {
        const found = spawnSync('where.exe', [name], { encoding: 'utf8', windowsHide: true });
        if (found.status === 0) candidates.push(...found.stdout.trim().split(/\r?\n/).filter(p => !p.includes('WindowsApps')));
      }
      const launched = spawnSync('py', ['-3', '-c', 'import sys; print(sys.executable)'], { encoding: 'utf8', windowsHide: true, timeout: 15000 });
      if (launched.status === 0) candidates.push(launched.stdout.trim());
    } else candidates.push('python3', 'python');
  }
  for (const candidate of [...new Set(candidates)]) {
    if (path.isAbsolute(candidate) && !existsSync(candidate)) continue;
    const result = spawnSync(candidate, ['-c', 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)'], { encoding: 'utf8', windowsHide: true, timeout: 15000 });
    if (result.status === 0) return result.stdout.trim();
  }
  throw new Error('Lora 1 needs Python 3.11 or newer. Install Python, then run npm run setup -- --python <python-executable>.');
}
