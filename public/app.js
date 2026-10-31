import { api, post, put, patch, del, query, realtime } from './api.js';
import { esc, icon, button, iconButton, badge, count, bytes, time, relative, avatar, empty, errorView, skeleton, field, toast, modal, updateModal, isModalActive, getModalHandle, closeModal, downloadText } from './ui.js';
import { categories, settingsContent, submitSettings, settingsAction, uploadCookies, applyTheme } from './settings.js';
import { hasPin, verifyPin, shortcuts, shortcutNames, chord } from './security.js';
import { mountHomeMotion, releaseHomeMotion, selectPlatform, rollText } from './motion.js';
import { openMenu, closeMenu, selectMenu, bindMenus } from './menus.js';
import { libraryTabs, libraryFilters, libraryContents, libraryFilterMenu, libraryDateRange, syncLibraryHeader, fileActionMenu } from './library.js';
import { patchContent } from './render.js';
import { bindMediaViewer, fitVideoModal } from './media.js';

applyTheme();
bindMediaViewer();
const routes = { '/': ['New', 'plus'], '/downloads': ['Downloads', 'folder'], '/monitor': ['Monitoring', 'monitor'], '/queue': ['Download queue', 'queue'], '/dashboard': ['Overview', 'chart'], '/activity': ['Activity', 'activity'], '/logs': ['Logs', 'logs'], '/terminal': ['Terminal', 'terminal'], '/settings': ['Settings', 'settings'] };
const state = { route: '/', category: 'overview', platform: '', page: 1, search: '', status: '', sort: 'created_at:desc', selected: new Set(), view: localStorage.getItem('vydlora.library-view') || 'grid', homePlatform: '', homeMode: 'download', homeUrl: '', homeRequests: [], connected: false, wsConnected: false, loading: false, notifications: 0, rows: [], pageData: null, settings: {}, loadId: 0, locked: hasPin(), terminalPaused: false, terminalLines: [], chartDays: '14', creator: null, media: null, mediaIndex: 0 };
let refreshTimer, searchTimer, liveTimer;
Object.assign(state, { homeBusy: false, requestSequence: 0 });
Object.assign(state, { librarySource: '', libraryPeriod: '', libraryFrom: '', libraryTo: '', librarySearchOpen: false });
const shell = document.querySelector('#app');
function routeInfo() { return routes[state.route] || routes['/']; }
function workspaceActions() { return `${iconButton('Search workspace', 'search-dialog', 'search')}<button class="icon-button notifications-button" data-action="notifications" aria-label="Notifications" title="Notifications">${icon('bell')}<span id="notification-dot" hidden></span></button><button class="connection-status" data-action="connection" aria-label="Engine connection settings"><i class="status-dot" id="connection-dot"></i><span id="connection-label">Connecting</span></button>`; }
function navLink(path, label, name) { return `<a class="nav-link ${state.route === path ? 'active' : ''}" href="${path}" ${state.route === path ? 'aria-current="page"' : ''} title="${esc(label)}">${icon(name)}<span>${esc(label)}</span>${path === '/queue' ? '<span class="nav-count" id="queue-count" hidden></span>' : ''}</a>`; }
function renderShell() {
  shell.innerHTML = `<div class="app-shell ${localStorage.getItem('vydlora.collapsed') === 'true' ? 'collapsed' : ''}"><button class="sidebar-scrim" data-action="mobile-close" aria-label="Close navigation"></button><aside class="sidebar" aria-label="Workspace navigation"><div class="sidebar-header"><a href="/" class="brand" aria-label="Vydlora home"><img src="/assets/mark.svg" alt=""><span>Vydlora<span class="brand-dot">®</span></span></a>${iconButton('Collapse sidebar', 'sidebar', 'sidebar')}</div><div class="sidebar-scroll"><nav class="nav-section">${navLink('/', 'New', 'plus')}${navLink('/downloads', 'Downloads', 'grid')}${navLink('/monitor', 'Monitoring', 'monitor')}${navLink('/queue', 'Queue', 'queue')}</nav><nav class="nav-section"><div class="section-label">Workspace</div>${navLink('/dashboard', 'Overview', 'chart')}${navLink('/activity', 'Activity', 'activity')}${navLink('/logs', 'Logs', 'logs')}</nav><section class="sidebar-recents"><div class="section-label">Recents</div><div id="sidebar-recents"><p class="sidebar-hint">Your downloads will appear here</p></div></section></div><div class="sidebar-bottom"><nav>${navLink('/terminal', 'Terminal', 'terminal')}${navLink('/settings', 'Settings', 'settings')}</nav><button class="profile-button" data-action="profile"><span class="profile-avatar">V</span><span class="profile-info"><strong>Personal workspace</strong><small><i class="status-dot" id="sidebar-dot"></i><span id="sidebar-status">Connecting to Lora 1</span></small></span>${icon('down')}</button></div></aside><div class="workspace"><header class="topbar"><div class="topbar-left">${iconButton('Open navigation', 'mobile-menu', 'menu')}<h1 id="page-title">New</h1></div><div id="header-center" class="header-center"></div><div class="topbar-right"></div></header><main id="main" tabindex="-1"></main><footer class="workspace-footer"><span>Vydlora <span class="muted">/</span> Powered by Lora 1</span><button data-action="shortcuts" aria-label="Keyboard shortcuts">${icon('keyboard')}<span>Keyboard shortcuts</span></button></footer></div></div>`;
  if (state.locked) showLock();
}
function pageActions() {
  switch (state.route) {
    case '/monitor': return `${button('Export', 'export-creators', { icon: 'upload' })}${button('Import', 'import-dialog', { icon: 'download' })}${button('Add creators', 'add-creator', { icon: 'plus', kind: 'primary' })}`;
    case '/queue': return `${button('Pause all', 'queue-pause-all', { icon: 'pause' })}${button('Resume all', 'queue-resume-all', { icon: 'play' })}${button('Retry failed', 'queue-retry-failed', { icon: 'retry' })}${button('Clear completed', 'queue-clear-completed', { icon: 'check' })}${button('Clear queue', 'queue-clear-dialog', { icon: 'trash', kind: 'danger subtle' })}`;
    case '/dashboard': return `${button(state.settings.monitoring_paused ? 'Resume monitoring' : 'Pause monitoring', 'toggle-monitoring', { icon: state.settings.monitoring_paused ? 'play' : 'pause' })}${button('Scan now', 'scan-all', { icon: 'retry' })}${button('Catch up', 'catchup', { icon: 'download' })}`;
    case '/activity': return button('Clear activity', 'activity-clear-dialog', { icon: 'trash' });
    case '/logs': return `${button('Live terminal', 'go-terminal', { icon: 'terminal' })}${button('Export logs', 'export-logs', { icon: 'download' })}`;
    case '/terminal': return `${button(state.terminalPaused ? 'Resume stream' : 'Pause stream', 'terminal-pause', { icon: state.terminalPaused ? 'play' : 'pause' })}${button('Copy', 'terminal-copy', { icon: 'copy' })}${button('Clear view', 'terminal-clear', { icon: 'trash' })}`;
    default: return '';
  }
}
function updateHeader() {
  const [title] = routeInfo(); document.title = `Vydlora | ${title}`; document.querySelector('#page-title').textContent = title;
  const center = document.querySelector('#header-center'), right = document.querySelector('.topbar-right');
  const files = state.route === '/downloads', actions = ['/monitor', '/queue', '/dashboard', '/activity', '/logs', '/terminal'].includes(state.route);
  const kind = files ? 'files' : actions ? state.route : ['/', '/settings'].includes(state.route) ? 'workspace' : 'none';
  document.querySelector('.topbar').classList.toggle('files-topbar', files);
  document.querySelector('.topbar').classList.toggle('actions-topbar', actions);
  document.querySelector('.app-shell').classList.toggle('files-route', files);
  if (right.dataset.kind !== kind) {
    right.innerHTML = files ? libraryFilters(state) : actions ? pageActions() : kind === 'workspace' ? workspaceActions() : '';
    right.dataset.kind = kind;
  }
  if (files) { center.innerHTML = libraryTabs(state.status); syncLibraryHeader(state); }
  else if (state.route === '/') {
    if (!center.querySelector('.segmented')) center.innerHTML = `<div class="segmented" role="group" aria-label="Workspace mode"><button data-action="home-mode:download">${icon('download')}Download</button><button data-action="home-mode:monitor">${icon('monitor')}Monitor</button></div>`;
    center.querySelectorAll('button').forEach(button => { const selected = button.dataset.action === `home-mode:${state.homeMode}`; button.classList.toggle('selected', selected); button.setAttribute('aria-pressed', String(selected)); });
  } else center.replaceChildren();
  document.querySelectorAll('.nav-link').forEach(a => { const active = a.getAttribute('href') === state.route; a.classList.toggle('active', active); if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  syncHeaderActions();
  updateConnection();
}
function syncHeaderActions() {
  const right = document.querySelector('.topbar-right');
  const control = state.route === '/dashboard' ? right.querySelector('[data-action="toggle-monitoring"]') : state.route === '/terminal' ? right.querySelector('[data-action="terminal-pause"]') : null;
  if (!control) return;
  const paused = state.route === '/dashboard' ? state.settings.monitoring_paused : state.terminalPaused;
  const label = `${paused ? 'Resume' : 'Pause'} ${state.route === '/dashboard' ? 'monitoring' : 'stream'}`;
  if (control.textContent.trim() !== label) control.innerHTML = `${icon(paused ? 'play' : 'pause')}${esc(label)}`;
  if (state.route === '/dashboard') control.disabled = !('monitoring_paused' in state.settings);
}
function updateConnection() {
  const online = state.connected;
  for (const id of ['sidebar-dot', 'connection-dot']) document.getElementById(id)?.classList.toggle('online', online);
  const label = document.querySelector('#connection-label'); if (label) label.textContent = online ? 'Connected' : 'Engine offline';
  const side = document.querySelector('#sidebar-status'); if (side) side.textContent = online ? state.wsConnected ? 'Lora 1 · Live' : 'Lora 1 connected' : 'Lora 1 offline';
  const dot = document.querySelector('#notification-dot'); if (dot) { dot.hidden = !state.notifications; dot.textContent = state.notifications > 9 ? '9+' : state.notifications; }
}
function syncSidebarA11y() {
  const app = document.querySelector('.app-shell');
  const mobile = matchMedia('(max-width: 768px)').matches;
  const overlay = mobile ? app.classList.contains('mobile-open') : matchMedia('(max-width: 1050px)').matches && app.classList.contains('tablet-expanded');
  document.querySelector('.sidebar').inert = mobile && !overlay;
  document.querySelector('.workspace').inert = overlay;
  document.querySelector('[data-action="mobile-menu"]').setAttribute('aria-expanded', String(overlay));
}
function parseRoute() {
  const parts = location.pathname.replace(/\/$/, '').split('/').filter(Boolean);
  state.route = parts.length ? `/${parts[0]}` : '/';
  if (!routes[state.route]) state.route = '/';
  state.category = state.route === '/settings' && categories.some(c => c[0] === parts[1]) ? parts[1] : 'overview';
}
async function navigate(path, { replace = false } = {}) {
  if (state.locked) return;
  if (path === location.pathname) {
    document.querySelector('.app-shell').classList.remove('mobile-open', 'tablet-expanded');
    syncSidebarA11y();
    return;
  }
  clearTimeout(searchTimer);
  closeMenu();
  closeModal();
  if (path !== location.pathname) history[replace ? 'replaceState' : 'pushState']({}, '', path);
  parseRoute(); state.page = 1; state.search = ''; state.status = ''; state.platform = ''; state.selected.clear();
  state.librarySource = ''; state.libraryPeriod = ''; state.libraryFrom = ''; state.libraryTo = ''; state.librarySearchOpen = false;
  document.querySelector('.app-shell').classList.remove('mobile-open');
  document.querySelector('.app-shell').classList.remove('tablet-expanded');
  syncSidebarA11y();
  updateHeader(); await loadPage();
  if (location.pathname !== path) return;
  document.querySelector('#main').focus({ preventScroll: true }); window.scrollTo(0, 0);
}
const pageHeading = (title, description) => `<div class="page-heading"><div><h2>${esc(title)}</h2><p>${esc(description)}</p></div></div>`;
function platformTabs(action = 'platform', current = state.platform, all = true, className = '') { return `<div class="platform-tabs ${esc(className)}" role="group" aria-label="Platform">${[...(all ? [['', 'All platforms', 'globe']] : []), ['tiktok', 'TikTok', 'tiktok'], ['instagram', 'Instagram', 'instagram'], ['x', 'X', 'x']].map(([value, label, name]) => `<button type="button" class="platform-pill ${current === value ? 'selected' : ''}" data-platform="${value || 'all'}" data-action="${action}:${value}" aria-label="${label}" aria-pressed="${current === value}">${icon(name)}<span>${label}</span></button>`).join('')}</div>`; }
function pagination(data) {
  if (!data || data.pages <= 1) return '';
  return `<div class="pagination"><span>${count(data.total)} results · Page ${data.page} of ${data.pages}</span><div>${button('Previous', 'previous-page', { disabled: data.page <= 1 })}${button('Next', 'next-page', { disabled: data.page >= data.pages })}</div></div>`;
}
function searchInput(placeholder) { return `<label class="search-input">${icon('search')}<input type="search" data-list-search value="${esc(state.search)}" aria-label="${esc(placeholder)}" placeholder="${esc(placeholder)}"></label>`; }
function toolbar({ search = 'Search…', statuses = null, sort = false, extra = '' } = {}) { return `<div class="toolbar">${searchInput(search)}<div class="toolbar-options">${statuses ? `${selectMenu(statuses, state.status, { label: 'Filter by status', prefix: 'Status', attrs: 'data-filter="status"' })}` : ''}${sort ? `${selectMenu([['created_at:desc', 'Newest first'], ['created_at:asc', 'Oldest first']], state.sort, { label: 'Sort results', prefix: 'Sort by', attrs: 'data-filter="sort"' })}` : ''}${extra}${iconButton('Refresh', 'refresh', 'retry')}</div></div>`; }
function selectionBar(kind) { const n = state.selected.size; return `<div class="selection-bar ${n ? 'visible' : ''}" id="selection-bar"><strong id="selected-count">${n} selected</strong><div class="button-row">${kind === 'monitor' ? `${button('Enable', 'bulk-enable', { icon: 'play' })}${button('Disable', 'bulk-disable', { icon: 'pause' })}${button('Scan', 'bulk-scan', { icon: 'retry' })}` : ''}${button(kind === 'queue' ? 'Remove' : 'Delete', 'bulk-delete', { icon: 'trash', kind: 'danger subtle' })}${button('Deselect', 'deselect')}</div></div>`; }

function homeContent() {
  const monitor = state.homeMode === 'monitor';
  const platform = { tiktok: 'TikTok', instagram: 'Instagram', x: 'X' }[state.homePlatform];
  return {
    title: monitor ? 'Creator Monitor' : 'Media Download',
    icon: monitor ? 'monitor' : 'download',
    label: monitor ? 'Keep up with every new post' : 'Add a link. Keep what inspires you.',
    description: monitor ? 'Add a creator and let Vydlora collect the rest.' : 'Videos, images, and stories. In their original quality.',
    placeholder: monitor ? 'Enter a username or profile link' : platform ? `Paste a ${platform} link…` : 'Paste a link to download…',
    inputLabel: monitor ? 'Creator username or profile URL' : 'Media URLs, one per line',
    inputIcon: monitor ? 'users' : 'link',
    submit: monitor ? 'Add creator' : 'Download',
    submitLabel: monitor ? 'Add creator' : 'Download media',
    submitIcon: monitor ? 'plus' : 'download',
    hint: monitor ? 'TikTok, Instagram, and X creators' : 'TikTok, Instagram, X, and more · One link per line',
  };
}
function homeView() {
  const copy = homeContent();
  return `<section class="home-stage"><div class="home-workspace" data-platform="${state.homePlatform || 'all'}">
    <h2 class="hero-title" aria-label="${copy.title}"><span class="hero-icon">${icon(copy.icon)}</span><span class="title-reel" data-value="${copy.title}" aria-hidden="true">${copy.title}</span></h2>
    ${platformTabs('home-platform', state.homePlatform)}
    <div class="download-workspace"><span class="workspace-edge" aria-hidden="true"><span class="workspace-edge-line"></span></span>
      <form data-form="home" class="download-form"><div class="download-content">
        <label class="download-label" for="download-input">${copy.label}</label><p class="download-description">${copy.description}</p>
        <div class="url-input-wrap">${icon(copy.inputIcon)}<textarea id="download-input" name="url" rows="1" placeholder="${copy.placeholder}" aria-label="${copy.inputLabel}" required spellcheck="false">${esc(state.homeUrl)}</textarea><button type="submit" class="button primary" id="download-submit" aria-label="${copy.submitLabel}">${icon(copy.submitIcon)}<span>${copy.submit}</span></button></div>
        <p class="input-hint">${copy.hint}</p>
      </div><div class="workspace-controls"><div class="capability-pills"><span>${icon('spark')}Original quality</span><span>${icon('image')}Video & images</span><button type="button" data-action="batch-help">${icon('queue')}Batch links</button></div>${iconButton('Download settings', 'download-settings', 'settings')}</div></form>
    </div>
    <div class="home-note"><span><i class="status-dot ${state.connected ? 'online' : ''}"></i>${state.connected ? 'Your Lora 1 engine is ready' : 'Connect Lora 1 to start collecting'}</span><button data-action="${state.connected ? 'go-queue' : 'connection'}">${state.connected ? 'View queue' : 'Set up connection'}${icon('arrow')}</button></div>
    <div class="home-requests" id="home-requests">${homeRequestRows()}</div>
  </div></section><section class="home-recent-section" id="home-recent"></section>`;
}
function updateHomeView({ modeChanged = false } = {}) {
  const workspace = document.querySelector('.home-workspace');
  if (!workspace) return;
  const copy = homeContent();
  workspace.dataset.platform = state.homePlatform || 'all';
  selectPlatform(workspace.querySelector('.platform-tabs'), state.homePlatform);
  const input = workspace.querySelector('#download-input');
  input.placeholder = copy.placeholder;
  input.setAttribute('aria-label', copy.inputLabel);
  if (modeChanged) {
    workspace.querySelector('.hero-title').setAttribute('aria-label', copy.title);
    rollText(workspace.querySelector('.title-reel'), copy.title);
    workspace.querySelector('.hero-icon').innerHTML = icon(copy.icon);
    workspace.querySelector('.download-label').textContent = copy.label;
    workspace.querySelector('.download-description').textContent = copy.description;
    workspace.querySelector('.input-hint').textContent = copy.hint;
    workspace.querySelector('.url-input-wrap > .icon').outerHTML = icon(copy.inputIcon);
    const submit = workspace.querySelector('#download-submit');
    submit.setAttribute('aria-label', copy.submitLabel);
    submit.querySelector('.icon').outerHTML = icon(copy.submitIcon);
    submit.querySelector('span').textContent = copy.submit;
  }
  syncHomeBusy();
}
function homeRequestRows() { return state.homeRequests.map(r => `<div class="request-row" data-key="request:${r.id}">${icon(r.error ? 'info' : r.done ? 'check' : 'download')}<div><strong>${esc(r.title || r.url)}</strong><small>${esc(r.error || r.message || 'Submitting to Lora 1…')}</small></div>${badge(r.error ? 'failed' : r.status || (r.done ? 'queued' : 'processing'))}</div>`).join(''); }
function syncHomeBusy() {
  const form = document.querySelector('[data-form="home"]');
  if (!form) return;
  const submit = form.querySelector('[type="submit"]');
  if (state.homeBusy) { form.dataset.busy = 'true'; submit.setAttribute('aria-busy', 'true'); }
  else { delete form.dataset.busy; submit.removeAttribute('aria-busy'); }
  submit.disabled = state.homeBusy;
  submit.querySelector('span').textContent = state.homeBusy ? 'Adding…' : homeContent().submit;
}
async function loadPage({ silent = false } = {}) {
  const loadId = ++state.loadId; state.loading = true;
  const main = document.querySelector('#main');
  const view = state.route === '/settings' ? `/settings/${state.category}` : state.route;
  const sameView = main.dataset.view === view;
  main.setAttribute('aria-busy', 'true');
  const scroll = window.scrollY;
  let loadingTimer;
  if (!sameView) {
    closeMenu();
    main.inert = true;
    if (state.route === '/') {
      releaseHomeMotion();
      main.innerHTML = homeView(); main.dataset.view = view; main.inert = false;
      mountHomeMotion(main.querySelector('.home-workspace')); syncHomeBusy();
    } else if (!main.firstElementChild) {
      // Fast responses go straight to content, avoiding a skeleton/content flash.
      loadingTimer = setTimeout(() => {
        if (loadId === state.loadId) main.innerHTML = `<div class="${state.route === '/downloads' ? 'files-page' : 'page-content'}">${skeleton()}</div>`;
      }, 150);
    }
  }
  try {
    let html;
    switch (state.route) {
      case '/': if (!sameView || !silent) refreshHomeRecent(); return;
      case '/monitor': html = await monitorView(); break;
      case '/downloads': html = await libraryView(); break;
      case '/queue': html = await queueView(); break;
      case '/dashboard': html = await dashboardView(); break;
      case '/activity': html = await activityView(); break;
      case '/logs': html = await logsView(); break;
      case '/terminal': html = await terminalView(); break;
      case '/settings': html = await settingsView(); break;
    }
    if (loadId !== state.loadId) return;
    clearTimeout(loadingTimer);
    if (sameView) patchContent(main, html);
    else { releaseHomeMotion(); main.innerHTML = html; main.dataset.view = view; }
    state.refreshError = null;
    syncHeaderActions();
    updateSelection();
    if (state.route === '/terminal' && !state.terminalPaused) scrollTerminal();
    if (silent) window.scrollTo(0, scroll);
  } catch (error) {
    if (loadId !== state.loadId) return;
    if (sameView && main.firstElementChild) {
      if (state.refreshError !== error.message) toast(error.message, true);
      state.refreshError = error.message;
    } else { releaseHomeMotion(); main.innerHTML = `<div class="page-content">${errorView(error.message)}</div>`; delete main.dataset.view; }
  }
  finally {
    clearTimeout(loadingTimer);
    if (loadId === state.loadId) { state.loading = false; main.inert = false; main.removeAttribute('aria-busy'); }
  }
}
async function refreshHomeRecent() {
  try {
    const data = await api('/api/downloads?page_size=6');
    if (state.route !== '/') return;
    const element = document.querySelector('#home-recent'); if (!element) return;
    patchContent(element, data.items.length ? `<div class="section-heading"><h3>Recently collected</h3><a href="/downloads">View all ${icon('chevron')}</a></div><div class="media-grid compact">${data.items.map(mediaCard).join('')}</div>` : '');
  } catch { /* Connection status provides the actionable engine error. */ }
}
async function monitorView() {
  if (!['tiktok', 'instagram', 'x'].includes(state.platform)) state.platform = 'tiktok';
  const loadId = state.loadId;
  const [sortBy, sortDir] = state.sort.split(':');
  const [data, settings, scan] = await Promise.all([api(query('/api/users', { page: state.page, page_size: 30, platform: state.platform, search: state.search, status: state.status, sort_by: sortBy, sort_dir: sortDir })), api('/api/settings'), api(query('/api/workers/scan-status', { platform: state.platform || 'tiktok' }))]);
  if (loadId !== state.loadId || state.route !== '/monitor') return '';
  state.rows = data.items; state.pageData = data; state.settings = settings;
  return `<div class="page-content monitor-page">${pageHeading('Your creators, on autopilot.', 'Follow the creators you love. Lora 1 takes care of every new post.')}<div class="monitor-subheader">${platformTabs('platform', state.platform, false, 'monitor-platform-tabs')}<div class="scan-state"><i class="status-dot ${!settings.monitoring_paused && scan.running && !scan.deferred ? 'online' : ''}"></i><span>${settings.monitoring_paused ? 'Monitoring paused' : scan.deferred ? esc(scan.deferred_reason) : scan.running ? 'Monitoring active' : 'Monitor idle'}</span>${button(settings.monitoring_paused ? 'Resume' : 'Pause', 'toggle-monitoring', { icon: settings.monitoring_paused ? 'play' : 'pause', kind: 'subtle' })}${button('Scan now', 'scan-all', { icon: 'retry', kind: 'subtle' })}</div></div><div class="monitor-results">${toolbar({ search: 'Search creators…', statuses: [['', 'All statuses'], ['active', 'Active'], ['monitoring', 'Monitoring'], ['disabled', 'Disabled'], ['error', 'Error'], ['private', 'Private'], ['rate_limited', 'Rate limited'], ['deleted', 'Deleted']], sort: true })}${selectionBar('monitor')}<div class="table-card">${data.items.length ? `<div class="table-scroll"><table><thead><tr><th class="check-cell"><input type="checkbox" data-select-all aria-label="Select all creators on this page" ${data.items.every(r => state.selected.has(r.id)) ? 'checked' : ''}></th><th>Creator</th><th>Status</th><th>Collected</th><th>Last checked</th><th class="align-right">Actions</th></tr></thead><tbody>${data.items.map(u => `<tr data-key="creator:${u.id}"><td><input type="checkbox" data-select="${u.id}" aria-label="Select ${esc(u.username)}" ${state.selected.has(u.id) ? 'checked' : ''}></td><td><button class="creator-cell" data-action="creator:${u.id}">${avatar(u)}<span><strong>${esc(u.nickname || u.username)}</strong><small>${icon(u.platform)}@${esc(u.username)}</small></span></button></td><td>${badge(u.enabled ? u.status : 'disabled')}${u.last_error ? `<small class="error-detail" title="${esc(u.last_error)}">${esc(u.last_error)}</small>` : ''}</td><td><strong>${count(u.downloaded_count)}</strong><small>${count(u.pending_count)} pending · ${count(u.total_posts)} posts</small></td><td title="${esc(time(u.last_checked))}">${relative(u.last_checked)}</td><td class="align-right"><div class="row-actions">${iconButton('Scan creator', `creator-scan:${u.id}`, 'retry')}${iconButton('Download all posts', `creator-download:${u.id}`, 'download')}${iconButton(u.enabled ? 'Disable monitoring' : 'Enable monitoring', `creator-toggle:${u.id}`, u.enabled ? 'pause' : 'play')}${iconButton('Remove creator', `creator-delete:${u.id}`, 'trash')}</div></td></tr>`).join('')}</tbody></table></div>` : empty(state.search || state.status ? 'No matching creators' : 'Make room for your favorites', state.search || state.status ? 'Try another search or filter.' : 'Add your first creator to start collecting new posts automatically.', 'monitor', button('Add creator', 'add-creator', { kind: 'primary', icon: 'plus' }))}</div>${pagination(data)}<p class="page-footnote">${scan.next_scan_at ? `Next ${esc(state.platform || 'TikTok')} scan: ${time(scan.next_scan_at)}` : 'Each platform uses its own monitoring schedule.'}</p></div></div>`;
}
function mediaCard(d) { return `<article class="media-card" data-file-id="${d.id}"><button class="media-preview" data-action="media:${d.id}" aria-label="Open ${esc(d.title || d.video_id)}"><img src="/api/downloads/${d.id}/thumbnail" alt="" loading="lazy" data-fallback="media"><span class="media-placeholder">${icon(d.post_type === 'image' ? 'image' : 'video')}</span><span class="media-type">${icon(d.post_type === 'image' ? 'image' : 'play')}${d.duration ? `${Math.floor(d.duration / 60)}:${String(Math.floor(d.duration % 60)).padStart(2, '0')}` : d.post_type === 'image' ? 'Images' : 'Video'}</span>${d.remote_path ? `<span class="cloud-indicator" title="Stored in MEGA">${icon('cloud')}</span>` : ''}</button><div class="media-info"><strong title="${esc(d.title)}">${esc(d.title || d.video_id || 'Untitled media')}</strong><div><span>${esc(d.username ? '@' + d.username : d.source || 'Media')} · ${bytes(d.file_size)}</span><label class="media-select"><input type="checkbox" data-select="${d.id}" aria-label="Select ${esc(d.title || d.video_id)}" ${state.selected.has(d.id) ? 'checked' : ''}></label></div></div></article>`; }
async function libraryView() {
  const loadId = state.loadId;
  const [sortBy, sortDir] = state.sort.split(':');
  const data = await api(query('/api/downloads', { page: state.page, page_size: 30, search: state.search,
    post_type: state.status, sort_by: sortBy, sort_dir: sortDir, source_group: state.librarySource, ...libraryDateRange(state) }));
  if (loadId !== state.loadId || state.route !== '/downloads') return '';
  state.rows = data.items; state.pageData = data;
  syncLibraryHeader(state);
  return libraryContents(data, state);
}
async function refreshLibrary() {
  closeMenu({ focus: true });
  state.page = 1; state.selected.clear();
  syncLibraryHeader(state);
  await loadPage({ silent: true });
}
async function queueView() {
  const loadId = state.loadId;
  const [data, counts] = await Promise.all([api(query('/api/queue', { page: state.page, page_size: 30, status: state.status })), api('/api/queue/counts')]);
  if (loadId !== state.loadId || state.route !== '/queue') return '';
  state.rows = data.items; state.pageData = data;
  const rows = data.items.filter(r => !state.search || `${r.title} ${r.username} ${r.url}`.toLowerCase().includes(state.search.toLowerCase()));
  return `<div class="page-content">${pageHeading('Let the good things queue.', 'Follow every download, from the first request to the last frame.')}<div class="stat-grid queue-stats">${[['pending', 'Pending'], ['processing', 'Downloading'], ['completed', 'Completed'], ['failed', 'Failed'], ['paused', 'Paused']].map(([key, label]) => `<button class="stat-card ${state.status === key ? 'selected' : ''}" data-action="status:${key}"><span>${label}</span><strong>${count(counts[key])}</strong></button>`).join('')}</div>${toolbar({ search: 'Search this queue page…', statuses: [['', 'All jobs'], ['pending', 'Pending'], ['processing', 'Downloading'], ['completed', 'Completed'], ['failed', 'Failed'], ['paused', 'Paused'], ['cancelled', 'Cancelled']] })}${selectionBar('queue')}<div class="table-card">${rows.length ? `<div class="table-scroll"><table><thead><tr><th><input type="checkbox" data-select-all aria-label="Select all jobs on this page"></th><th>Download</th><th>Progress</th><th>Worker</th><th>Added</th><th>Actions</th></tr></thead><tbody>${rows.map(j => `<tr data-job-id="${j.id}"><td><input type="checkbox" data-select="${j.id}" aria-label="Select job ${j.id}" ${state.selected.has(j.id) ? 'checked' : ''}></td><td class="job-title"><strong title="${esc(j.url)}"><button type="button" class="job-details-button" data-action="job-details:${j.id}">${esc(j.title || j.video_id || j.url)}</button></strong><small>${esc(j.username ? '@' + j.username : j.source)} · ${count(j.attempts)} attempts</small>${j.error && j.status !== 'completed' ? `<small class="error-detail">${esc(j.error)}</small>` : ''}</td><td><div data-job-status>${badge(j.status)}</div><div class="progress"><div style="width:${Math.max(0, Math.min(100, j.progress || 0))}%" data-job-progress></div></div></td><td>${esc(j.worker || 'Waiting')}</td><td>${relative(j.created_at)}</td><td><div class="row-actions">${['pending', 'processing'].includes(j.status) ? iconButton('Pause download', `job:pause:${j.id}`, 'pause') : ''}${j.status === 'paused' ? iconButton('Resume download', `job:resume:${j.id}`, 'play') : ''}${['failed', 'cancelled'].includes(j.status) ? iconButton('Retry download', `job:retry:${j.id}`, 'retry') : ''}${!['completed', 'cancelled'].includes(j.status) ? iconButton('Cancel download', `job:cancel:${j.id}`, 'close') : ''}${iconButton('Remove job', `job-remove:${j.id}`, 'trash')}</div></td></tr>`).join('')}</tbody></table></div>` : empty('All caught up', state.status || state.search ? 'No jobs match this filter.' : 'New downloads will appear here. Lora 1 handles the rest.', 'queue', button('Add a download', 'go-home', { kind: 'primary', icon: 'plus' }))}</div>${pagination(data)}</div>`;
}
async function dashboardView() {
  const loadId = state.loadId;
  const [data, points, settings] = await Promise.all([api('/api/dashboard'), api(state.chartDays === 'hourly' ? '/api/analytics/downloads-per-hour?hours=24' : `/api/analytics/downloads-per-day?days=${state.chartDays}`), api('/api/settings')]);
  if (loadId !== state.loadId || state.route !== '/dashboard') return '';
  const overview = data.overview; state.settings = settings;
  const max = Math.max(1, ...points.map(p => p.value));
  return `<div class="page-content">${pageHeading('A little perspective.', 'Your collection, your creators, and everything happening in between.')}<div class="stat-grid">${[['Total downloads', overview.total_downloads, 'download', `${count(overview.downloads_today)} collected today`], ['Monitored creators', overview.monitored_users, 'users', 'Across your connected platforms'], ['Success rate', `${(overview.success_rate || 0).toFixed(1)}%`, 'check', 'Completed downloads'], ['Storage used', overview.storage_used_human || bytes(overview.storage_used), 'cloud', `${count(overview.active_workers)} active workers`]].map(([label, value, name, description]) => `<div class="stat-card"><span>${label}${icon(name)}</span><strong>${typeof value === 'number' ? count(value) : value}</strong><small>${description}</small></div>`).join('')}</div><div class="dashboard-grid"><section class="panel chart-panel"><div class="panel-heading"><div><h3>Downloads over time</h3><p>Your collection is growing.</p></div>${selectMenu([['hourly', 'Last 24 hours'], ['7', 'Last 7 days'], ['14', 'Last 14 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days']], String(state.chartDays), { label: 'Chart period', prefix: 'Period', attrs: 'data-filter="chart-days"' })}</div><div class="bar-chart" role="img" aria-label="${state.chartDays === 'hourly' ? 'Downloads per hour for the last 24 hours' : `Downloads per day for the last ${state.chartDays} days`}"><div class="chart-y"><span>${count(max)}</span><span>${count(Math.round(max / 2))}</span><span>0</span></div><div class="chart-bars ${points.length > 30 ? 'dense' : ''}">${points.map(p => `<div class="chart-column" title="${esc(p.label)}: ${p.value} downloads"><div style="height:${p.value / max * 100}%" aria-label="${esc(p.label)}: ${p.value}"></div><span>${esc(p.label.slice(-5))}</span></div>`).join('')}</div></div>${points.every(p => !p.value) ? '<p class="chart-empty">Your first download will start the story.</p>' : ''}</section><section class="panel"><div class="panel-heading"><h3>Queue snapshot</h3><a href="/queue">View queue ${icon('arrow')}</a></div><div class="detail-list">${Object.entries(data.queue).map(([label, n]) => `<div class="detail-row">${badge(label)}<strong>${count(n)}</strong></div>`).join('')}</div></section><section class="panel"><div class="panel-heading"><h3>Top creators</h3><a href="/monitor">All creators ${icon('arrow')}</a></div>${data.top_creators.length ? data.top_creators.map((u, i) => `<div class="top-creator"><span class="rank">${i + 1}</span>${avatar(u)}<span><strong>${esc(u.nickname || u.username)}</strong><small>@${esc(u.username)}</small></span><strong>${count(u.downloads)}</strong></div>`).join('') : empty('Your favorites will rise here', 'Start monitoring creators to see their activity.', 'users')}</section><section class="panel"><div class="panel-heading"><h3>Workers</h3><a href="/settings/overview">System health ${icon('arrow')}</a></div>${data.workers.length ? data.workers.map(w => `<div class="worker-row">${icon('monitor')}<div><strong>${esc(w.name)}</strong><small>${esc(w.current_job || w.kind)}</small></div>${badge(w.state)}</div>`).join('') : empty('Workers are resting', 'Workers appear when the engine starts.', 'monitor')}</section></div></div>`;
}
async function activityView() {
  const loadId = state.loadId;
  const data = await api(query('/api/activity', { limit: 200, type: state.status }));
  if (loadId !== state.loadId || state.route !== '/activity') return '';
  state.rows = data;
  const filtered = data.filter(r => !state.search || r.message.toLowerCase().includes(state.search.toLowerCase()));
  return `<div class="page-content">${pageHeading('The story of your workspace.', 'New posts, finished downloads, and the moments in between.')}${toolbar({ search: 'Search recent activity…', statuses: [['', 'All activity'], ['download_completed', 'Downloads completed'], ['download_failed', 'Downloads failed'], ['user_added', 'Creators added'], ['scan_completed', 'Scans completed'], ['telegram_received', 'Telegram'], ['cookie_expired', 'Cookies expired'], ['download_started', 'Downloads started'], ['queue_completed', 'Queue completed'], ['user_removed', 'Creators removed'], ['worker_restart', 'Worker restarts'], ['cookie_rotation', 'Cookie rotation'], ['rate_limit', 'Rate limits'], ['user_private', 'Private creators']] })}${selectionBar('activity')}<div class="panel activity-feed">${filtered.length ? filtered.map(a => `<article class="feed-row" data-key="activity:${a.id}"><input type="checkbox" data-select="${a.id}" aria-label="Select activity ${a.id}" ${state.selected.has(a.id) ? 'checked' : ''}><span class="feed-icon ${a.type.includes('failed') ? 'failed' : ''}">${icon(a.type.includes('download') ? 'download' : a.type.includes('user') ? 'users' : a.type.includes('telegram') ? 'telegram' : 'activity')}</span><div><strong>${esc(a.message)}</strong><small>${esc(a.type.replaceAll('_', ' '))} · ${time(a.created_at)}</small></div>${a.user_id ? button('View creator', `creator:${a.user_id}`, { kind: 'subtle' }) : ''}</article>`).join('') : empty('A quiet moment', 'Your workspace activity will appear here as it happens.', 'activity')}</div></div>`;
}
async function logsView() {
  const loadId = state.loadId;
  const data = await api(query('/api/logs', { page: state.page, page_size: 50, level: state.status, search: state.search, source: state.logSource }));
  if (loadId !== state.loadId || state.route !== '/logs') return '';
  state.rows = data.items; state.pageData = data;
  return `<div class="page-content">${pageHeading('A closer look.', 'Filter, inspect, and export logs directly from Lora 1.')}${toolbar({ search: 'Search logs…', statuses: [['', 'All levels'], ['DEBUG', 'Debug'], ['INFO', 'Info'], ['WARNING', 'Warning'], ['ERROR', 'Error']], extra: `${selectMenu([['', 'All sources'], ['system', 'System'], ['worker', 'Worker'], ['download', 'Download'], ['telegram', 'Telegram'], ['monitor', 'Monitor'], ['api', 'API']], state.logSource || '', { label: 'Log source', prefix: 'Source', attrs: 'data-filter="log-source"' })}` })}<div class="table-card">${data.items.length ? `<div class="table-scroll"><table class="logs-table"><thead><tr><th>Time</th><th>Level</th><th>Source</th><th>Message</th></tr></thead><tbody>${data.items.map(l => `<tr data-key="log:${l.id}"><td>${time(l.created_at)}</td><td>${badge(l.level)}</td><td>${esc(l.source)}</td><td>${esc(l.message)}</td></tr>`).join('')}</tbody></table></div>` : empty('Nothing to report', 'Log messages matching your filters will appear here.', 'logs')}</div>${pagination(data)}</div>`;
}
function terminalLine(l) { return `<div class="terminal-line" data-key="line:${l.seq}"><time>${esc(time(l.ts || l.created_at))}</time><span class="terminal-level level-${esc(l.level)}">${esc(l.level)}</span><span class="terminal-source">${esc(l.source)}</span><span>${esc(l.message)}</span></div>`; }
async function terminalView() {
  if (!state.terminalInitialized) {
    const history = (await api('/api/logs/live?limit=500')).lines;
    state.terminalLines = [...new Map([...history, ...state.terminalLines].map(line => [line.seq, line])).values()].sort((a, b) => a.seq - b.seq).slice(-2000);
    state.terminalInitialized = true;
  }
  return `<div class="page-content terminal-page">${pageHeading('Behind the scenes.', 'Live engine output. This viewer does not execute commands.')}<div class="terminal-window"><div class="terminal-titlebar"><span><i class="status-dot ${state.wsConnected ? 'online' : ''}"></i>Lora 1 engine</span><span>${state.terminalPaused ? 'Paused' : 'Live output'}</span></div><div class="terminal-output" id="terminal-output" role="log" aria-label="Engine log output">${state.terminalLines.length ? state.terminalLines.map(terminalLine).join('') : '<p class="muted">Waiting for engine output…</p>'}</div></div></div>`;
}
function scrollTerminal() { const el = document.querySelector('#terminal-output'); if (el) el.scrollTop = el.scrollHeight; }
async function settingsView() { const content = await settingsContent(state.category); return `<div class="page-content settings-page">${pageHeading('Make yourself at home.', 'One workspace. Set up just the way you like it.')}<div class="settings-layout"><nav class="settings-nav" aria-label="Settings categories">${categories.map(([id, title, name]) => `<a href="/settings/${id}" class="${state.category === id ? 'active' : ''}" ${state.category === id ? 'aria-current="page"' : ''}>${icon(name)}${title}</a>`).join('')}</nav><div class="settings-content">${content}</div></div></div>`; }

async function healthCheck() {
  const results = await Promise.allSettled([api('/api/ping'), api('/api/queue/counts'), api('/api/notifications/unread-count')]);
  state.connected = results[0].status === 'fulfilled';
  if (results[1].status === 'fulfilled') { const q = results[1].value; const el = document.querySelector('#queue-count'); const n = q.pending + q.processing; if (el) { el.textContent = n; el.hidden = !n; } }
  if (results[2].status === 'fulfilled') state.notifications = results[2].value.count;
  updateConnection();
  if (state.route === '/' && !document.querySelector('#download-input')?.value && !document.querySelector('.modal') && !state.locked) { const note = document.querySelector('.home-note'); if (note) patchContent(note, `<span><i class="status-dot ${state.connected ? 'online' : ''}"></i>${state.connected ? 'Your Lora 1 engine is ready' : 'Connect Lora 1 to start collecting'}</span><button data-action="${state.connected ? 'go-queue' : 'connection'}">${state.connected ? 'View queue' : 'Set up connection'}${icon('arrow')}</button>`); }
}
async function refreshSidebar() {
  if (state.locked) return;
  try { const data = await api('/api/queue?page_size=6'); const el = document.querySelector('#sidebar-recents'); if (!el) return;
    patchContent(el, data.items.length ? data.items.map(j => `<button class="recent-item" data-key="recent:${j.id}" data-action="go-queue" title="${esc(j.title || j.url)}"><span class="recent-icon">${icon(j.status === 'completed' ? 'check' : 'download')}</span><span><strong>${esc(j.title || j.username || j.video_id || 'Media download')}</strong><small>${esc(j.status)} · ${relative(j.created_at)}</small></span></button>`).join('') : '<p class="sidebar-hint">Your downloads will appear here</p>');
  } catch { /* Keep the last known recent items. */ }
}
function scheduleRefresh() {
  if (liveTimer) return;
  liveTimer = setTimeout(() => {
    liveTimer = undefined;
    if (state.locked || state.loading || isModalActive() || document.querySelector('.menu-popover') || state.selected.size || state.route === '/settings' || state.route === '/' || state.route === '/terminal' || document.activeElement?.matches('input,textarea,select,[data-select-menu]')) return;
    loadPage({ silent: true });
  }, 900);
}
function onRealtime(message) {
  if (state.locked) return;
  if (message.type === 'log_lines') {
    const lines = message.data?.lines || []; state.terminalLines.push(...lines); state.terminalLines = state.terminalLines.slice(-2000);
    if (state.route === '/terminal' && !state.terminalPaused) { const output = document.querySelector('#terminal-output'); if (output) { output.insertAdjacentHTML('beforeend', lines.map(terminalLine).join('')); while (output.children.length > 2000) output.firstElementChild.remove(); scrollTerminal(); } }
    return;
  }
  if (message.type === 'queue_job') {
    const request = state.homeRequests.find(r => r.jobId === message.data.id);
    if (request) { request.status = message.data.status || request.status; request.progress = message.data.progress ?? request.progress; patchContent(document.querySelector('#home-requests'), homeRequestRows()); }
    const row = document.querySelector(`[data-job-id="${Number(message.data.id)}"]`);
    if (row && message.data.progress !== undefined) row.querySelector('[data-job-progress]').style.width = `${Math.max(0, Math.min(100, message.data.progress))}%`;
    if (row && message.data.status) row.querySelector('[data-job-status]').innerHTML = badge(message.data.status);
  }
  if (message.type === 'notification') { state.notifications++; updateConnection(); }
  if (['queue_job', 'download', 'scan', 'worker', 'activity', 'cloud_sync', 'cloud_data'].includes(message.type)) { scheduleRefresh(); if (!refreshTimer) refreshTimer = setTimeout(() => { refreshTimer = undefined; healthCheck(); refreshSidebar(); if (state.route === '/') refreshHomeRecent(); }, 1000); }
}

function creatorProgressMarkup() {
  return '<div class="creator-progress" role="status" aria-live="polite" aria-atomic="true" hidden><span class="process-spinner" aria-hidden="true"></span><div><strong data-creator-progress-title></strong><p data-creator-progress-detail></p></div></div>';
}
function startCreatorProgress(form, title, detail) {
  const progress = form.querySelector('.creator-progress');
  progress.dataset.state = 'loading';
  progress.querySelector('[data-creator-progress-title]').textContent = title;
  progress.querySelector('[data-creator-progress-detail]').textContent = detail;
  progress.hidden = false;
  const controls = [...form.elements].filter(control => !control.disabled);
  controls.forEach(control => { control.disabled = true; });
  const submit = form.querySelector('[type="submit"]'), originalLabel = submit.innerHTML;
  submit.innerHTML = '<span class="process-spinner" aria-hidden="true"></span>Adding…';
  return error => {
    controls.forEach(control => { control.disabled = false; });
    submit.innerHTML = originalLabel;
    progress.hidden = !error;
    if (error) {
      progress.dataset.state = 'error';
      progress.querySelector('[data-creator-progress-title]').textContent = form.dataset.form === 'add-creator' ? 'Couldn’t add creator' : 'Couldn’t finish import';
      progress.querySelector('[data-creator-progress-detail]').textContent = error.message;
    }
  };
}
function addCreatorDialog(platform = state.platform || 'tiktok', username = '') {
  state.preview = null;
  modal('Add a creator', `<form data-form="add-creator"><p class="modal-intro">Every new post, collected automatically.</p><label class="field"><span>Platform</span>${selectMenu([['tiktok', 'TikTok'], ['instagram', 'Instagram'], ['x', 'X']], platform, { label: 'Platform', name: 'platform', attrs: 'data-preview-platform' })}</label>${field('Username or profile URL', 'username', username, { placeholder: '@creator', required: true })}<div id="creator-preview"></div>${creatorProgressMarkup()}<div class="form-actions">${button('Preview profile', 'preview-creator', { icon: 'search' })}<button type="submit" class="button primary">${icon('plus')}Add creator</button></div></form>`);
}
function usernameOf(value, platform) {
  let name = value.trim();
  if (/^https?:\/\//i.test(name)) { const url = new URL(name); const hosts = { tiktok: ['tiktok.com', 'www.tiktok.com'], instagram: ['instagram.com', 'www.instagram.com'], x: ['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'] }; if (!hosts[platform].includes(url.hostname)) throw new Error('Choose the platform that matches this profile link.'); name = url.pathname.split('/').filter(Boolean)[0] || ''; }
  name = name.replace(/^@/, '');
  if (!name || !/^[\w.]+$/.test(name)) throw new Error('Enter a valid creator username or profile URL.');
  return name;
}
async function addCreator(platform, username) { return platform === 'tiktok' ? post('/api/users', { username }) : post(`/api/${platform}/monitor`, { username, monitored: true }); }
async function previewCreator() {
  const form = document.querySelector('[data-form="add-creator"]'); const platform = form.elements.platform.value, draft = form.elements.username.value, username = usernameOf(draft, platform);
  const result = platform === 'tiktok' ? await post('/api/users/preview', { username }) : await api(query(`/api/${platform}/preview`, { username }));
  if (!form.isConnected || form.elements.platform.value !== platform || form.elements.username.value !== draft) return;
  state.preview = result;
  form.querySelector('[type="submit"]').disabled = result.addable === false;
  patchContent(form.querySelector('#creator-preview'), `<div class="preview-profile">${avatar(result)}<div><strong>${esc(result.nickname || result.username || username)}</strong><small>@${esc(result.username || username)} · ${count(result.total_posts ?? result.posts)} posts · ${count(result.followers)} followers</small>${result.error || result.message ? `<small>${esc(result.error || result.message)}</small>` : ''}</div>${badge(result.exists === false ? 'not found' : 'ready')}</div>`);
}
async function creatorDialog(id) {
  const dialog = modal('Creator details', skeleton(), { wide: true, className: 'creator-modal', delay: 150 });
  try {
    const [user, posts, logs] = await Promise.all([api(`/api/users/${id}`), api(`/api/users/${id}/posts?limit=50`), api(`/api/users/${id}/logs?limit=50`)]);
    if (!isModalActive(dialog)) return;
    state.creator = user;
    updateModal(dialog, user.nickname || user.username, `<div class="creator-detail-header">${avatar(user)}<div><h3>@${esc(user.username)}</h3><p>${icon(user.platform)}${esc(user.platform)} · ${count(user.followers)} followers · ${count(user.following)} following</p></div>${badge(user.enabled ? user.status : 'disabled')}</div><div class="stat-grid mini"><div class="stat-card"><span>Posts</span><strong>${count(user.total_posts)}</strong></div><div class="stat-card"><span>Collected</span><strong>${count(user.downloaded_count)}</strong></div><div class="stat-card"><span>Pending</span><strong>${count(user.pending_count)}</strong></div><div class="stat-card"><span>Success</span><strong>${(user.success_rate || 0).toFixed(1)}%</strong></div></div><div class="button-row">${button('Scan now', `creator-scan:${id}`, { icon: 'retry' })}${button('Download all', `creator-download:${id}`, { icon: 'download' })}${button(user.enabled ? 'Disable' : 'Enable', `creator-toggle:${id}`, { icon: user.enabled ? 'pause' : 'play' })}${button('Copy username', `copy:${user.username}`, { icon: 'copy' })}</div><div class="detail-list"><div class="detail-row"><span>Last checked</span><strong>${time(user.last_checked)}</strong></div><div class="detail-row"><span>Last download</span><strong>${time(user.last_download)}</strong></div>${user.tiktok_uid ? `<div class="detail-row"><span>Platform UID</span><strong>${esc(user.tiktok_uid)}</strong></div>` : ''}${user.last_error ? `<p class="inline-notice">${esc(user.last_error)}</p>` : ''}</div><details open><summary>Recent posts (${posts.length})</summary>${posts.length ? `<div class="post-list">${posts.map(p => `<div class="post-row">${icon(p.post_type === 'image' ? 'image' : 'video')}<span>${esc(p.title || p.video_id)}</span>${badge(p.downloaded ? 'completed' : 'pending')}</div>`).join('')}</div>` : '<p class="muted">No posts indexed yet.</p>'}</details><details><summary>Creator logs (${logs.length})</summary>${logs.map(l => `<div class="creator-log">${badge(l.level)}<span>${esc(l.message)}</span><small>${time(l.created_at)}</small></div>`).join('') || '<p class="muted">No logs yet.</p>'}</details><form data-form="rename-creator" data-id="${id}">${field('Display name', 'nickname', user.nickname)}<div class="form-actions"><button class="button" type="submit">Save display name</button></div></form>`, { wide: true });
  } catch (error) { updateModal(dialog, 'Creator details', `<p class="inline-notice">${esc(error.message)}</p>`); }
}
async function downloadCreator(id) {
  const dialog = modal('Preparing download', skeleton(), { delay: 150 });
  try {
    const user = state.rows.find(r => r.id === Number(id)) || (state.creator?.id === Number(id) ? state.creator : await api(`/api/users/${id}`));
    if (!isModalActive(dialog)) return;
    if (user.platform !== 'tiktok') { confirmDialog('Download creator media', `Download the available media from @${user.username}?`, `creator-download-confirm:${id}`, 'Download all', false, '', dialog); return; }
    const preview = await api(`/api/users/${id}/download-all/preview`);
    confirmDialog('Download all posts', `${count(preview.to_download)} new posts from @${user.username}. ${count(preview.already_downloaded)} already collected. Estimated size: ${preview.estimated_size_human || bytes(preview.estimated_size)}.`, `creator-download-confirm:${id}`, 'Queue downloads', false, '', dialog);
  } catch (error) { updateModal(dialog, 'Download unavailable', `<p class="inline-notice">${esc(error.message)}</p>`); }
}
function confirmDialog(title, description, action, label = 'Confirm', danger = false, extra = '', dialog = null) {
  const html = `<p class="modal-intro">${esc(description)}</p>${extra}<div class="form-actions">${button('Cancel', 'close-modal')}${button(label, action, { kind: danger ? 'danger' : 'primary' })}</div>`;
  return dialog ? updateModal(dialog, title, html) : modal(title, html);
}
function importDialog() { modal('Import creators', `<form data-form="import-creators"><p class="modal-intro">Paste usernames or profile links, one per line. Existing creators are skipped.</p><label class="field"><span>Platform</span>${selectMenu([['tiktok', 'TikTok'], ['instagram', 'Instagram'], ['x', 'X']], state.platform || 'tiktok', { label: 'Platform', name: 'platform' })}</label><label class="field"><span>Creators</span><textarea name="usernames" rows="7" placeholder="@creator_one&#10;@creator_two" required></textarea></label><label class="button upload-control">${icon('upload')}Load .txt or .csv<input type="file" accept=".txt,.csv,.json" data-import-file aria-label="Load creator list"></label><div class="form-actions"><button type="submit" class="button primary">Import creators</button></div>${creatorProgressMarkup()}</form>`); }
async function exportCreators() {
  let page = 1, all = [], pages = 1;
  do { const result = await api(query('/api/users', { page, page_size: 200, platform: state.platform })); all.push(...result.items); pages = result.pages; page++; } while (page <= pages);
  const csv = [['platform', 'username', 'nickname', 'enabled'], ...all.map(u => [u.platform, u.username, u.nickname, u.enabled])].map(row => row.map(v => `"${( /^[=+@\-\t\r]/.test(String(v)) ? "'" + String(v) : String(v)).replaceAll('"', '""')}"`).join(',')).join('\r\n');
  downloadText('vydlora-creators.csv', csv, 'text/csv'); toast(`${all.length} creators exported`);
}
async function mediaDialog(id) {
  const dialog = modal('Opening media', `<div class="media-viewer" aria-busy="true">${skeleton()}</div><div class="media-viewer-info"><span>Loading media…</span></div><div class="media-loading-actions" aria-hidden="true"></div>`, { wide: true, className: 'viewer-modal', delay: 150 });
  try { const [media, download] = await Promise.all([api(`/api/downloads/${id}/media`), api(`/api/downloads/${id}`)]); if (!isModalActive(dialog)) return; state.mediaModal = dialog; state.media = { ...media, download }; state.mediaIndex = 0; renderMedia(); }
  catch (error) { updateModal(dialog, 'Media unavailable', `<p class="inline-notice">${esc(error.message)}</p>`); }
}
function renderMedia() {
  const m = state.media;
  if (!m || !isModalActive(state.mediaModal)) return;
  const safeMediaPath = path => typeof path === 'string' && /^\/api\/downloads\/\d+\/(?:file|image\/\d+)$/.test(path) ? path : '';
  updateModal(state.mediaModal, m.title || m.download.video_id || 'Media preview', `<div class="media-viewer">${m.type === 'image' ? `<img src="${esc(safeMediaPath(m.images[state.mediaIndex]))}" alt="${esc(m.title || 'Downloaded image')}">${m.images.length > 1 ? `<div class="gallery-controls">${button('Previous', 'media-prev', { disabled: !state.mediaIndex })}<span>${state.mediaIndex + 1} / ${m.images.length}</span>${button('Next', 'media-next', { disabled: state.mediaIndex >= m.images.length - 1 })}</div>` : ''}` : `<video src="${esc(safeMediaPath(m.video_url))}" poster="/api/downloads/${m.id}/thumbnail" controls playsinline preload="metadata"></video>`}</div><div class="media-viewer-info"><span>${esc(m.download.username ? '@' + m.download.username : m.download.source)} · ${bytes(m.download.file_size)} · ${time(m.download.created_at)}</span>${m.download.remote_path ? badge('cloud') : badge('local')}</div><div class="button-row"><a class="button primary" href="/api/downloads/${m.id}/download">${icon('download')}Save file</a>${button('Delete', `media-delete:${m.id}`, { icon: 'trash', kind: 'danger subtle' })}</div>`, { wide: true, className: `viewer-modal${m.type === 'video' ? ' video-viewer' : ''}` });
  if (m.type === 'video') fitVideoModal(document.querySelector('.video-viewer video'), document.querySelector(`[data-file-id="${m.id}"] img`));
}
async function searchDialog() { modal('Search your workspace', `<div class="global-search">${icon('search')}<input type="search" data-global-search placeholder="Creators, downloads, queue, logs…" aria-label="Search workspace" autocomplete="off"></div><div id="search-results">${empty('Find something good', 'Type at least two characters to search Lora 1.', 'search')}</div>`, { wide: true, className: 'search-modal' }); }
let searchGeneration = 0;
async function doGlobalSearch(value) {
  const generation = ++searchGeneration;
  const el = document.querySelector('#search-results'); if (!el) return;
  if (value.length < 2) { patchContent(el, empty('Find something good', 'Type at least two characters to search Lora 1.', 'search')); el.removeAttribute('aria-busy'); return; }
  el.setAttribute('aria-busy', 'true');
  try { const result = await api(query('/api/search', { q: value })); if (generation !== searchGeneration || !el.isConnected) return;
    const rows = [...result.users.map(u => `<button class="search-result" data-action="creator:${u.id}">${avatar(u)}<span><strong>${esc(u.nickname || u.username)}</strong><small>Creator · @${esc(u.username)}</small></span>${icon('chevron')}</button>`), ...result.downloads.map(d => `<button class="search-result" data-action="media:${d.id}">${icon('video')}<span><strong>${esc(d.title || d.video_id)}</strong><small>Download · ${esc(d.username)}</small></span>${icon('chevron')}</button>`), ...result.queue.map(q => `<button class="search-result" data-action="go-queue">${icon('queue')}<span><strong>${esc(q.title || q.video_id || q.url)}</strong><small>Queue · ${esc(q.status)}</small></span>${icon('chevron')}</button>`), ...result.logs.map(l => `<div class="search-result">${icon('logs')}<span><strong>${esc(l.message)}</strong><small>Log · ${esc(l.source)} · ${time(l.created_at)}</small></span></div>`)];
    patchContent(el, rows.length ? rows.join('') : empty('No matches', 'Try a different name or keyword.', 'search'));
  } catch (error) { if (el.isConnected && generation === searchGeneration) patchContent(el, `<p class="inline-notice">${esc(error.message)}</p>`); }
  finally { if (el.isConnected && generation === searchGeneration) el.removeAttribute('aria-busy'); }
}
async function notificationsDialog(dialog) {
  if (dialog && !isModalActive(dialog)) return;
  if (!dialog) dialog = modal('Notifications', skeleton(), { className: 'notifications-modal', delay: 150 });
  state.notificationsModal = dialog;
  try {
    const items = await api('/api/notifications?limit=50');
    updateModal(dialog, 'Notifications', `<div class="notification-actions">${button('Mark all as read', 'notifications-read', { icon: 'check' })}</div>${items.length ? items.map(n => `<article data-key="notification:${n.id}" class="notification-item ${n.read ? 'read' : ''}">${icon(n.type === 'success' ? 'check' : 'bell')}<div><strong>${esc(n.title)}</strong><p>${esc(n.body)}</p><small>${time(n.created_at)}</small></div>${!n.read ? iconButton('Mark as read', `notification-read:${n.id}`, 'check') : ''}</article>`).join('') : empty('You’re up to date', 'Lora 1 notifications will appear here.', 'bell')}`);
  } catch (error) { updateModal(dialog, 'Notifications', `<p class="inline-notice">${esc(error.message)}</p>`); }
}
function showLock() { state.locked = true; document.querySelector('.app-shell').classList.add('locked'); document.querySelector('.app-shell').inert = true; closeModal(); modal('Your workspace is locked', `<div class="lock-emblem">${icon('lock')}</div><p class="lock-description">Your downloads and monitoring are still running.</p><form data-form="unlock">${field('Four-digit PIN', 'pin', '', { type: 'password', required: true })}<p class="pin-error" role="alert"></p><button class="button primary" type="submit">Unlock Vydlora</button></form>`, { className: 'lock-modal', closeable: false }); }
function shortcutsDialog() { modal('Keyboard shortcuts', `<div class="shortcut-list">${Object.entries(shortcuts()).map(([id, value]) => `<div><span>${esc(shortcutNames[id])}</span><kbd>${esc(value.replaceAll('Control', 'Ctrl').replaceAll('+', ' + ') || 'Disabled')}</kbd></div>`).join('')}</div><p class="help-text">Browser shortcuts for zoom, full screen, back, and forward continue to work.</p><div class="form-actions">${button('Customize shortcuts', 'shortcut-settings')}</div>`); }

async function actionHandler(action, target) {
  const [name, id, extra] = action.split(':');
  const owner = getModalHandle();
  if (state.locked) return;
  if (!['library-menu', 'library-options', 'file-menu'].includes(name)) closeMenu();
  const go = { 'go-home': '/', 'go-queue': '/queue', 'go-terminal': '/terminal', connection: '/settings/connection', 'download-settings': '/settings/downloads', 'shortcut-settings': '/settings/appearance' };
  if (go[name]) return navigate(go[name]);
  switch (name) {
    case 'close-modal': if (isModalActive(owner)) closeModal(); return;
    case 'refresh': await loadPage(); healthCheck(); return;
    case 'sidebar': { const el = document.querySelector('.app-shell'); if (matchMedia('(max-width: 768px)').matches) { el.classList.remove('mobile-open'); syncSidebarA11y(); document.querySelector('[data-action="mobile-menu"]').focus(); return; } if (matchMedia('(max-width: 1050px)').matches) { el.classList.toggle('tablet-expanded'); syncSidebarA11y(); return; } const collapsed = el.classList.toggle('collapsed'); localStorage.setItem('vydlora.collapsed', collapsed); const btn = document.querySelector('[data-action="sidebar"]'); btn.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar'); return; }
    case 'mobile-menu': document.querySelector('.app-shell').classList.add('mobile-open'); syncSidebarA11y(); document.querySelector('[data-action="sidebar"]').focus(); return;
    case 'mobile-close': document.querySelector('.app-shell').classList.remove('mobile-open', 'tablet-expanded'); syncSidebarA11y(); return;
    case 'profile': modal('Personal workspace', `<div class="profile-menu">${button('Settings', 'connection', { icon: 'settings' })}${button('Keyboard shortcuts', 'shortcuts', { icon: 'keyboard' })}${button('Appearance', 'shortcut-settings', { icon: 'sun' })}${button('Help & about', 'help', { icon: 'help' })}${hasPin() ? button('Lock workspace', 'lock', { icon: 'lock' }) : ''}</div><p class="help-text">Your local workspace, powered by Lora 1. Your downloads and settings stay on this device.</p>`); return;
    case 'home-mode': if (state.homeMode === id) return; state.homeUrl = document.querySelector('#download-input')?.value || ''; state.homeMode = id; updateHeader(); updateHomeView({ modeChanged: true }); return;
    case 'home-platform': state.homeUrl = document.querySelector('#download-input')?.value || ''; state.homePlatform = id; updateHomeView(); return;
    case 'library-type': state.status = id; await refreshLibrary(); return;
    case 'library-menu': openMenu(target, libraryFilterMenu(id, state), { wide: id === 'timeframe', label: `${id === 'timeframe' ? 'Timeframe' : id === 'source' ? 'Source' : 'Sort'} options` }); return;
    case 'library-sort': state.sort = action.slice('library-sort:'.length); await refreshLibrary(); return;
    case 'library-source': state.librarySource = id; await refreshLibrary(); return;
    case 'library-period': state.libraryPeriod = id; state.libraryFrom = ''; state.libraryTo = ''; await refreshLibrary(); return;
    case 'library-clear': state.status = ''; state.librarySource = ''; state.libraryPeriod = ''; state.libraryFrom = ''; state.libraryTo = ''; state.search = ''; state.librarySearchOpen = false; await refreshLibrary(); return;
    case 'library-search': state.librarySearchOpen = true; await loadPage({ silent: true }); document.querySelector('[data-list-search]')?.focus(); return;
    case 'library-search-close': state.librarySearchOpen = false; state.search = ''; await refreshLibrary(); document.querySelector('[data-action="library-search"]')?.focus(); return;
    case 'library-select-all': state.rows.forEach(row => state.selected.add(row.id)); document.querySelectorAll('[data-select]').forEach(input => { input.checked = true; }); updateSelection(); return;
    case 'library-options': openMenu(target, `<button type="button" role="menuitem" tabindex="-1" data-action="library-select-all">${icon('check')}<span>Select all files on this page</span></button><div class="file-menu-divider"></div><button type="button" role="menuitem" tabindex="-1" data-action="shortcuts">${icon('keyboard')}<span>Keyboard shortcuts</span></button>`); return;
    case 'file-menu': { const row = state.rows.find(row => row.id === Number(id)); if (row) openMenu(target, fileActionMenu(row), { label: 'File actions' }); return; }
    case 'batch-help': modal('A little link goes a long way.', `<p class="modal-intro">Paste one link per line to collect multiple posts. Vydlora submits each link to Lora 1 and reports its result independently.</p><p class="help-text">TikTok, Instagram, X, and other sites supported by your engine use their existing download engines. Original quality is selected by Lora 1.</p>${button('Got it', 'close-modal', { kind: 'primary' })}`); return;
    case 'platform': state.platform = id; state.page = 1; state.selected.clear(); selectPlatform(document.querySelector('.monitor-platform-tabs'), id); await loadPage({ silent: true }); return;
    case 'status': state.status = state.status === id ? '' : id; state.page = 1; state.selected.clear(); await loadPage(); return;
    case 'previous-page': state.page = Math.max(1, state.page - 1); state.selected.clear(); await loadPage(); return;
    case 'next-page': state.page++; state.selected.clear(); await loadPage(); return;
    case 'view': state.view = id; localStorage.setItem('vydlora.library-view', id); await loadPage({ silent: true }); document.querySelector(`[data-action="view:${id}"]`)?.focus({ preventScroll: true }); return;
    case 'add-creator': addCreatorDialog(); return;
    case 'preview-creator': await previewCreator(); return;
    case 'import-dialog': importDialog(); return;
    case 'export-creators': await exportCreators(); return;
    case 'creator': await creatorDialog(Number(id)); return;
    case 'creator-scan': { const result = await post(`/api/users/${id}/scan`); toast(result.detail); await loadPage({ silent: true }); return; }
    case 'creator-toggle': { const u = state.rows.find(r => r.id === Number(id)) || state.creator || await api(`/api/users/${id}`); await patch(`/api/users/${id}`, { enabled: !u.enabled }); if (state.creator?.id === Number(id)) state.creator.enabled = !u.enabled; toast('Monitoring updated'); if (isModalActive(owner)) closeModal(); await loadPage({ silent: true }); return; }
    case 'creator-delete': confirmDialog('Remove creator?', 'This removes the creator from monitoring. Downloaded media is kept.', `creator-delete-confirm:${id}`, 'Remove creator', true); return;
    case 'creator-delete-confirm': await del(`/api/users/${id}`); if (isModalActive(owner)) closeModal(); toast('Creator removed'); await loadPage(); return;
    case 'creator-download': await downloadCreator(id); return;
    case 'creator-download-confirm': { const u = state.rows.find(r => r.id === Number(id)) || state.creator || await api(`/api/users/${id}`); if (isModalActive(owner)) closeModal(); if (u.platform === 'tiktok') { const result = await post(`/api/users/${id}/download-all`); toast(`${result.queued} posts queued`); } else { toast(`Collecting @${u.username}…`); const result = await post(`/api/${u.platform}/download`, { url: `https://${u.platform === 'x' ? 'x.com' : 'www.instagram.com'}/${u.username}/` }); if (result.ok === false || result.error) throw new Error(result.error || 'Some downloads failed.'); toast(`${result.count || 0} items collected`); } await loadPage({ silent: true }); return; }
    case 'toggle-monitoring': await put('/api/settings', { monitoring_paused: !state.settings.monitoring_paused }); toast(state.settings.monitoring_paused ? 'Monitoring resumed' : 'Monitoring paused'); await loadPage({ silent: true }); return;
    case 'scan-all': { const result = await post('/api/workers/scan-now'); toast(result.detail); return; }
    case 'job': await post(`/api/queue/${extra}/${id}`); toast(`Download ${id === 'retry' ? 'retried' : id === 'pause' ? 'paused' : id === 'resume' ? 'resumed' : 'cancelled'}`); if (isModalActive(owner)) closeModal(); await loadPage({ silent: true }); return;
    case 'job-details': { const job = state.rows.find(j => j.id === Number(id)); if (!job) return; modal('Download details', `<h3>${esc(job.title || job.video_id)}</h3><div class="detail-list">${[['Status', job.status], ['Creator', job.username], ['Source', job.source], ['Worker', job.worker || 'Waiting'], ['Attempts', job.attempts], ['Added', time(job.created_at)], ['Duration', job.duration ? `${job.duration.toFixed(1)} seconds` : '-']].map(([label, value]) => `<div class="detail-row"><span>${label}</span><strong>${esc(value)}</strong></div>`).join('')}</div><label class="field"><span>Source link</span><input value="${esc(job.url)}" readonly></label>${job.error && job.status !== 'completed' ? `<p class="inline-notice">${esc(job.error)}</p>` : ''}<div class="button-row">${button('Copy link', `copy:${job.url}`, { icon: 'copy' })}${['failed','cancelled'].includes(job.status) ? button('Retry download', `job:retry:${job.id}`, { icon: 'retry', kind: 'primary' }) : ''}</div>`); return; }
    case 'queue-retry-failed': { const jobs = []; let page = 1, pages; do { const data = await api(`/api/queue?status=failed&page=${page}&page_size=200`); jobs.push(...data.items); pages = data.pages; page++; } while (page <= pages); const result = await Promise.allSettled(jobs.map(j => post(`/api/queue/${j.id}/retry`))); toast(`${result.filter(r => r.status === 'fulfilled').length} downloads requeued`); await loadPage({ silent: true }); return; }
    case 'job-remove': confirmDialog('Remove queue job?', 'This removes the job from the queue.', `job-remove-confirm:${id}`, 'Remove', true); return;
    case 'job-remove-confirm': await post('/api/queue/bulk-remove', { ids: [Number(id)] }); if (isModalActive(owner)) closeModal(); await loadPage(); return;
    case 'queue-pause-all': await post('/api/queue/pause-all'); toast('Queue paused'); await loadPage({ silent: true }); return;
    case 'queue-resume-all': await post('/api/queue/resume-all'); toast('Queue resumed'); await loadPage({ silent: true }); return;
    case 'queue-clear-completed': await post('/api/queue/clear-completed'); toast('Completed jobs cleared'); await loadPage({ silent: true }); return;
    case 'queue-clear-dialog': confirmDialog('Clear the queue?', 'This removes all queued jobs. Downloaded media is kept.', 'queue-clear-confirm', 'Clear queue', true); return;
    case 'queue-clear-confirm': await post('/api/queue/clear'); if (isModalActive(owner)) closeModal(); await loadPage(); return;
    case 'deselect': state.selected.clear(); await loadPage({ silent: true }); return;
    case 'bulk-enable': case 'bulk-disable': case 'bulk-scan': { const ids = [...state.selected]; if (!ids.length) return; const results = await Promise.allSettled(ids.map(uid => name === 'bulk-scan' ? post(`/api/users/${uid}/scan`) : patch(`/api/users/${uid}`, { enabled: name === 'bulk-enable' }))); const failed = results.filter(r => r.status === 'rejected'); toast(`${ids.length - failed.length} creators updated${failed.length ? `; ${failed.length} failed` : ''}`, Boolean(failed.length)); state.selected.clear(); await loadPage({ silent: true }); return; }
    case 'bulk-delete': if (!state.selected.size) return; confirmDialog(`Remove ${state.selected.size} selected items?`, state.route === '/downloads' ? 'Choose whether to delete the media files too. Deleting files cannot be undone.' : 'The selected records will be removed. This cannot be undone.', 'bulk-delete-confirm', 'Remove selected', true, state.route === '/downloads' ? '<label class="check-label"><input type="checkbox" id="delete-files">Also delete media files</label>' : ''); return;
    case 'bulk-delete-confirm': await bulkDelete(owner); return;
    case 'activity-clear-dialog': confirmDialog('Clear all activity?', 'This permanently clears the activity feed. Downloaded media is kept.', 'activity-clear-confirm', 'Clear activity', true); return;
    case 'activity-clear-confirm': await post('/api/activity/delete', { ids: [] }); if (isModalActive(owner)) closeModal(); await loadPage(); return;
    case 'export-logs': downloadText('vydlora-logs.txt', await api(query('/api/logs/export', { level: state.status, source: state.logSource }))); toast('Logs exported'); return;
    case 'terminal-pause': state.terminalPaused = !state.terminalPaused; await loadPage({ silent: true }); return;
    case 'terminal-clear': state.terminalInitialized = true; state.terminalLines = []; document.querySelector('#terminal-output').innerHTML = ''; toast('Terminal view cleared'); return;
    case 'terminal-copy': await navigator.clipboard.writeText(state.terminalLines.map(l => `${l.ts} | ${l.level} | ${l.source} | ${l.message}`).join('\n')); toast('Terminal copied'); return;
    case 'media': await mediaDialog(Number(id)); return;
    case 'media-prev': state.mediaIndex = Math.max(0, state.mediaIndex - 1); renderMedia(); return;
    case 'media-next': state.mediaIndex = Math.min(state.media.images.length - 1, state.mediaIndex + 1); renderMedia(); return;
    case 'media-telegram': confirmDialog('Send media to Telegram?', 'This sends the media to the approved chats configured in Lora 1.', `media-telegram-confirm:${id}`, 'Send to Telegram'); return;
    case 'media-telegram-confirm': { const result = await post(`/api/downloads/${id}/telegram`); if (isModalActive(owner)) closeModal(); toast(result.detail); return; }
    case 'media-delete': confirmDialog('Delete this download?', 'Remove the record from your library, or also delete its media file.', `media-delete-confirm:${id}`, 'Delete download', true, '<label class="check-label"><input type="checkbox" id="delete-files">Also delete media files</label>'); return;
    case 'media-delete-confirm': await del(`/api/downloads/${id}?delete_file=${Boolean(document.querySelector('#delete-files')?.checked)}`); if (isModalActive(owner)) closeModal(); toast('Download deleted'); await loadPage(); return;
    case 'search-dialog': await searchDialog(); return;
    case 'notifications': await notificationsDialog(); return;
    case 'notifications-read': { const dialog = state.notificationsModal; await post('/api/notifications/read'); state.notifications = 0; updateConnection(); if (isModalActive(dialog)) await notificationsDialog(dialog); return; }
    case 'notification-read': { const dialog = state.notificationsModal; await post(`/api/notifications/read?notif_id=${id}`); state.notifications = Math.max(0, state.notifications - 1); updateConnection(); if (isModalActive(dialog)) await notificationsDialog(dialog); return; }
    case 'copy': await navigator.clipboard.writeText(action.slice(5)); toast('Copied'); return;
    case 'lock': if (hasPin()) showLock(); else { toast('Set up a PIN in Security settings first.'); await navigate('/settings/security'); } return;
    case 'shortcuts': shortcutsDialog(); return;
    case 'help': { const dialog = modal('About Vydlora', skeleton(), { delay: 150 }); let version = 'Unavailable'; try { version = (await api('/api/ping')).version; } catch {} updateModal(dialog, 'About Vydlora', `<div class="about-brand"><img src="/assets/mark.svg" alt=""><h3>Vydlora</h3><p>Your media, beautifully collected.</p></div><div class="detail-list"><div class="detail-row"><span>Web app</span><strong>1.0.0</strong></div><div class="detail-row"><span>Lora 1 engine</span><strong>${esc(version)}</strong></div></div><p class="help-text">Download links, monitor creators, manage the queue, and connect Telegram and MEGA from one workspace. The engine keeps working when you close this browser tab. Browser controls handle zoom, full screen, and navigation.</p><div class="help-links"><a href="https://github.com/wavrix/vydlora" target="_blank" rel="noopener noreferrer">Documentation ${icon('arrow')}</a><a href="https://github.com/wavrix/vydlora/releases" target="_blank" rel="noopener noreferrer">What’s new ${icon('arrow')}</a><a href="https://github.com/wavrix/vydlora/issues" target="_blank" rel="noopener noreferrer">Troubleshooting ${icon('arrow')}</a><a href="https://github.com/wavrix/vydlora/issues/new" target="_blank" rel="noopener noreferrer">Send feedback ${icon('arrow')}</a></div><p class="help-text">Automatic updates are not configured. Update Vydlora and Lora 1 from their source projects.</p>${button('System status', 'system-status', { icon: 'activity' })}`); return; }
    case 'system-status': await navigate('/settings/overview'); return;
    case 'cookie-delete': confirmDialog('Remove cookie profile?', 'The saved session and its managed cookie file will be removed from Lora 1.', `cookie-delete-confirm:${id}`, 'Remove profile', true); return;
    case 'cookie-delete-confirm': await del(`/api/cookies/${id}`); if (isModalActive(owner)) closeModal(); await loadPage(); return;
    case 'platform-cookie-delete': confirmDialog('Remove cookies?', `The saved ${id === 'x' ? 'X' : 'Instagram'} session will be removed.`, `platform-cookie-delete-confirm:${id}`, 'Remove cookies', true); return;
    case 'platform-cookie-delete-confirm': await del(`/api/${id}/cookies`); if (isModalActive(owner)) closeModal(); await loadPage(); return;
    case 'reset-dialog': modal('Reset Lora 1 data', `<form data-form="reset"><p class="inline-notice">This permanently deletes engine records, creators, settings, and sessions. This affects this workspace’s saved data.</p>${field('Type RESET to confirm', 'confirmation', '', { required: true })}<label class="check-label"><input type="checkbox" name="wipe_files">Also permanently delete downloaded files</label><div class="form-actions">${button('Cancel', 'close-modal')}<button type="submit" class="button danger">Reset Lora 1</button></div></form>`); return;
    default: if (await settingsAction(action, () => loadPage({ silent: true }))) return;
  }
}
async function bulkDelete(owner) {
  const ids = [...state.selected];
  if (state.route === '/monitor') { const results = await Promise.allSettled(ids.map(id => del(`/api/users/${id}`))); const failures = results.filter(r => r.status === 'rejected'); if (failures.length) toast(`${failures.length} creators could not be removed`, true); }
  else if (state.route === '/queue') await post('/api/queue/bulk-remove', { ids });
  else if (state.route === '/activity') await post('/api/activity/delete', { ids });
  else if (state.route === '/downloads') await post('/api/downloads/bulk-delete', { ids, delete_file: Boolean(document.querySelector('#delete-files')?.checked) });
  state.selected.clear(); if (isModalActive(owner)) closeModal(); toast('Selection removed'); await loadPage();
}

document.addEventListener('click', async event => {
  const target = event.target.closest('[data-action]');
  const link = event.target.closest('a[href]');
  const href = link?.getAttribute('href');
  const appLink = href === '/' || Object.keys(routes).some(route => route !== '/' && (href === route || href?.startsWith(route + '/')));
  if (link && appLink && !event.ctrlKey && !event.metaKey && !event.shiftKey) { event.preventDefault(); return navigate(href); }
  if (!target || target.disabled || target.dataset.busy) return;
  if (state.locked) return;
  const action = target.dataset.action;
  if (target.closest('.menu-popover')) closeMenu({ focus: true });
  const asyncAction = !/^(close-modal|sidebar|mobile-menu|mobile-close|home-platform|home-mode|library-|file-menu|deselect|profile|batch-help|add-creator|import-dialog|shortcuts|lock)/.test(action);
  try { if (asyncAction) { target.dataset.busy = 'true'; target.setAttribute('aria-busy', 'true'); } await actionHandler(action, target); }
  catch (error) { toast(error.message, true); }
  finally { delete target.dataset.busy; target.removeAttribute('aria-busy'); }
});
document.addEventListener('submit', async event => {
  const form = event.target; if (!form.dataset.form) return; event.preventDefault();
  if (form.dataset.busy || (form.dataset.form === 'home' && state.homeBusy)) return;
  const submit = form.querySelector('[type="submit"]');
  let finishCreatorProgress, creatorProgressError;
  form.dataset.busy = 'true'; if (submit) { submit.disabled = true; submit.setAttribute('aria-busy', 'true'); }
  try {
    if (form.dataset.form === 'library-dates') {
      const from = form.elements.from.value, to = form.elements.to.value;
      if (!from && !to) throw new Error('Choose a start date or an end date.');
      if (from && to && from > to) throw new Error('The start date must be on or before the end date.');
      state.libraryPeriod = 'custom'; state.libraryFrom = from; state.libraryTo = to;
      await refreshLibrary();
    } else if (form.dataset.form === 'home') {
      state.homeUrl = form.elements.url.value;
      if (state.homeMode === 'monitor') { const value = state.homeUrl.trim(); let platform = state.homePlatform || (/instagram\.com/i.test(value) ? 'instagram' : /(?:x|twitter)\.com/i.test(value) ? 'x' : 'tiktok'); addCreatorDialog(platform, usernameOf(value, platform)); return; }
      const urls = [...new Set(state.homeUrl.split(/[\r\n]+/).map(u => u.trim()).filter(Boolean))];
      for (const url of urls) { let parsed; try { parsed = new URL(url); } catch { throw new Error('Use a complete link beginning with https:// or http://.'); } if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Use an HTTP or HTTPS media link.'); const domains = { tiktok: ['tiktok.com'], instagram: ['instagram.com'], x: ['x.com', 'twitter.com'] }[state.homePlatform]; if (domains && !domains.some(d => parsed.hostname === d || parsed.hostname.endsWith('.' + d))) throw new Error('This link belongs to another platform. Choose All platforms to let Lora 1 detect it.'); }
      const submittedDraft = state.homeUrl;
      state.homeBusy = true; syncHomeBusy();
      const requests = urls.map(url => ({ id: ++state.requestSequence, url, done: false })); state.homeRequests.unshift(...requests); state.homeRequests = state.homeRequests.slice(0, 100);
      patchContent(document.querySelector('#home-requests'), homeRequestRows());
      for (const request of requests) {
        try { const result = await post('/api/downloads/url', { url: request.url }); request.done = true; request.jobId = result.job_id; request.status = result.queued ? 'pending' : 'completed'; request.title = result.title || request.url; request.message = result.warning || result.detail || (result.queued ? `Added to ${result.platform || 'download'} queue` : `${result.completed || 0} items collected`); toast(request.message); }
        catch (error) { request.error = error.message; }
        patchContent(document.querySelector('#home-requests'), homeRequestRows());
      }
      if (requests.every(r => !r.error) && state.homeUrl === submittedDraft) {
        state.homeUrl = '';
        const input = document.querySelector('#download-input');
        if (input) { input.value = ''; input.style.height = ''; }
      }
      healthCheck(); refreshSidebar(); refreshHomeRecent();
    } else if (form.dataset.form === 'add-creator') {
      const platform = form.elements.platform.value, username = usernameOf(form.elements.username.value, platform);
      finishCreatorProgress = startCreatorProgress(form, `Adding @${username}…`, 'Checking the profile and setting up monitoring. This can take a moment.');
      const result = await addCreator(platform, username);
      if (result.ok === false) throw new Error(result.detail || 'Creator could not be added.');
      const active = form.isConnected;
      if (active) closeModal();
      toast(`@${username} added to monitoring`);
      if (state.route === '/monitor') {
        if (active) { state.platform = platform; state.page = 1; state.selected.clear(); selectPlatform(document.querySelector('.monitor-platform-tabs'), platform); }
        await loadPage({ silent: true });
      }
    } else if (form.dataset.form === 'rename-creator') { await patch(`/api/users/${form.dataset.id}`, { nickname: form.elements.nickname.value }); toast('Display name saved'); await loadPage({ silent: true }); }
    else if (form.dataset.form === 'import-creators') {
      const platform = form.elements.platform.value; const usernames = [...new Set(form.elements.usernames.value.split(/[\r\n,;]+/).map(v => v.trim()).filter(Boolean).map(v => usernameOf(v, platform)))];
      if (usernames.length > 500) throw new Error('Import up to 500 creators at a time.');
      if (!usernames.length) throw new Error('Enter at least one creator.');
      finishCreatorProgress = startCreatorProgress(form, `Adding ${usernames.length} ${usernames.length === 1 ? 'creator' : 'creators'}…`, 'Setting up monitoring. This can take a moment.');
      let result;
      if (platform === 'tiktok') result = await post('/api/users/import', { usernames });
      else { result = { added: [], skipped: [], failed: [] }; for (const username of usernames) { try { const added = await addCreator(platform, username); if (added.ok === false) throw new Error(added.detail || 'Creator could not be added.'); result.added.push(username); } catch { result.failed.push(username); } const progress = form.querySelector('[data-creator-progress-detail]'); if (progress) progress.textContent = `${result.added.length + result.failed.length} / ${usernames.length} processed`; } }
      const active = form.isConnected;
      if (active) modal('Import complete', `<div class="import-summary">${badge('completed')}<h3>${result.added.length} creators added</h3><p>${result.skipped.length} skipped · ${result.failed.length} failed</p>${result.failed.length ? `<p class="inline-notice">Failed: ${result.failed.map(esc).join(', ')}</p>` : ''}${button('Done', 'close-modal', { kind: 'primary' })}</div>`);
      else toast(`${result.added.length} creators added to monitoring`);
      if (state.route === '/monitor') {
        if (active) { state.platform = platform; state.page = 1; state.selected.clear(); selectPlatform(document.querySelector('.monitor-platform-tabs'), platform); }
        await loadPage({ silent: true });
      }
    } else if (form.dataset.form === 'unlock') {
      try { await verifyPin(form.elements.pin.value); state.locked = false; document.querySelector('.app-shell').classList.remove('locked'); document.querySelector('.app-shell').inert = false; closeModal(); await loadPage(); refreshSidebar(); }
      catch (error) { form.querySelector('.pin-error').textContent = error.message; form.elements.pin.value = ''; form.elements.pin.focus(); }
    } else if (form.dataset.form === 'reset') {
      if (form.elements.confirmation.value !== 'RESET') throw new Error('Type RESET exactly to confirm.');
      const result = await post(`/api/reset?confirm=RESET&wipe_files=${form.elements.wipe_files.checked}`); closeModal(); toast(`Reset complete. ${result.rows_deleted} records removed.`); await loadPage(); healthCheck();
    } else { if (await submitSettings(form)) { if (state.route === '/settings') await loadPage({ silent: true }); healthCheck(); } }
  } catch (error) { creatorProgressError = error; toast(error.message, true); }
  finally { finishCreatorProgress?.(creatorProgressError); delete form.dataset.busy; if (submit) { submit.disabled = false; submit.removeAttribute('aria-busy'); } if (form.dataset.form === 'home') { state.homeBusy = false; syncHomeBusy(); } }
});
document.addEventListener('input', event => {
  const el = event.target;
  if (el.matches('[data-list-search]')) { clearTimeout(searchTimer); const focus = { start: el.selectionStart, end: el.selectionEnd }; state.search = el.value; state.page = 1; searchTimer = setTimeout(async () => { await loadPage({ silent: true }); const next = document.querySelector('[data-list-search]'); next?.focus(); if (next && next.type !== 'search') next.setSelectionRange(focus.start, focus.end); }, 350); }
  if (el.matches('[data-global-search]')) { clearTimeout(searchTimer); searchTimer = setTimeout(() => { if (el.isConnected) doGlobalSearch(el.value.trim()); }, 300); }
  if (el.matches('#download-input')) { state.homeUrl = el.value; el.style.height = 'auto'; el.style.height = `${Math.min(120, Math.max(22, el.scrollHeight))}px`; }
  if (el.matches('[data-form="add-creator"] [name="username"]')) { const form = el.closest('form'); form.querySelector('[type="submit"]').disabled = false; document.querySelector('#creator-preview').innerHTML = ''; }
});
document.addEventListener('change', async event => {
  const el = event.target;
  try {
    if (el.matches('[data-filter]')) { const filter = el.dataset.filter; if (filter === 'status') state.status = el.value; if (filter === 'sort') state.sort = el.value; if (filter === 'log-source') state.logSource = el.value; if (filter === 'chart-days') state.chartDays = el.value; state.page = 1; state.selected.clear(); await loadPage({ silent: state.route === '/monitor' }); document.querySelector(`[data-filter="${filter}"]`)?.parentElement.querySelector('[data-select-menu]')?.focus({ preventScroll: true }); }
    if (el.matches('[data-select]')) { const id = Number(el.dataset.select); el.checked ? state.selected.add(id) : state.selected.delete(id); updateSelection(); }
    if (el.matches('[data-select-all]')) { for (const row of state.rows) el.checked ? state.selected.add(row.id) : state.selected.delete(row.id); document.querySelectorAll('[data-select]').forEach(input => { input.checked = state.selected.has(Number(input.dataset.select)); }); updateSelection(); }
    if (el.matches('[data-cookie-upload]')) { el.disabled = true; await uploadCookies(el); await loadPage({ silent: true }); }
    if (el.matches('[data-preview-platform]')) { const preview = document.querySelector('#creator-preview'); if (preview) preview.innerHTML = ''; el.closest('form').querySelector('[type="submit"]').disabled = false; }
    if (el.matches('[data-import-file]')) { const file = el.files[0]; if (!file) return; if (file.size > 2 * 1024 * 1024) throw new Error('Choose a creator list smaller than 2 MB.'); const text = await file.text(); const form = el.closest('form'); if (file.name.endsWith('.json')) { const data = JSON.parse(text); const rows = Array.isArray(data) ? data : data.items; if (!Array.isArray(rows)) throw new Error('The JSON file must contain a creator list.'); form.elements.usernames.value = rows.map(r => typeof r === 'string' ? r : r.username).filter(Boolean).join('\n'); } else if (text.startsWith('"platform","username"')) { const selected = form.elements.platform.value; const lines = text.split(/\r?\n/).slice(1); form.elements.usernames.value = lines.map(line => [...line.matchAll(/"((?:[^"]|"")*)"(?:,|$)/g)].map(m => m[1].replaceAll('""', '"'))).filter(row => row[0] === selected).map(row => row[1]).join('\n'); } else form.elements.usernames.value = text; }
  } catch (error) { toast(error.message, true); }
  finally { if (el.matches('[data-cookie-upload]')) el.disabled = false; }
});
function updateSelection() {
  document.querySelector('#selection-bar')?.classList.toggle('visible', state.selected.size > 0);
  const countEl = document.querySelector('#selected-count'); if (countEl) countEl.textContent = `${state.selected.size} selected`;
  const n = state.rows.filter(row => state.selected.has(row.id)).length;
  document.querySelectorAll('[data-select-all]').forEach(all => { all.checked = n > 0 && n === state.rows.length; all.indeterminate = n > 0 && n < state.rows.length; });
  document.querySelectorAll('[data-file-id]').forEach(card => card.classList.toggle('selected', state.selected.has(Number(card.dataset.fileId))));
}

document.addEventListener('error', event => { if (event.target instanceof HTMLImageElement && event.target.dataset.fallback) event.target.classList.add('image-failed'); }, true);
document.addEventListener('keydown', event => {
  const active = document.activeElement;
  const navigationOpen = document.querySelector('.app-shell').matches('.mobile-open,.tablet-expanded');
  if (navigationOpen && event.key === 'Escape' && !state.locked) { document.querySelector('.app-shell').classList.remove('mobile-open', 'tablet-expanded'); syncSidebarA11y(); document.querySelector('[data-action="mobile-menu"]').focus(); return; }
  if (active?.matches('[data-shortcut-input]')) { event.preventDefault(); if (event.key === 'Backspace' || event.key === 'Delete') active.value = ''; else if (!['Control', 'Alt', 'Shift', 'Meta'].includes(event.key)) { if (!event.ctrlKey && !event.metaKey && !event.altKey) return toast('Include Ctrl or Alt in a custom shortcut.', true); active.value = chord(event); } return; }
  const dialog = document.querySelector('.modal');
  if (dialog && event.key === 'Tab') { const focusable = [...dialog.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]):not([type="hidden"]),select,textarea,[tabindex="0"]')]; const first = focusable[0], last = focusable.at(-1); if (event.shiftKey && active === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && active === last) { event.preventDefault(); first?.focus(); } return; }
  if (event.key === 'Escape' && !dialog && isModalActive() && !state.locked) { closeModal(); return; }
  if (event.key === 'Escape' && dialog && !state.locked && dialog.querySelector('[data-action="close-modal"]')) { closeModal(); return; }
  if (state.locked) return;
  if (active?.matches('#download-input') && event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); active.closest('form').requestSubmit(); return; }
  if (active?.matches('input,textarea,select,[data-select-menu]')) return;
  const match = Object.entries(shortcuts()).find(([, value]) => value && value.toLowerCase() === chord(event).toLowerCase());
  if (!match) return; event.preventDefault();
  const actions = { search: 'search-dialog', add: 'add-creator', import: 'import-dialog', export: 'export-creators', sidebar: 'sidebar', terminal: 'go-terminal', refresh: 'refresh', shortcuts: 'shortcuts', lock: 'lock' };
  actionHandler(actions[match[0]], active).catch(error => toast(error.message, true));
});
window.addEventListener('popstate', () => { if (state.locked) return; closeMenu(); parseRoute(); state.page = 1; state.selected.clear(); updateHeader(); loadPage(); });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
window.addEventListener('online', healthCheck);
bindMenus();
parseRoute(); renderShell(); updateHeader(); syncSidebarA11y(); if (!state.locked) loadPage();
matchMedia('(max-width: 768px)').addEventListener('change', syncSidebarA11y);
matchMedia('(max-width: 1050px)').addEventListener('change', syncSidebarA11y);
healthCheck(); refreshSidebar();
const stopRealtime = realtime(onRealtime, connected => { state.wsConnected = connected; updateConnection(); });
const healthInterval = setInterval(() => { if (!document.hidden) { healthCheck(); refreshSidebar(); } }, 30000);
window.addEventListener('pagehide', () => { releaseHomeMotion(); stopRealtime(); clearInterval(healthInterval); });
