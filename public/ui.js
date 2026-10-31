import { patchContent } from './render.js';

export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const paths = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="m7 17 10-10M7 7h10v10"/>',
  download: '<path d="M12 3v12m-4-4 4 4 4-4M4 16v4h16v-4"/>',
  upload: '<path d="M12 16V4m-4 4 4-4 4 4M4 16v4h16v-4"/>',
  grid: '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/>',
  monitor: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4m-6-10 3-3 3 4 3-5 3 4"/>',
  queue: '<path d="M9 6h12M9 12h12M9 18h12M3 6h1M3 12h1M3 18h1"/>',
  activity: '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  chart: '<path d="M4 3v18h17M8 16v-5m5 5V7m5 9V4"/>',
  logs: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 11h8M8 15h5"/>',
  settings: '<path d="m9 3-1 3-3 1-2 5 2 5 3 1 1 3h6l1-3 3-1 2-5-2-5-3-1-1-3Z"/><circle cx="12" cy="12" r="3"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  sidebar: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18m8-13-3 4 3 4"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  link: '<path d="m10 13 4-4m-5 7-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m0 2 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0"/>',
  video: '<rect x="3" y="5" width="13" height="14" rx="2"/><path d="m16 10 5-3v10l-5-3"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  play: '<path d="m8 4 12 8-12 8Z"/>',
  retry: '<path d="M3 10a9 9 0 1 1 1 7M3 4v6h6"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  cloud: '<path d="M7 18a5 5 0 1 1-1-10 7 7 0 0 1 13 1 4.5 4.5 0 0 1-1 9Z"/>',
  folder: '<path d="M3 7V4h7l3 3h8v13H3Z"/>',
  'folder-open': '<path d="M3 20V5h6l3 3h8v3"/><path d="m3 20 3-9h16l-3 9Z"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  shield: '<path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6Z"/><path d="m8 12 3 3 5-6"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/>',
  keyboard: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M6 12h.01M10 12h.01M14 12h.01M18 12h.01M7 15h10"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
  moon: '<path d="M21 13a9 9 0 0 1-10-10A9 9 0 1 0 21 13Z"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5m0 3h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
  users: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m2-16a3 3 0 0 1 0 6m2 4a5 5 0 0 1 2 4v2"/>',
  telegram: '<path d="m2 11 20-8-4 18-6-5-4 3 1-6 9-7-11 6Z"/>',
  tiktok: '<path d="M14 3v12a4 4 0 1 1-4-4m4-8c0 4 3 6 7 6"/>',
  instagram: '<rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><path d="M17.5 6.5h.01"/>',
  x: '<path d="m4 3 16 18h-5L4 3h5l11 18M20 3 4 21"/>',
  globe: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18M5 6h14M5 18h14"/>',
  dots: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  spark: '<path d="m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3Z"/>',
  wifi: '<path d="M3 8a15 15 0 0 1 18 0M6 12a10 10 0 0 1 12 0m-9 4a5 5 0 0 1 6 0m-3 4h.01"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8Z"/>'
};
export const icon = (name, cls = '') => `<svg class="icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.grid}</svg>`;
export const button = (label, action, { icon: name, kind = '', disabled = false, attrs = '' } = {}) => `<button type="button" class="button ${kind}" ${action ? `data-action="${esc(action)}"` : ''} ${disabled ? 'disabled' : ''} ${attrs}>${name ? icon(name) : ''}${esc(label)}</button>`;
export const iconButton = (label, action, name, attrs = '') => `<button type="button" class="icon-button" aria-label="${esc(label)}" title="${esc(label)}" data-action="${esc(action)}" ${attrs}>${icon(name)}</button>`;
export const badge = (status = '') => `<span class="badge status-${esc(String(status).toLowerCase())}"><i></i>${esc(String(status).replaceAll('_', ' '))}</span>`;
export const count = value => value === undefined || value === null ? '-' : Number(value).toLocaleString();
export const bytes = value => { if (value === undefined || value === null) return '-'; if (!value) return '0 B'; const n = Math.min(4, Math.floor(Math.log(value) / Math.log(1024))); return `${(value / 1024 ** n).toFixed(n ? 1 : 0)} ${['B', 'KB', 'MB', 'GB', 'TB'][n]}`; };
export const time = value => { if (!value) return 'Never'; const d = new Date(/Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`); return Number.isNaN(+d) ? '-' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }); };
export const relative = value => { if (!value) return 'Not yet'; const d = new Date(/Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`), m = Math.floor((Date.now() - d) / 60000); return m < 1 ? 'Just now' : m < 60 ? `${m}m ago` : m < 1440 ? `${Math.floor(m / 60)}h ago` : `${Math.floor(m / 1440)}d ago`; };
export const empty = (title, description, name = 'folder', action = '') => `<div class="empty-state"><div class="empty-icon">${icon(name)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
export const errorView = message => `<div class="error-state" role="alert">${icon('info')}<div><h3>Couldn’t load this view</h3><p>${esc(message)}</p>${button('Try again', 'refresh', { icon: 'retry' })} ${button('Connection settings', 'connection')}</div></div>`;
export const skeleton = () => '<div class="skeleton-view" aria-label="Loading" aria-busy="true"><div></div><div></div><div></div></div>';
export const avatar = user => `<span class="avatar">${user.avatar_url ? `<img src="/api/media/image?url=${encodeURIComponent(user.avatar_url)}" alt="" loading="lazy" data-fallback="avatar">` : ''}<span>${esc((user.nickname || user.username || '?').slice(0, 1).toUpperCase())}</span></span>`;
export const field = (label, name, value, { type = 'text', help = '', min, max, step, placeholder = '', required = false } = {}) => `<label class="field"><span>${esc(label)}</span><input name="${esc(name)}" type="${type}" value="${esc(value)}" ${min !== undefined ? `min="${min}"` : ''} ${max !== undefined ? `max="${max}"` : ''} ${step !== undefined ? `step="${step}"` : ''} placeholder="${esc(placeholder)}" ${required ? 'required' : ''}>${help ? `<small>${esc(help)}</small>` : ''}</label>`;
export const toggle = (label, name, checked, description = '') => `<label class="setting-row"><span><strong>${esc(label)}</strong>${description ? `<small>${esc(description)}</small>` : ''}</span><input type="checkbox" name="${esc(name)}" role="switch" ${checked ? 'checked' : ''}><span class="switch" aria-hidden="true"></span></label>`;
export function toast(message, error = false) {
  const el = document.createElement('div'); el.className = `toast ${error ? 'is-error' : ''}`; el.innerHTML = `${icon(error ? 'info' : 'check')}<span>${esc(message)}</span>`; document.querySelector('#toasts').append(el); setTimeout(() => el.remove(), 6500);
}
let previousFocus, activeModal, modalTimer, modalFocus;
export const getModalHandle = () => activeModal;
export const isModalActive = (handle = activeModal) => Boolean(handle && handle === activeModal);
function commitModal(handle, updating) {
  if (!isModalActive(handle)) return;
  const { title, html, options: { wide, className, closeable } } = handle;
  const overlays = document.querySelector('#overlays');
  let dialog = overlays.querySelector('.modal');
  const header = `<h2 id="modal-title" title="${esc(title)}">${esc(title)}</h2>${closeable ? iconButton('Close dialog', 'close-modal', 'close') : ''}`;
  const classes = `modal ${wide ? 'wide' : ''} ${className}`;
  if (!dialog) {
    overlays.innerHTML = `<div class="modal-backdrop"><section class="${classes}" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1"><header>${header}</header><div class="modal-content">${html}</div></section></div>`;
    dialog = overlays.querySelector('.modal');
    updating = false;
  } else {
    const scroll = dialog.scrollTop, hadFocus = dialog.contains(document.activeElement);
    const expanded = updating ? [...dialog.querySelectorAll('details')].map(element => [element, element.open]) : [];
    if (dialog.className !== classes) dialog.className = classes;
    if (updating) patchContent(dialog, `<header>${header}</header><div class="modal-content">${html}</div>`);
    else { patchContent(dialog.querySelector('header'), header); dialog.querySelector('.modal-content').innerHTML = html; }
    for (const [element, open] of expanded) if (element.isConnected && element.open !== open) element.open = open;
    dialog.scrollTop = updating ? scroll : 0;
    if (updating && hadFocus && !dialog.contains(document.activeElement)) dialog.focus({ preventScroll: true });
  }
  if (!document.body.classList.contains('modal-open')) document.body.classList.add('modal-open');
  if (!updating) modalFocus = requestAnimationFrame(() => { if (isModalActive(handle)) dialog.focus({ preventScroll: true }); });
}
export function modal(title, html, { wide = false, className = '', closeable = true, delay = 0 } = {}) {
  if (!activeModal) previousFocus = document.activeElement;
  clearTimeout(modalTimer); cancelAnimationFrame(modalFocus);
  const handle = { title, html, options: { wide, className, closeable } };
  activeModal = handle;
  if (delay && !document.querySelector('#overlays .modal')) modalTimer = setTimeout(() => commitModal(handle, false), delay);
  else commitModal(handle, false);
  return handle;
}
export function updateModal(handle, title, html, options = {}) {
  if (!isModalActive(handle)) return false;
  clearTimeout(modalTimer);
  handle.title = title; handle.html = html; Object.assign(handle.options, options);
  commitModal(handle, true);
  return true;
}
export function closeModal() {
  if (!activeModal) return;
  clearTimeout(modalTimer); cancelAnimationFrame(modalFocus); activeModal = null;
  document.querySelector('#overlays').replaceChildren(); document.body.classList.remove('modal-open');
  if (previousFocus?.isConnected && !previousFocus.closest('[inert]')) previousFocus.focus({ preventScroll: true });
  else document.querySelector('#main')?.focus({ preventScroll: true });
  previousFocus = null;
}
export function downloadText(filename, content, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([content], { type })); const a = document.createElement('a'); a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
