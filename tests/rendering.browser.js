// Native-DOM regression checks. Run in the disposable browser preview; no DOM package required.
export function renderingChecks(document, patchContent) {
  const results = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const host = document.createElement('div'); document.body.append(host);
  const run = (name, check) => {
    try { host.replaceChildren(); check(); results.push({ name, passed: true }); }
    catch (error) { results.push({ name, passed: false, error: error.message }); }
  };
  run('unchanged data causes no DOM mutations', () => {
    const html = '<div><button data-action="test">Refresh</button><strong>42</strong></div>';
    patchContent(host, html);
    const observer = new MutationObserver(() => {}); observer.observe(host, { subtree: true, childList: true, attributes: true, characterData: true });
    patchContent(host, html);
    assert(observer.takeRecords().length === 0, 'Unchanged elements were rewritten'); observer.disconnect();
  });
  run('editing and caret survive stale data', () => {
    patchContent(host, '<input name="search" value="old">');
    const input = host.firstElementChild; input.focus(); input.value = 'new draft'; input.setSelectionRange(3, 6);
    patchContent(host, '<input name="search" value="old"><span>Updated results</span>');
    assert(host.firstElementChild === input && input.value === 'new draft' && input.selectionStart === 3 && input.selectionEnd === 6 && document.activeElement === input, 'Draft or caret was reset');
  });
  run('keyed rows and thumbnails survive reordering', () => {
    const row = id => `<article data-file-id="${id}"><img src="/assets/mark.svg"><button data-action="media:${id}">Open</button></article>`;
    patchContent(host, row(1) + row(2));
    const first = host.children[0], second = host.children[1], image = first.querySelector('img'), button = first.querySelector('button'); button.focus();
    patchContent(host, row(2) + row(3) + row(1));
    assert(host.children[0] === second && host.children[2] === first && first.querySelector('img') === image && document.activeElement === button, 'Stable media or focus was replaced');
    patchContent(host, row(1)); assert(host.children.length === 1 && host.firstElementChild === first, 'Stale rows were not removed');
  });
  run('progress changes preserve rows and controls', () => {
    patchContent(host, '<div data-job-id="1"><button data-action="pause">Pause</button><div style="width:10%"></div></div>');
    const row = host.firstElementChild, button = row.firstElementChild;
    patchContent(host, '<div data-job-id="1"><button data-action="pause">Pause</button><div style="width:80%"></div></div>');
    assert(host.firstElementChild === row && row.firstElementChild === button && row.lastElementChild.style.width === '80%', 'Progress rebuilt the row');
  });
  run('checkbox state follows the rendered selection', () => {
    patchContent(host, '<input type="checkbox" data-select="1">'); const input = host.firstElementChild;
    patchContent(host, '<input type="checkbox" data-select="1" checked>'); assert(input.checked, 'Selection did not update');
    patchContent(host, '<input type="checkbox" data-select="1">'); assert(!input.checked && host.firstElementChild === input, 'Selection did not clear');
  });
  run('failed thumbnails keep their fallback until the source changes', () => {
    patchContent(host, '<img src="/assets/mark.svg" class="image-failed">'); const image = host.firstElementChild;
    patchContent(host, '<img src="/assets/mark.svg">'); assert(image.classList.contains('image-failed'), 'Fallback flashed off');
    patchContent(host, '<img src="/assets/other.svg">'); assert(!image.classList.contains('image-failed'), 'New source retained the failed state');
  });
  run('busy controls remain protected during refresh', () => {
    patchContent(host, '<button data-action="add" data-busy="true" aria-busy="true" disabled>Add</button>'); const button = host.firstElementChild;
    patchContent(host, '<button data-action="add">Add</button>');
    assert(button.disabled && button.dataset.busy === 'true' && button.getAttribute('aria-busy') === 'true', 'Pending action was enabled early');
  });
  run('SVG updates retain the correct namespace', () => {
    patchContent(host, '<svg viewBox="0 0 10 10"><path d="M0 0"></path></svg>'); const svg = host.firstElementChild;
    patchContent(host, '<svg viewBox="0 0 10 10"><path d="M1 1"></path><circle r="2"></circle></svg>');
    assert(host.firstElementChild === svg && svg.lastElementChild.namespaceURI === 'http://www.w3.org/2000/svg' && svg.firstElementChild.getAttribute('d') === 'M1 1', 'SVG reconciliation failed');
  });
  host.remove(); return results;
}
