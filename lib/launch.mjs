import { spawn } from 'node:child_process';

export function localAppOrigin(port = 5173) {
  const value = Number(port);
  if (!Number.isInteger(value) || value < 1 || value > 65535) throw new Error('PORT must be a number from 1 to 65535.');
  return `http://127.0.0.1:${value}`;
}

export async function isVydloraRunning(origin, fetcher = fetch) {
  try {
    const reply = await fetcher(origin + '/__vydlora/connection', { signal: AbortSignal.timeout(1500) });
    if (!reply.ok) return false;
    const data = await reply.json();
    return typeof data.base_url === 'string' && typeof data.token_set === 'boolean' && typeof data.managed === 'boolean';
  } catch { return false; }
}

export async function waitForEngine(origin, timeout = 15000) {
  const deadline = Date.now() + timeout;
  do {
    try {
      const reply = await fetch(origin + '/api/ping', { signal: AbortSignal.timeout(1500) });
      if (reply.ok && (await reply.json()).ok === true) return true;
    } catch { }
    await new Promise(resolve => setTimeout(resolve, 250));
  } while (Date.now() < deadline);
  return false;
}

export function openAppBrowser(origin) {
  const command = process.platform === 'win32' ? 'explorer.exe' : process.platform === 'darwin' ? 'open' : 'xdg-open';
  const child = spawn(command, [origin], { stdio: 'ignore', windowsHide: true, detached: true });
  child.on('error', () => console.log(`Open ${origin} in your browser.`));
  child.unref();
}
