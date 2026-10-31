import { api, post, put, del, upload } from './api.js';
import { esc, icon, button, iconButton, badge, count, bytes, time, field, toggle, empty, toast, modal, closeModal } from './ui.js';
import { hasPin, setPin, removePin, shortcuts, shortcutNames, shortcutDefaults, saveShortcuts } from './security.js';

export const categories = [ ['overview', 'Overview', 'grid'], ['connection', 'Connection', 'wifi'], ['downloads', 'Downloads', 'download'], ['monitoring', 'Monitoring', 'monitor'], ['cookies', 'Cookies', 'shield'], ['telegram', 'Telegram', 'telegram'], ['storage', 'Storage & MEGA', 'cloud'], ['security', 'Security', 'lock'], ['appearance', 'Appearance & shortcuts', 'sun'], ['maintenance', 'Maintenance', 'settings']];
const settingsGroups = {
  downloads: [
    ['Download engine', 'Tune parallel transfers and request limits.', [
      ['download_workers', 'Parallel download workers', 1, 64, 1], ['request_timeout', 'Request timeout (seconds)', 1, 120, 1],
      ['tikdownloader_min_interval', 'Minimum request interval (seconds)', 0, 60, 0.1], ['tikdownloader_max_interval', 'Maximum request interval (seconds)', 1, 300, 0.1], ['tikdownloader_max_retries', 'Maximum retries', 1, 10, 1]
    ]]
  ]
};
const card = (title, description, content, extra = '') => `<section class="settings-card ${extra}"><div class="card-heading"><h3>${esc(title)}</h3>${description ? `<p>${esc(description)}</p>` : ''}</div>${content}</section>`;
const actions = label => `<div class="form-actions"><span class="form-feedback" role="status"></span><button type="submit" class="button primary">${label}</button></div>`;
const statusRow = (label, value) => `<div class="detail-row"><span>${esc(label)}</span><strong>${esc(value ?? '-')}</strong></div>`;
let loaded = {};

function monitoringContent(settings) {
  const number = (name, label, min, max, step = 1) => field(label, name, settings[name], { type: 'number', min, max, step, required: true });
  const grid = (...controls) => `<div class="fields-grid">${controls.join('')}</div>`;
  const group = (title, controls, description = '') => `<section class="monitoring-group"><h4>${esc(title)}</h4>${description ? `<p class="help-text">${esc(description)}</p>` : ''}${controls}</section>`;
  return card('General', 'Shared controls for TikTok, Instagram, and X.',
    toggle('Pause all monitoring', 'monitoring_paused', settings.monitoring_paused, 'Pauses scheduled checks on all three platforms and holds queued monitoring downloads. Manual and Telegram downloads continue.')) +
    card('TikTok', 'Checks enabled TikTok creators automatically while monitoring is not paused.',
      group('Posts', grid(
        number('scan_interval', 'Scan interval (seconds)', 5, 86400),
        number('max_posts_per_scan', 'Posts per scan', 1, 50),
        number('scan_concurrency', 'Concurrent creator scans', 1, 20))) +
      group('Creator pacing', grid(
        number('scan_spacing_min', 'Minimum creator spacing (seconds)', 0, 120, 0.1),
        number('scan_spacing_max', 'Maximum creator spacing (seconds)', 0, 120, 0.1)), 'Applies when concurrent creator scans is set to 1.') +
      group('Stories',
        toggle('Monitor stories', 'monitor_stories', settings.monitor_stories, 'Check active TikTok stories on a separate schedule. Requires an active TikTok cookie session.') +
        grid(number('story_scan_interval', 'Story scan interval (seconds)', 300, 86400))) +
      group('Offline recovery',
        toggle('Catch up after going offline', 'catch_up_enabled', settings.catch_up_enabled, 'Checks for missed TikTok posts at startup and when your connection returns. Includes active stories when story monitoring is enabled.'))) +
    card('Instagram', 'Uses your Instagram cookie session and its own monitoring schedule.',
      toggle('Enable Instagram monitoring', 'instagram_monitor_enabled', settings.instagram_monitor_enabled, 'Runs scheduled checks for enabled Instagram creators while global monitoring is not paused.') +
      grid(number('instagram_scan_interval', 'Scan interval (seconds)', 60, 86400),
        number('instagram_max_posts_per_scan', 'Posts per scan', 1, 50),
        number('instagram_scan_concurrency', 'Concurrent scans', 1, 6),
        number('instagram_sleep', 'Request spacing (seconds)', 0, 60, 0.1))) +
    card('X', 'Uses your X cookie session and its own monitoring schedule.',
      toggle('Enable X monitoring', 'x_monitor_enabled', settings.x_monitor_enabled, 'Runs scheduled checks for enabled X creators while global monitoring is not paused.') +
      grid(number('x_scan_interval', 'Scan interval (seconds)', 60, 86400),
        number('x_max_posts_per_scan', 'Posts per scan', 1, 50)));
}

