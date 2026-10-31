export function menuChecks(document, menus, { libraryFilterMenu, librarySourceGroup }) {
  const results = [], host = document.createElement('div'); document.body.append(host);
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const key = (element, value) => element.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true }));
  const source = current => {
    host.innerHTML = '<button aria-haspopup="menu">Source</button>';
    const trigger = host.firstElementChild;
    trigger.addEventListener('click', () => menus.openMenu(trigger, libraryFilterMenu('source', { librarySource: current }), { label: 'Source options' }));
    trigger.focus({ preventScroll: true }); return trigger;
  };
  const run = (name, check) => {
    try { check(); results.push({ name, passed: true }); }
    catch (error) { results.push({ name, passed: false, error: error.message }); }
    finally { menus.closeMenu(); host.replaceChildren(); }
  };
  menus.bindMenus();
  run('Source offers exactly four categories and groups existing downloads', () => {
    const trigger = source('monitor'); trigger.click();
    const popup = document.querySelector('.menu-popover');
    assert(JSON.stringify([...popup.querySelectorAll('[role="menuitemradio"]')].map(item=>item.textContent.trim())) === JSON.stringify(['Manual','Monitoring','Stories','Telegram']), 'Unexpected source options');
    for (const value of ['bulk','import','instagram','custom']) assert(librarySourceGroup(value) === 'manual', `Manual source omitted: ${value}`);
    assert(librarySourceGroup('catchup') === 'monitor', 'Catch-up is not grouped under Monitoring');
    assert(popup.querySelectorAll('[aria-checked="true"]').length === 1 && popup.querySelector('[data-action="library-source:monitor"]').getAttribute('aria-checked') === 'true', 'Selected source is incorrect');
  });
  run('ArrowDown opens action-based Source menus at the selected option', () => {
    const trigger = source('monitor'); key(trigger, 'ArrowDown');
    assert(document.activeElement?.getAttribute('data-action') === 'library-source:monitor' && trigger.getAttribute('aria-expanded') === 'true', 'Source did not open with the keyboard');
  });
  run('ArrowUp opens a Source menu at its final option', () => {
    const trigger = source(''); key(trigger, 'ArrowUp');
    const items = [...document.querySelectorAll('.menu-popover [role="menuitemradio"]')];
    assert(document.activeElement === items.at(-1), 'ArrowUp did not reach the final option');
  });
  run('repeated letters cycle through matching source names', () => {
    const trigger = source('story'); key(trigger, 'ArrowDown'); key(document.activeElement, 'm');
    assert(document.activeElement?.getAttribute('data-action') === 'library-source:manual', 'Manual was not found');
    key(document.activeElement, 'm');
    assert(document.activeElement?.getAttribute('data-action') === 'library-source:monitor', 'Monitoring was not found on repeated M');
  });
  run('keyboard navigation scrolls the menu without moving the page', () => {
    const trigger = source(''); trigger.click(); const popup = document.querySelector('.menu-popover'); popup.style.maxHeight = '100px';
    const scroll = document.scrollingElement.scrollTop; key(document.activeElement, 'End');
    assert(popup.scrollTop > 0 && document.scrollingElement.scrollTop === scroll && document.querySelector('.menu-popover') === popup, `Menu navigation state: ${JSON.stringify({popupScroll:popup.scrollTop,pageBefore:scroll,pageAfter:document.scrollingElement.scrollTop,open:document.querySelector('.menu-popover')===popup,lastTop:popup.lastElementChild.offsetTop,height:popup.clientHeight,fullHeight:popup.scrollHeight})}`);
    key(document.activeElement, 'Escape'); assert(!document.querySelector('.menu-popover') && document.activeElement === trigger, 'Escape did not restore focus');
  });
  run('shared form dropdowns retain their value and focus after selection', () => {
    host.innerHTML = menus.selectMenu([['a', 'Alpha'], ['b', 'Beta']], 'a', { name: 'sample', label: 'Sample' });
    const trigger = host.querySelector('button'); key(trigger, 'ArrowDown');
    document.querySelector('[data-menu-option="1"]').click();
    assert(host.querySelector('input').value === 'b' && host.querySelector('.dropdown-value').textContent === 'Beta' && document.activeElement === trigger && !document.querySelector('.menu-popover'), 'The shared dropdown value or focus was lost');
  });
  host.remove(); return results;
}
