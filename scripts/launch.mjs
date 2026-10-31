import path from 'node:path';
import { loadEnvironment } from '../lib/environment.mjs';
import { localAppOrigin, isVydloraRunning, waitForEngine, openAppBrowser } from '../lib/launch.mjs';

const root = path.resolve(import.meta.dirname, '..');
await loadEnvironment(root);
const origin = localAppOrigin(process.env.PORT || 5173);
if (await isVydloraRunning(origin)) {
  console.log(`Vydlora is already running at ${origin}`);
} else {
  const { startServer } = await import('../server.mjs');
  await startServer();
  console.log('Keep this terminal open. Press Ctrl+C here to stop Vydlora.');
}
if (!await waitForEngine(origin)) console.log('The web app is open, but Lora 1 is not ready. Check the engine output or Settings → Connection.');
if (!process.argv.includes('--no-browser')) openAppBrowser(origin);