export async function settingsContent(category) {
  if (settingsGroups[category] || category === 'monitoring') {
    loaded = await api('/api/settings');
    const content = category === 'monitoring' ? monitoringContent(loaded) : settingsGroups[category].map(([title, description, fields]) => card(title, description, `<div class="fields-grid">${fields.map(([name, label, min, max, step]) => min === 'bool' ? toggle(label, name, loaded[name]) : field(label, name, loaded[name], { type: 'number', min, max, step, required: true })).join('')}</div>`)).join('');
    return `<form data-form="settings" data-category="${category}">${content}${actions('Save changes')}</form>`;
  }
  if (category === 'connection') {
    const data = await api('/__vydlora/connection');
    return card('Lora 1 engine', 'Connect this workspace to your existing Lora 1 service.', `<form data-form="connection">${field('Engine address', 'base_url', data.base_url, { type: 'url', required: true })}${field('Access token', 'token', '', { type: 'password', placeholder: data.token_set ? 'Token configured. Leave blank to keep it.' : 'Optional for development' })}<p class="help-text">The token stays in the local server’s memory. Save permanent connection details in your local .env file.</p><div class="form-actions">${button('Test connection', 'test-connection', { icon: 'wifi' })}<button class="button primary" type="submit">Connect</button></div></form>`);
  }
  if (category === 'cookies') {
    const [tt, ig, x] = await Promise.all([api('/api/cookies'), api('/api/instagram/cookies'), api('/api/x/cookies')]);
    return card('TikTok cookie profiles', 'Upload Netscape-format .txt cookies. Activate the session you want Lora 1 to use.', `<div class="cookie-list">${tt.length ? tt.map(c => `<div class="cookie-item"><div>${icon('shield')}<span><strong>${esc(c.name)}</strong><small>${count(c.cookie_count)} cookies · ${c.authenticated ? 'Signed-in session' : 'No signed-in session'} · ${count(c.rate_limit_hits)} rate-limit hits</small></span></div><div class="row-actions">${c.active ? badge('active') : button('Activate', `cookie-activate:${c.id}`)}${iconButton('Remove cookie profile', `cookie-delete:${c.id}`, 'trash')}</div></div>`).join('') : '<p class="muted">No TikTok cookie profiles yet.</p>'}</div>${uploadControl('Upload TikTok cookies', 'tiktok')}`) + [['instagram', ig, 'Instagram'], ['x', x, 'X']].map(([platform, state, title]) => card(title, state.message || 'A signed-in cookie session enables private media and creator monitoring.', `<div class="inline-status">${badge(state.present ? 'connected' : 'not connected')}<span>${count(state.cookie_count || 0)} cookies</span>${state.present ? button('Remove cookies', `platform-cookie-delete:${platform}`, { kind: 'danger subtle' }) : ''}</div>${uploadControl(`Upload ${title} cookies`, platform)}`)).join('');
  }
  if (category === 'storage') {
    const [storage, mega, settings] = await Promise.all([api('/api/storage'), api('/api/mega'), api('/api/settings')]);
    return card('Temporary storage', 'Lora 1 manages working files in private app data. Files awaiting upload are kept safe.', `<label class="field"><span>Download working folder</span><input value="${esc(storage.downloads_dir)}" readonly aria-label="Download working folder"></label>${statusRow('Folder status', storage.exists ? 'Available' : 'Created when needed')}<div class="button-row">${button('Copy path', `copy:${storage.downloads_dir}`, { icon: 'copy' })}${button('Clear MEGA cache', 'mega-cache', { icon: 'trash' })}</div>`) +
      card('MEGA cloud storage', 'Use Lora 1’s existing cloud connection and account profiles.', `<div class="inline-status">${icon('cloud')}${badge(mega.connected ? 'connected' : 'disconnected')}<span>${esc(mega.account)}</span></div>${!mega.installed ? '<p class="inline-notice">Install MEGAcmd on the engine machine to connect cloud storage.</p>' : ''}${mega.connected ? `<div class="storage-meter"><div style="width:${Math.min(100, (mega.storage_used / (mega.storage_total || 1)) * 100)}%"></div></div><p class="muted">${bytes(mega.storage_used)} of ${bytes(mega.storage_total)} used</p><form data-form="mega-options">${toggle('Enable cloud storage', 'enabled', mega.enabled)}${toggle('Keep local copies after upload', 'mega_keep_local_copies', settings.mega_keep_local_copies)}${field('Cloud folder', 'folder', mega.folder)}${actions('Save cloud settings')}</form><div class="detail-list">${statusRow('Sync', mega.sync?.state)}${statusRow('Last synced', time(mega.sync?.last_synced))}${statusRow('Pending uploads', mega.sync?.pending_uploads)}${statusRow('Primary device', mega.sync?.primary_device ? 'This device' : 'Another device')}</div>${mega.sync?.error ? `<p class="inline-notice">${esc(mega.sync.error)}</p>` : ''}<div class="button-row"><a class="button" href="https://mega.nz/fm" target="_blank" rel="noopener noreferrer">${icon('arrow')}Open MEGA</a>${button('Sync now', 'mega-sync', { icon: 'retry' })}${button('Use this device', 'mega-device', { icon: 'monitor' })}${button('Clear local cache', 'mega-cache')}${button('Disconnect', 'mega-disconnect', { kind: 'danger subtle' })}</div>` : ''}<details class="connection-details" ${mega.connected ? '' : 'open'}><summary>${mega.connected ? 'Switch MEGA account' : 'Connect an account'}</summary><form data-form="mega-connect">${field('Email address', 'email', '', { type: 'email', required: true })}${field('Password', 'password', '', { type: 'password', required: true })}<p class="help-text">Lora 1 exchanges the password for a revocable session. Switching accounts may require a engine restart.</p>${actions('Connect MEGA')}</form></details>`);
  }
  if (category === 'telegram') {
    const [state, users, settings] = await Promise.all([api('/api/telegram/status'), api('/api/telegram/users'), api('/api/settings')]);
    return card('Telegram bot', 'Download links, receive new posts, and manage creators from Telegram.', `<div class="inline-status">${icon('telegram')}${badge(state.running ? 'running' : 'stopped')}<strong>${esc(state.bot_username ? '@' + state.bot_username : 'No bot connected')}</strong></div><form data-form="telegram">${field('Bot token', 'bot_token', '', { type: 'password', placeholder: state.token_set ? `Configured ${state.token_hint}. Leave blank to keep.` : 'Token from @BotFather' })}${field('Approved chat IDs', 'allowed_ids', state.allowed_ids.join(', '), { help: 'Separate chat IDs with commas.' })}${toggle('Send newly monitored posts', 'telegram_send_new_posts', settings.telegram_send_new_posts)}${toggle('Upload link downloads', 'telegram_upload_link_downloads', settings.telegram_upload_link_downloads)}${toggle('Allow public link downloads', 'telegram_public_downloads', settings.telegram_public_downloads)}${actions('Save Telegram settings')}</form>${state.enabled ? button('Disconnect bot', 'telegram-disconnect', { kind: 'danger subtle' }) : ''}`) +
      card('Bot users', `${count(users.total)} users · ${count(users.approved)} approved · ${count(users.active_last_7d)} active this week`, users.users.length ? `<div class="table-scroll"><table><thead><tr><th>Chat</th><th>Access</th><th>Downloads</th><th>Messages</th><th></th></tr></thead><tbody>${users.users.map(u => `<tr><td><strong>${esc(u.username || u.chat_id)}</strong><small>${esc(u.chat_id)}</small></td><td>${badge(u.approved ? 'approved' : 'pending')}</td><td>${count(u.downloads)}</td><td>${count(u.messages)}</td><td>${button(u.approved ? 'Revoke' : 'Approve', `telegram-access:${u.chat_id}:${u.approved ? 'revoke' : 'approve'}`)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">Users appear here after messaging the bot.</p>') +
      card('Recent messages', `${count(state.requests_today)} requests today · Upload limit ${bytes(state.upload_limit_bytes)}`, state.recent.length ? state.recent.map(m => `<div class="feed-row">${icon('telegram')}<div><strong>${esc(m.text || m.command || 'Media message')}</strong><small>${esc(m.chat_id)} · ${time(m.created_at)}</small></div>${badge(m.handled ? 'handled' : 'pending')}</div>`).join('') : '<p class="muted">No messages yet.</p>');
  }
  if (category === 'security') return card('Workspace lock', 'A device-local PIN hides this interface while the engine keeps working.', `<form data-form="pin">${hasPin() ? field('Current PIN', 'current', '', { type: 'password', required: true }) : ''}${field('New four-digit PIN', 'pin', '', { type: 'password', required: true })}${field('Confirm PIN', 'confirmation', '', { type: 'password', required: true })}<p class="help-text">This lock protects the browser interface on this device. Engine authentication is configured separately in Connection.</p>${actions(hasPin() ? 'Change PIN' : 'Set PIN')}</form>${hasPin() ? `<div class="button-row">${button('Lock workspace', 'lock', { icon: 'lock' })}${button('Remove PIN', 'remove-pin', { kind: 'danger subtle' })}</div>` : ''}`);
  if (category === 'appearance') return card('Appearance', 'Use the same theme throughout your workspace.', `<div class="theme-options" role="group" aria-label="Color theme">${[['dark', 'Dark', 'moon'], ['light', 'Light', 'sun'], ['system', 'System', 'monitor']].map(([key, label, name]) => `<button class="theme-choice ${localStorage.getItem('vydlora.theme') === key || (!localStorage.getItem('vydlora.theme') && key === 'dark') ? 'selected' : ''}" data-action="theme:${key}">${icon(name)}${label}</button>`).join('')}</div>`) + card('Keyboard shortcuts', 'Click a shortcut and press a new key combination. Clear a field to disable it.', `<form data-form="shortcuts"><div class="shortcut-list">${Object.entries(shortcuts()).map(([id, value]) => `<label><span>${esc(shortcutNames[id])}</span><input name="${id}" value="${esc(value)}" data-shortcut-input autocomplete="off" spellcheck="false" aria-label="${esc(shortcutNames[id])}"></label>`).join('')}</div><div class="form-actions">${button('Restore defaults', 'shortcuts-reset')}<button type="submit" class="button primary">Save shortcuts</button></div></form>`);
  if (category === 'maintenance') return card('Library & recovery', 'Reconcile files already on disk or recover posts missed while offline.', `<div class="maintenance-row"><div><strong>Reconcile downloads</strong><p>Index existing media so it will not download again.</p></div>${button('Reconcile', 'reconcile', { icon: 'folder' })}</div><div class="maintenance-row"><div><strong>Catch up now</strong><p>Backfill missing posts and active stories.</p></div>${button('Start catch-up', 'catchup', { icon: 'retry' })}</div>`) + card('Import metadata', 'Import a previous Lora 1 export from a folder on the engine machine.', `<form data-form="metadata">${field('Export folder', 'directory', '', { required: true, placeholder: 'D:\\Backups\\Lora 1' })}${toggle('Monitor imported creators', 'monitored', false)}${actions('Import metadata')}</form>`) + card('Reset Lora 1', 'Permanently clear the engine database. This affects all clients connected to this engine.', `<p class="help-text">You can choose whether to remove downloaded files in the confirmation dialog.</p>${button('Reset data…', 'reset-dialog', { icon: 'trash', kind: 'danger' })}`, 'danger-card');
  const [health, workers] = await Promise.all([api('/api/health'), api('/api/workers')]);
  return card('System health', 'Live status from the Lora 1 engine.', `<div class="health-grid">${[['API', health.api], ['Database', health.database], ['Queue', health.queue], ['Telegram', health.telegram]].map(([label, state]) => `<div><span>${label}</span>${badge(state)}</div>`).join('')}</div><div class="detail-list">${statusRow('Workers online', `${health.workers_online} / ${health.workers_total}`)}${statusRow('Valid cookies', `${health.cookies_valid} / ${health.cookies_total}`)}${statusRow('Storage used', health.storage_used_human || bytes(health.storage_used))}${statusRow('Uptime', `${Math.floor(health.uptime_seconds / 3600)}h ${Math.floor(health.uptime_seconds % 3600 / 60)}m`)}</div>`) + card('Workers', 'The existing Lora 1 download and monitoring workers.', workers.length ? workers.map(w => `<div class="worker-row">${icon(w.kind === 'monitor' ? 'monitor' : 'download')}<div><strong>${esc(w.name)}</strong><small>${esc(w.current_job || w.kind)} · ${count(w.processed)} processed · ${count(w.failed)} failed</small></div>${badge(w.state)}</div>`).join('') : empty('No workers online', 'Start the Lora 1 engine to run workers.', 'monitor'));
}
function uploadControl(label, platform) { return `<label class="button upload-control">${icon('upload')}${esc(label)}<input type="file" accept=".txt" data-cookie-upload="${platform}" aria-label="${esc(label)}"></label>`; }
function values(form) { const result = {}; for (const input of form.elements) { if (!input.name) continue; result[input.name] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value; } return result; }
export async function submitSettings(form) {
  const data = values(form);
  switch (form.dataset.form) {
    case 'settings': {
      const changed = Object.fromEntries(Object.entries(data).filter(([k, v]) => loaded[k] !== v));
      const combined = { ...loaded, ...changed };
      if (combined.scan_spacing_min > combined.scan_spacing_max) throw new Error('Minimum creator spacing cannot exceed the maximum.');
      if (combined.tikdownloader_min_interval > combined.tikdownloader_max_interval) throw new Error('Minimum request interval cannot exceed the maximum.');
      await put('/api/settings', changed); break;
    }
    case 'connection': await put('/__vydlora/connection', { base_url: data.base_url, ...(data.token ? { token: data.token } : {}) }); await api('/api/ping'); break;
    case 'storage': await put('/api/storage', data); break;
    case 'mega-connect': { const result = await post('/api/mega/connect', { ...data, switch_account: true }); form.reset(); if (result.restart_required) { await post('/__vydlora/engine/restart'); toast('Lora 1 opened the selected account.'); } break; }
    case 'mega-options': await put('/api/mega', { enabled: data.enabled, folder: data.folder }); await put('/api/settings', { mega_keep_local_copies: data.mega_keep_local_copies }); break;
    case 'telegram': await put('/api/telegram/config', { allowed_ids: data.allowed_ids, ...(data.bot_token ? { bot_token: data.bot_token } : {}) }); await put('/api/settings', Object.fromEntries(Object.entries(data).filter(([k]) => k.startsWith('telegram_')))); form.querySelector('[name="bot_token"]').value = ''; break;
    case 'metadata': { const result = await post('/api/downloads/import-metadata', data); toast(`${result.imported} posts imported from ${result.creators} creators. ${result.skipped_existing} already existed.`); if (result.problems?.length) modal('Import results', `<div class="detail-list">${result.problems.map(p => `<p>${esc(p)}</p>`).join('')}</div>`); return true; }
    case 'pin': await setPin(data.pin, data.confirmation, data.current); form.reset(); break;
    case 'remove-pin': await removePin(data.current); if (form.isConnected) closeModal(); break;
    case 'shortcuts': saveShortcuts(data); break;
    default: return false;
  }
  toast('Changes saved'); return true;
}
export async function settingsAction(action, reload) {
  const [name, id, extra] = action.split(':');
  if (name === 'cookie-activate') { await post(`/api/cookies/${id}/activate`); }
  else if (name === 'test-connection') { const form = document.querySelector('[data-form="connection"]'); const result = await post('/__vydlora/connection/test', { base_url: form.elements.base_url.value, token: form.elements.token.value }); toast(`Connected to ${result.app} ${result.version}`); return true; }
  else if (name === 'mega-sync') { const result = await post('/api/mega/sync'); toast(result.error || `Cloud sync: ${result.state}`); }
  else if (name === 'mega-device') { await post('/api/mega/sync/use-device'); }
  else if (name === 'mega-cache') { const result = await post('/api/mega/clear-cache'); toast(result.detail); }
  else if (name === 'mega-disconnect') { await post('/api/mega/disconnect'); }
  else if (name === 'telegram-disconnect') { await put('/api/telegram/config', { bot_token: '' }); }
  else if (name === 'telegram-access') { const state = await api('/api/telegram/status'); const ids = new Set(state.allowed_ids.map(String)); extra === 'approve' ? ids.add(id) : ids.delete(id); await put('/api/telegram/config', { allowed_ids: [...ids].join(',') }); }
  else if (name === 'reconcile' || name === 'catchup') { const result = await post(name === 'reconcile' ? '/api/downloads/reconcile' : '/api/workers/catch-up-now'); toast(result.detail); }
  else if (name === 'theme') { localStorage.setItem('vydlora.theme', id); applyTheme(); }
  else if (name === 'shortcuts-reset') { saveShortcuts(shortcutDefaults); }
  else if (name === 'remove-pin') { modal('Remove workspace PIN', `<form data-form="remove-pin">${field('Current PIN', 'current', '', { type: 'password', required: true })}${actions('Remove PIN')}</form>`); return true; }
  else return false;
  await reload(); return true;
}
export async function uploadCookies(input) {
  const file = input.files[0]; if (!file) return;
  if (file.size > 5 * 1024 * 1024) throw new Error('Choose a cookie file smaller than 5 MB.');
  const platform = input.dataset.cookieUpload;
  await upload(platform === 'tiktok' ? '/api/cookies/upload' : `/api/${platform}/cookies`, file);
  input.value = ''; toast('Cookies uploaded');
}
export function applyTheme() { const preference = localStorage.getItem('vydlora.theme') || 'dark'; document.documentElement.dataset.theme = preference === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : preference; }
