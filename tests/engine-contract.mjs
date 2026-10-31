import assert from 'node:assert/strict';
const base = process.env.LORA1_TEST_URL || 'http://127.0.0.1:5173';
const spec = await (await fetch(base + '/openapi.json')).json();
const required = {
  '/api/ping': ['get'], '/api/dashboard': ['get'], '/api/overview': ['get'], '/api/health': ['get'],
  '/api/analytics/downloads-series': ['get'], '/api/search': ['get'], '/api/users': ['get', 'post'],
  '/api/users/preview': ['post'], '/api/users/import': ['post'], '/api/users/{user_id}': ['get', 'patch', 'delete'],
  '/api/users/{user_id}/scan': ['post'], '/api/users/{user_id}/download-all/preview': ['get'],
  '/api/users/{user_id}/download-all': ['post'], '/api/users/{user_id}/posts': ['get'], '/api/users/{user_id}/logs': ['get'],
  '/api/downloads': ['get'], '/api/downloads/url': ['post'], '/api/downloads/bulk-delete': ['post'],
  '/api/downloads/{download_id}/media': ['get'], '/api/downloads/{download_id}/download': ['get'], '/api/downloads/{download_id}/telegram': ['post'],
  '/api/queue': ['get'], '/api/queue/counts': ['get'], '/api/queue/{job_id}/retry': ['post'], '/api/queue/{job_id}/pause': ['post'],
  '/api/queue/{job_id}/resume': ['post'], '/api/queue/{job_id}/cancel': ['post'], '/api/queue/pause-all': ['post'],
  '/api/queue/resume-all': ['post'], '/api/queue/clear': ['post'], '/api/queue/clear-completed': ['post'], '/api/queue/bulk-remove': ['post'],
  '/api/settings': ['get', 'put'], '/api/storage': ['get', 'put'], '/api/mega': ['get', 'put'],
  '/api/mega/connect': ['post'], '/api/mega/disconnect': ['post'], '/api/mega/sync': ['get', 'post'], '/api/mega/sync/use-device': ['post'], '/api/mega/clear-cache': ['post'],
  '/api/cookies': ['get'], '/api/cookies/upload': ['post'], '/api/cookies/{profile_id}/activate': ['post'], '/api/cookies/{profile_id}': ['delete'],
  '/api/instagram/cookies': ['get', 'post', 'delete'], '/api/instagram/monitor': ['post'], '/api/instagram/download': ['post'], '/api/instagram/preview': ['get'],
  '/api/x/cookies': ['get', 'post', 'delete'], '/api/x/monitor': ['post'], '/api/x/download': ['post'], '/api/x/preview': ['get'],
  '/api/workers': ['get'], '/api/workers/scan-now': ['post'], '/api/workers/scan-status': ['get'], '/api/workers/catch-up-now': ['post'],
  '/api/activity': ['get'], '/api/activity/delete': ['post'], '/api/logs': ['get'], '/api/logs/live': ['get'], '/api/logs/export': ['get'],
  '/api/notifications': ['get'], '/api/notifications/read': ['post'], '/api/notifications/unread-count': ['get'],
  '/api/telegram/status': ['get'], '/api/telegram/users': ['get'], '/api/telegram/config': ['put'], '/api/reset': ['post'],
  '/api/downloads/import-metadata': ['post'], '/api/downloads/reconcile': ['post']
};
let operations = 0;
for (const [path, methods] of Object.entries(required)) for (const method of methods) { assert.ok(spec.paths[path]?.[method], `${method.toUpperCase()} ${path} is present in the unchanged backend`); operations++; }
const schema = name => spec.components.schemas[name];
assert.ok(schema('BulkDeleteRequest').properties.delete_file, 'Bulk deletion uses the backend’s delete_file flag');
const readPaths = ['/api/ping', '/api/dashboard', '/api/health', '/api/settings', '/api/users', '/api/downloads', '/api/queue', '/api/queue/counts', '/api/activity', '/api/logs', '/api/logs/live?limit=10', '/api/workers', '/api/workers/scan-status?platform=instagram', '/api/cookies', '/api/instagram/cookies', '/api/x/cookies', '/api/storage', '/api/mega', '/api/telegram/status', '/api/telegram/users', '/api/notifications', '/api/search?q=reference'];
const responses = await Promise.all(readPaths.map(async path => { const r = await fetch(base + path); assert.ok(r.ok, `${path} returns ${r.status}`); return r.json(); }));
assert.ok(Array.isArray(responses[4].items), 'Creators use the expected pagination envelope');
assert.ok(Array.isArray(responses[5].items), 'Downloads use the expected pagination envelope');
for (const media of responses[5].items) { const r = await fetch(base + `/api/downloads/${media.id}/media`); assert.ok(r.ok); const m = await r.json(); assert.ok(['image','video'].includes(m.type)); const path = m.type === 'image' ? m.images[0] : m.video_url; if (path) assert.ok((await fetch(base + path)).ok, `Media ${media.id} can be played`); }
console.log(`${operations} API operations verified against Lora 1 OpenAPI; ${readPaths.length} live read endpoints and ${responses[5].items.length} media previews passed.`);
