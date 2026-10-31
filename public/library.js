import { esc, icon, iconButton, button, count, bytes, time, relative } from './ui.js';

export const librarySorts = [
  ['created_at:desc', 'Recently added', 'Recent'], ['created_at:asc', 'Oldest added', 'Oldest'],
  ['file_size:desc', 'Largest first', 'Largest'],
];
export const librarySources = [
  ['manual', 'Manual'], ['monitor', 'Monitoring'], ['story', 'Stories'], ['telegram', 'Telegram'],
];
const periods = [['', 'All'], ['today', 'Today'], ['7', 'Last 7 days'], ['30', 'Last 30 days']];
export function librarySourceGroup(value) {
  if (!value) return '';
  if (value === 'catchup') return 'monitor';
  return ['monitor', 'story', 'telegram'].includes(value) ? value : 'manual';
}
const sourceLabel = value => librarySources.find(([id]) => id === librarySourceGroup(value))?.[1] || 'All';
const periodLabel = value => periods.find(([id]) => id === value)?.[1] || 'Custom';

export function libraryTabs(status) {
  return `<nav class="files-type-tabs" aria-label="Media type">${[['', 'All files'], ['image', 'Images'], ['video', 'Videos']].map(([value, label]) => `<button type="button" data-action="library-type:${value}" aria-pressed="${status === value}" class="${status === value ? 'selected' : ''}">${label}</button>`).join('')}</nav>`;
}

export function libraryFilters(state) {
  return `<div class="files-filters">${[['sort', 'Sort by'], ['source', 'Source'], ['timeframe', 'Timeframe']].map(([id, label]) => `<button type="button" class="dropdown-trigger" data-action="library-menu:${id}" aria-haspopup="${id === 'timeframe' ? 'dialog' : 'menu'}" aria-expanded="false" aria-label="${esc(label)}: ${esc(filterValue(id, state))}"><span class="dropdown-label">${label}</span><span class="dropdown-value" data-filter-value="${id}">${esc(filterValue(id, state))}</span>${icon('down')}</button>`).join('')}<button type="button" class="button primary files-new-download" data-action="go-home" aria-label="New download">${icon('plus')}<span class="files-download-desktop">New download</span><span class="files-download-mobile">Download</span></button></div>`;
}

function filterValue(id, state) {
  if (id === 'sort') return librarySorts.find(([value]) => value === state.sort)?.[2] || 'Recent';
  return id === 'source' ? sourceLabel(state.librarySource) : periodLabel(state.libraryPeriod);
}

export function syncLibraryHeader(state) {
  document.querySelectorAll('.files-type-tabs button').forEach(tab => {
    const selected = tab.dataset.action === `library-type:${state.status}`;
    tab.classList.toggle('selected', selected);
    tab.setAttribute('aria-pressed', String(selected));
  });
  document.querySelectorAll('[data-filter-value]').forEach(label => {
    label.textContent = filterValue(label.dataset.filterValue, state);
    const prefix = label.parentElement.querySelector('.dropdown-label').textContent;
    label.parentElement.title = `${prefix} ${label.textContent}`;
    label.parentElement.setAttribute('aria-label', `${prefix}: ${label.textContent}`);
  });
}

const radioItem = (label, action, selected) => `<button type="button" role="menuitemradio" aria-checked="${selected}" tabindex="-1" data-action="${esc(action)}"><span>${esc(label)}</span>${selected ? icon('check') : ''}</button>`;

