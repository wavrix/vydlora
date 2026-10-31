// Exercise the production modal lifecycle against the browser's native DOM.
export async function modalChecks(document, { modal, updateModal, closeModal, isModalActive }) {
  const results = [], overlays = document.createElement('div'), opener = document.createElement('button');
  overlays.id = 'overlays'; opener.textContent = 'Open test dialog'; document.body.append(opener, overlays);
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const frame = () => new Promise(requestAnimationFrame);
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const run = async (name, check) => {
    try { await check(); results.push({ name, passed: true }); }
    catch (error) { results.push({ name, passed: false, error: error.message }); }
    finally { closeModal(); await frame(); }
  };
  await run('modal updates preserve the shell, draft, focus, and caret', async () => {
    const html = '<input name="draft" value="saved"><p>First result</p>';
    const handle = modal('Editor', html); await frame();
    const backdrop = overlays.firstElementChild, dialog = overlays.querySelector('.modal'), input = dialog.querySelector('input');
    input.focus(); input.value = 'unsaved draft'; input.setSelectionRange(2, 7);
    updateModal(handle, 'Updated editor', html.replace('First result', 'New result'));
    assert(overlays.firstElementChild === backdrop && overlays.querySelector('.modal') === dialog, 'The dialog shell was recreated');
    assert(dialog.querySelector('input') === input && input.value === 'unsaved draft' && document.activeElement === input && input.selectionStart === 2 && input.selectionEnd === 7, 'Editing state was lost');
  });
  await run('result updates do not restart modal or backdrop animations', async () => {
    const handle = modal('Loading', '<p>Loading</p>'); await frame();
    const dialog = overlays.querySelector('.modal'), backdrop = overlays.firstElementChild;
    const entrance = dialog.getAnimations()[0], fade = backdrop.getAnimations()[0];
    updateModal(handle, 'Ready', '<p>Ready</p>');
    if (entrance) assert(dialog.getAnimations()[0] === entrance, 'Modal animation restarted');
    if (fade) assert(backdrop.getAnimations()[0] === fade, 'Backdrop animation restarted');
  });
  await run('closed dialogs reject late responses', () => {
    const handle = modal('Loading', '<p>Waiting</p>'); closeModal();
    assert(!updateModal(handle, 'Late result', '<p>Late</p>') && !overlays.firstElementChild && !isModalActive(handle), 'Closed dialog reopened');
  });
  await run('superseded dialogs cannot overwrite the active dialog', () => {
    const first = modal('First', '<input name="draft" value="first">'), backdrop = overlays.firstElementChild;
    const second = modal('Second', '<input name="draft" value="second">');
    assert(!updateModal(first, 'Stale result', '<p>Stale</p>') && isModalActive(second) && document.querySelector('#modal-title').textContent === 'Second', 'Stale response replaced the new dialog');
    assert(overlays.firstElementChild === backdrop && overlays.querySelector('input').value === 'second', 'Dialog transition replaced the backdrop or leaked a draft');
  });
  await run('fast results bypass the loading placeholder', async () => {
    const handle = modal('Loading', '<p>Placeholder</p>', { delay: 60 });
    assert(!overlays.firstElementChild, 'Placeholder appeared immediately');
    updateModal(handle, 'Ready', '<p>Final result</p>'); const dialog = overlays.querySelector('.modal');
    await pause(80);
    assert(overlays.querySelector('.modal') === dialog && !dialog.textContent.includes('Placeholder') && dialog.textContent.includes('Final result'), 'Delayed placeholder replaced the result');
  });
  await run('closing a pending modal cancels its delayed opening', async () => {
    const handle = modal('Loading', '<p>Placeholder</p>', { delay: 60 }); closeModal(); await pause(80);
    assert(!overlays.firstElementChild && !isModalActive(handle) && !document.body.classList.contains('modal-open'), 'Pending dialog reopened or left scrolling locked');
  });
  await run('closing a modal transition restores the original opener', async () => {
    opener.focus(); modal('First', '<button>Continue</button>'); await frame();
    overlays.querySelector('.modal-content button').focus(); modal('Second', '<button>Done</button>'); await frame(); closeModal();
    assert(document.activeElement === opener, 'Focus did not return to the original opener');
    closeModal(); assert(document.activeElement === opener, 'Closing an absent modal stole focus');
  });
  await run('updates preserve scrolling, expanded sections, and media elements', async () => {
    const html = '<video controls></video><details><summary>More</summary><p>Details</p></details><div style="height:1600px">Content</div>';
    const handle = modal('Details', html); await frame();
    const dialog = overlays.querySelector('.modal'), video = dialog.querySelector('video'), details = dialog.querySelector('details');
    details.open = true; dialog.scrollTop = 100;
    updateModal(handle, 'Details', html.replace('Content', 'Updated content'));
    assert(dialog.scrollTop === 100 && details.open && dialog.querySelector('video') === video, 'Scroll, expanded sections, or the media player was reset');
  });
  overlays.remove(); opener.remove(); return results;
}
