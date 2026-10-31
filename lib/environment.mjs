import { readFile } from 'node:fs/promises';
import path from 'node:path';

export async function loadEnvironment(root) {
  try {
    for (const line of (await readFile(path.join(root, '.env'), 'utf8')).split(/\r?\n/)) {
      const match = line.match(/^([A-Z][A-Z0-9_]*)\s*=\s*(.*)$/);
      if (match && process.env[match[1]] === undefined) process.env[match[1]] = match[2].trim().replace(/^['"]|['"]$/g, '');
    }
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  // Configuration compatibility, never product display names.
  const aliases = { LORA1_ENGINE_URL: 'WEVDL_BACKEND_URL', LORA1_AUTH_TOKEN: 'WEVDL_AUTH_TOKEN', LORA1_DATA_DIR: 'SOLSTICE_DATA_DIR', LORA1_CLOUD_SYNC_DISABLED: 'WEVDL_CLOUD_SYNC_DISABLED' };
  for (const [current, legacy] of Object.entries(aliases)) if (process.env[current] === undefined && process.env[legacy] !== undefined) process.env[current] = process.env[legacy];
}