export function libraryFilterMenu(kind, state) {
  if (kind === 'sort') return librarySorts.map(([value, label]) => radioItem(label, `library-sort:${value}`, state.sort === value)).join('');
  if (kind === 'source') return librarySources.map(([value, label]) => radioItem(label, `library-source:${value}`, state.librarySource === value)).join('');
  return `<div class="file-period-options">${periods.map(([value, label]) => `<button type="button" class="${state.libraryPeriod === value ? 'selected' : ''}" data-action="library-period:${value}">${icon('calendar')}<span>${label}</span>${state.libraryPeriod === value ? icon('check') : ''}</button>`).join('')}</div><form class="file-date-range" data-form="library-dates"><h3>Custom range</h3><p>Show files added between these dates.</p><label class="field"><span>From</span><input type="date" name="from" aria-label="Start date" value="${esc(state.libraryFrom)}"></label><label class="field"><span>To</span><input type="date" name="to" aria-label="End date" value="${esc(state.libraryTo)}"></label><div class="file-date-actions">${button('Clear', 'library-period:')}<button type="submit" class="button primary">Apply</button></div></form>`;
}

export function libraryDateRange(state, now = new Date()) {
  if (state.libraryPeriod === 'custom') return { date_from: state.libraryFrom || undefined, date_to: state.libraryTo || undefined };
  if (!state.libraryPeriod) return {};
  const end = now.toISOString().slice(0, 10);
  const start = new Date(`${end}T00:00:00Z`);
  start.setUTCDate(start.getUTCDate() - (state.libraryPeriod === 'today' ? 0 : Number(state.libraryPeriod) - 1));
  return { date_from: start.toISOString().slice(0, 10), date_to: end };
}

export function libraryContents(data, state) {
  const filtered = Boolean(state.search || state.status || state.librarySource || state.libraryPeriod);
  const search = state.librarySearchOpen || state.search;
  return `<section class="files-page" aria-label="Downloaded files"><header class="files-section-header"><h2>All files <span id="files-total">${count(data.total)}</span></h2><div class="files-utilities">
    ${search ? `<label class="files-search">${icon('search')}<input type="search" data-list-search aria-label="Search your downloads" placeholder="Search files…" value="${esc(state.search)}">${iconButton('Close file search', 'library-search-close', 'close')}</label>` : iconButton('Search files', 'library-search', 'search')}
    <div class="files-view-toggle" role="group" aria-label="File layout">${iconButton('Grid view', 'view:grid', 'grid', `aria-pressed="${state.view === 'grid'}"`)}${iconButton('List view', 'view:list', 'queue', `aria-pressed="${state.view === 'list'}"`)}</div>
    ${iconButton('Refresh files', 'refresh', 'retry')}${iconButton('More file options', 'library-options', 'dots')}
  </div></header>
  ${filtered ? `<div class="files-filter-summary"><span>${state.status ? `${state.status === 'image' ? 'Images' : 'Videos'} · ` : ''}${state.librarySource ? `${sourceLabel(state.librarySource)} · ` : ''}${state.libraryPeriod ? `${periodLabel(state.libraryPeriod)} · ` : ''}${state.search ? `“${esc(state.search)}” · ` : ''}${count(data.total)} ${data.total === 1 ? 'file' : 'files'}</span>${button('Clear filters', 'library-clear', { kind: 'subtle', icon: 'close' })}</div>` : ''}
  ${librarySelection(state)}
  ${data.items.length ? state.view === 'list' ? libraryList(data.items, state.selected) : `<div class="files-grid">${data.items.map(row => libraryCard(row, state.selected)).join('')}</div>` : `<div class="files-empty">${icon('folder-open')}<p>${filtered ? 'No matching files' : 'No files'}</p></div>`}
  ${data.pages > 1 ? `<div class="pagination"><span>${count(data.total)} files · Page ${data.page} of ${data.pages}</span><div>${button('Previous', 'previous-page', { disabled: data.page <= 1 })}${button('Next', 'next-page', { disabled: data.page >= data.pages })}</div></div>` : ''}
  </section>`;
}

function librarySelection(state) {
  return `<div class="selection-bar files-selection ${state.selected.size ? 'visible' : ''}" id="selection-bar"><label class="files-select-page"><input type="checkbox" data-select-all aria-label="Select all files on this page"><span>Select all</span></label><strong id="selected-count">${state.selected.size} selected</strong><div class="button-row">${button('Delete', 'bulk-delete', { icon: 'trash', kind: 'danger subtle' })}${button('Deselect', 'deselect', { icon: 'close', kind: 'subtle' })}</div></div>`;
}

