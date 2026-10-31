// Keep the panel highlight aligned with the selected tab, including while it expands.
let homeObserver, homeFrame;
export function releaseHomeMotion() {
  homeObserver?.disconnect();
  cancelAnimationFrame(homeFrame);
  homeObserver = null;
}

export function mountHomeMotion(workspace) {
  releaseHomeMotion();
  if (!workspace) return;
  const panel = workspace.querySelector('.download-workspace');
  const edge = workspace.querySelector('.workspace-edge');
  const stage = workspace.closest('.home-stage');
  const requests = workspace.querySelector('.home-requests');
  const align = () => {
    if (!workspace.isConnected) return;
    // Status rows grow below the centered controls rather than recentering them.
    const requestHeight = requests.childElementCount ? requests.getBoundingClientRect().height + parseFloat(getComputedStyle(requests).marginTop) : 0;
    const height = `${requestHeight}px`;
    if (stage.style.getPropertyValue('--requests-height') !== height) stage.style.setProperty('--requests-height', height);
    const selected = workspace.querySelector('.platform-pill.selected').getBoundingClientRect();
    const bounds = panel.getBoundingClientRect();
    edge.style.setProperty('--edge-left', `${selected.left - bounds.left}px`);
    edge.style.setProperty('--edge-width', `${selected.width}px`);
  };
  const schedule = () => {
    if (!workspace.isConnected) return;
    cancelAnimationFrame(homeFrame);
    homeFrame = requestAnimationFrame(align);
  };
  // ResizeObserver runs before paint, so panel and highlight move together.
  homeObserver = new ResizeObserver(align);
  homeObserver.observe(workspace);
  workspace.querySelectorAll('.platform-pill').forEach(tab => homeObserver.observe(tab));
  align();
  document.fonts.ready.then(schedule);
}

export function selectPlatform(tabs, platform) {
  if (!tabs) return;
  tabs.querySelectorAll('.platform-pill').forEach(tab => {
    const selected = tab.dataset.platform === (platform || 'all');
    tab.classList.toggle('selected', selected);
    tab.setAttribute('aria-pressed', String(selected));
  });
}

// The reference rolls the previous heading upward and settles the new one in 380 ms.
const reels = new WeakMap();
export function rollText(element, value) {
  const previous = element.dataset.value || element.textContent;
  if (previous === value) return;
  const fontSize = parseFloat(getComputedStyle(element).fontSize);
  const previousWidth = element.getBoundingClientRect().width / fontSize;
  clearTimeout(reels.get(element));
  element.dataset.value = value;
  element.replaceChildren();
  const track = document.createElement('span');
  track.className = 'reel-track';
  for (const text of [previous, value, value]) {
    const slot = document.createElement('span');
    slot.className = 'reel-slot';
    slot.textContent = text;
    track.append(slot);
  }
  element.append(track);
  // Retain the widest title without fixing its pixel width at mobile font sizes.
  element.style.minWidth = `${Math.max(previousWidth, element.getBoundingClientRect().width / fontSize)}em`;
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) track.classList.add('rolling');
  reels.set(element, setTimeout(() => { element.textContent = value; }, 380));
}
