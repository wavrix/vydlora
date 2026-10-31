// Refresh data without replacing live controls, thumbnails, or scrolling containers.
function key(node) {
  if (node.nodeType !== 1) return null;
  for (const attr of ['id', 'data-key', 'data-file-id', 'data-job-id', 'data-action', 'data-select', 'name', 'href']) {
    if (node.hasAttribute(attr)) return `${node.nodeName}:${attr}:${node.getAttribute(attr)}`;
  }
  return null;
}

function compatible(current, next) {
  return current.nodeType === next.nodeType && current.nodeName === next.nodeName;
}

function update(current, next) {
  if (current.nodeType !== 1) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return;
  }
  const focused = current === current.ownerDocument.activeElement;
  const busy = current.hasAttribute('data-busy');
  if (current.nodeName === 'IMG' && current.classList.contains('image-failed') && current.getAttribute('src') === next.getAttribute('src')) next.classList.add('image-failed');
  for (const attr of [...current.attributes]) {
    if (busy && ['data-busy', 'aria-busy', 'disabled'].includes(attr.name)) continue;
    if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
  }
  for (const attr of next.attributes) {
    if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
  }
  if (current.nodeName === 'INPUT') {
    if (!focused && current.type !== 'file' && current.value !== next.value) current.value = next.value;
    if (current.checked !== next.checked) current.checked = next.checked;
  } else if (current.nodeName === 'TEXTAREA') {
    if (!focused && current.value !== next.value) current.value = next.value;
    return;
  }
  reconcile(current, next);
}

function reconcile(parent, nextParent) {
  const previous = [...parent.childNodes];
  const keyed = new Map(previous.map(node => [key(node), node]).filter(([value]) => value !== null));
  const used = new Set();
  let cursor = parent.firstChild;
  for (const next of nextParent.childNodes) {
    const nextKey = key(next);
    let current = nextKey === null ? null : keyed.get(nextKey);
    if (current && !compatible(current, next)) current = null;
    if (!current && nextKey === null) {
      current = previous.find(node => !used.has(node) && key(node) === null && compatible(node, next));
    }
    if (current && !used.has(current)) {
      if (current !== cursor) parent.insertBefore(current, cursor);
      update(current, next);
    } else {
      current = next.cloneNode(true);
      parent.insertBefore(current, cursor);
    }
    used.add(current);
    cursor = current.nextSibling;
  }
  for (const node of previous) if (!used.has(node)) node.remove();
}

export function patchContent(element, html) {
  if (!element) return;
  const active = element.ownerDocument.activeElement;
  const caret = active && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd, active.selectionDirection] : null;
  const template = element.ownerDocument.createElement('template');
  template.innerHTML = html;
  reconcile(element, template.content);
  if (active?.isConnected && active !== element.ownerDocument.activeElement) {
    active.focus({ preventScroll: true });
    if (caret) active.setSelectionRange(...caret);
  }
}