function fileDuration(row) {
  return row.duration ? `${Math.floor(row.duration / 60)}:${String(Math.floor(row.duration % 60)).padStart(2, '0')}` : row.post_type === 'image' ? 'Images' : 'Video';
}

function libraryCard(row, selected) {
  const title = row.title || row.video_id || 'Untitled media';
  return `<article class="file-card ${selected.has(row.id) ? 'selected' : ''}" data-file-id="${row.id}"><div class="file-thumb">
    <button class="file-open" data-action="media:${row.id}" aria-label="Open ${esc(title)}"><span class="file-placeholder">${icon(row.post_type === 'image' ? 'image' : 'video')}</span><img src="/api/downloads/${row.id}/thumbnail" alt="" loading="lazy" data-fallback="media"></button>
    <label class="file-checkbox"><input type="checkbox" data-select="${row.id}" aria-label="Select ${esc(title)}" ${selected.has(row.id) ? 'checked' : ''}></label>
    <span class="file-type">${icon(row.post_type === 'image' ? 'image' : 'play')}${fileDuration(row)}</span>${row.remote_path ? `<span class="file-cloud" title="Stored in MEGA">${icon('cloud')}</span>` : ''}
    <div class="file-hover-actions"><a href="/api/downloads/${row.id}/download" class="icon-button" aria-label="Save ${esc(title)}" title="Save file">${icon('download')}</a>${iconButton(`Open ${title}`, `media:${row.id}`, 'arrow')}</div>
  </div><div class="file-caption"><div class="file-title-row"><button class="file-name" title="${esc(title)}" data-action="media:${row.id}">${esc(title)}</button>${iconButton(`More actions for ${title}`, `file-menu:${row.id}`, 'dots')}</div><p>${esc(row.username ? '@' + row.username : sourceLabel(row.source))} · ${bytes(row.file_size)}</p><p title="${esc(time(row.created_at))}">${relative(row.created_at)}</p></div></article>`;
}

function libraryList(rows, selected) {
  return `<div class="files-table-scroll"><table class="files-table"><thead><tr><th class="check-cell"><input type="checkbox" data-select-all aria-label="Select all files on this page"></th><th>Name</th><th>Creator</th><th>Size</th><th>Added</th><th></th></tr></thead><tbody>${rows.map(row => `<tr data-file-id="${row.id}" class="${selected.has(row.id) ? 'selected' : ''}"><td><input type="checkbox" data-select="${row.id}" aria-label="Select ${esc(row.title || row.video_id)}" ${selected.has(row.id) ? 'checked' : ''}></td><td><button class="file-list-name" data-action="media:${row.id}"><img src="/api/downloads/${row.id}/thumbnail" alt="" loading="lazy" data-fallback="media"><span>${esc(row.title || row.video_id || 'Untitled media')}</span></button></td><td>${esc(row.username ? '@' + row.username : sourceLabel(row.source))}</td><td>${bytes(row.file_size)}</td><td title="${esc(time(row.created_at))}">${relative(row.created_at)}</td><td>${iconButton(`More actions for ${row.title || row.video_id}`, `file-menu:${row.id}`, 'dots')}</td></tr>`).join('')}</tbody></table></div>`;
}

export function fileActionMenu(row) {
  return `<button type="button" role="menuitem" tabindex="-1" data-action="media:${row.id}">${icon('arrow')}<span>Open file</span></button><a role="menuitem" tabindex="-1" href="/api/downloads/${row.id}/download" data-file-download>${icon('download')}<span>Save file</span></a><div class="file-menu-divider"></div><button type="button" role="menuitem" tabindex="-1" class="danger" data-action="media-delete:${row.id}">${icon('trash')}<span>Delete</span></button>`;
}
