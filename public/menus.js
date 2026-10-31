import { esc, icon } from './ui.js';

let selectId = 0;
export function selectMenu(options, current, { label, name = '', prefix = '', attrs = '' } = {}) {
  const values = options.map(([value, text]) => [String(value), String(text)]);
  const selected = values.find(([value]) => value === String(current)) || values[0];
  const id = `select-menu-${++selectId}`;
  return `<span class="dropdown-select"><input type="hidden" id="${id}" ${name ? `name="${esc(name)}"` : ''} value="${esc(selected[0])}" data-menu-options="${esc(JSON.stringify(values))}" ${attrs}><button type="button" class="dropdown-trigger" data-select-menu="${id}" data-menu-label="${esc(label)}" aria-label="${esc(label)}: ${esc(selected[1])}" title="${esc(label)}: ${esc(selected[1])}" aria-haspopup="menu" aria-expanded="false">${prefix ? `<span class="dropdown-label">${esc(prefix)}</span>` : ''}<span class="dropdown-value">${esc(selected[1])}</span>${icon('down')}</button></span>`;
}

function openSelectMenu(trigger) {
  const input = document.getElementById(trigger.dataset.selectMenu);
  if (!input) return;
  const options = JSON.parse(input.dataset.menuOptions);
  const html = options.map(([value, label], index) => `<button type="button" role="menuitemradio" aria-checked="${value === input.value}" tabindex="-1" data-menu-option="${index}"><span>${esc(label)}</span>${value === input.value ? icon('check') : ''}</button>`).join('');
  openMenu(trigger, html, {
    label: trigger.dataset.menuLabel,
    minWidth: trigger.getBoundingClientRect().width,
    onChoose(index) {
      if (!input.isConnected || !options[index]) return;
      const [value, label] = options[index];
      input.value = value;
      trigger.querySelector('.dropdown-value').textContent = label;
      trigger.setAttribute('aria-label', `${trigger.dataset.menuLabel}: ${label}`);
      trigger.title = `${trigger.dataset.menuLabel}: ${label}`;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    },
  });
}

let popup, popupTrigger, choiceHandler, eventsBound = false, typed = '', typedAt = 0;
function focusMenuItem(item) {
  if (!item || !popup) return;
  item.focus({ preventScroll: true });
  const top = item.offsetTop, bottom = top + item.offsetHeight;
  if (top < popup.scrollTop + 8) popup.scrollTop = Math.max(0, top - 8);
  else if (bottom > popup.scrollTop + popup.clientHeight - 8) popup.scrollTop = bottom - popup.clientHeight + 8;
}
export function closeMenu({ focus = false } = {}) {
  popup?.remove();
  popup = null;
  choiceHandler = null;
  typed = '';
  popupTrigger?.setAttribute('aria-expanded', 'false');
  popupTrigger?.removeAttribute('aria-controls');
  if (focus && popupTrigger?.isConnected) popupTrigger.focus({ preventScroll: true });
  popupTrigger = null;
}

export function openMenu(trigger, html, { wide = false, label = 'Options', minWidth = 0, onChoose = null } = {}) {
  if (popupTrigger === trigger) return closeMenu({ focus: true });
  closeMenu();
  popupTrigger = trigger;
  choiceHandler = onChoose;
  popup = document.createElement('div');
  popup.id = 'menu-popup';
  popup.className = `menu-popover ${wide ? 'file-period-popover' : ''}`;
  popup.classList.toggle('in-modal', Boolean(trigger.closest('.modal')));
  if (minWidth) popup.style.minWidth = `${Math.min(innerWidth - 24, Math.max(154, minWidth))}px`;
  popup.setAttribute('role', wide ? 'dialog' : 'menu');
  popup.setAttribute('aria-label', label);
  popup.innerHTML = html;
  document.body.append(popup);
  trigger.setAttribute('aria-expanded', 'true');
  trigger.setAttribute('aria-controls', popup.id);
  const rect = trigger.getBoundingClientRect(), width = popup.offsetWidth, height = popup.offsetHeight;
  popup.style.left = `${Math.max(12, Math.min(rect.right - width, innerWidth - width - 12))}px`;
  popup.style.top = `${Math.max(12, Math.min(rect.bottom + 8, innerHeight - height - 12))}px`;
  focusMenuItem(popup.querySelector('[aria-checked="true"]') || popup.querySelector('button,a,input'));
  bindMenus();
}

export function bindMenus() {
  if (!eventsBound) {
    eventsBound = true;
    document.addEventListener('pointerdown', event => { if (popup && !popup.contains(event.target) && !popupTrigger.contains(event.target)) closeMenu(); }, true);
    document.addEventListener('click', event => {
      const trigger = event.target.closest('[data-select-menu]');
      if (trigger) { event.preventDefault(); openSelectMenu(trigger); return; }
      const option = event.target.closest('[data-menu-option]');
      if (option && popup?.contains(option)) {
        const choose = choiceHandler, index = Number(option.dataset.menuOption);
        closeMenu({ focus: true });
        choose?.(index);
      } else if (event.target.closest('[data-file-download]')) closeMenu({ focus: true });
    });
    document.addEventListener('focusin', event => { if (popup && !popup.contains(event.target) && !popupTrigger.contains(event.target)) closeMenu(); });
    window.addEventListener('resize', () => closeMenu());
    window.addEventListener('scroll', event => { if (popup && !popup.contains(event.target)) closeMenu(); }, true);
    document.addEventListener('keydown', event => {
      const trigger = event.target.closest('[aria-haspopup="menu"],[aria-haspopup="dialog"]');
      if (!popup && trigger && !trigger.disabled && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        trigger.click();
        if (event.key === 'ArrowUp' && popup?.getAttribute('role') === 'menu') focusMenuItem([...popup.querySelectorAll('[role="menuitem"],[role="menuitemradio"]')].at(-1));
        return;
      }
      if (!popup) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeMenu({ focus: true }); return; }
      if (popup.getAttribute('role') !== 'menu') return;
      const items = [...popup.querySelectorAll('[role="menuitem"],[role="menuitemradio"]')];
      if (event.key === 'Tab') { closeMenu({ focus: true }); return; }
      if (event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) {
        const now = Date.now();
        typed = now - typedAt < 600 ? typed + event.key.toLowerCase() : event.key.toLowerCase();
        typedAt = now;
        const search = [...new Set(typed)].length === 1 ? typed[0] : typed;
        const index = items.indexOf(document.activeElement);
        const ordered = search.length === 1 ? [...items.slice(index + 1), ...items.slice(0, index + 1)] : items;
        const match = ordered.find(item => item.textContent.trim().toLowerCase().startsWith(search));
        if (match) { event.preventDefault(); focusMenuItem(match); }
        return;
      }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      typed = '';
      const index = items.indexOf(document.activeElement);
      focusMenuItem(items[event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]);
    }, true);
  }
}
