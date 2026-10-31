// Size the dialog around the picture, leaving room for its title and controls.
export function fitVideoModal(video, preview) {
  const dialog = video?.closest('.video-viewer');
  if (!dialog) return;
  const width = video.videoWidth || preview?.naturalWidth;
  const height = video.videoHeight || preview?.naturalHeight;
  if (width > 0 && height > 0) dialog.style.setProperty('--video-ratio', width / height);

  const content = dialog.querySelector('.modal-content');
  const viewer = video.parentElement;
  const pixels = (style, ...properties) => properties.reduce((sum, name) => sum + (parseFloat(style[name]) || 0), 0);
  const frame = getComputedStyle(dialog), padding = getComputedStyle(content);
  const border = getComputedStyle(viewer), backdrop = getComputedStyle(dialog.parentElement);
  const inset = pixels(padding, 'paddingLeft', 'paddingRight') + pixels(frame, 'borderLeftWidth', 'borderRightWidth') + pixels(border, 'borderLeftWidth', 'borderRightWidth');
  dialog.style.setProperty('--video-inset', `${inset}px`);
  // A second measurement accounts for controls wrapping in narrow viewports.
  for (let pass = 0; pass < 2; pass++) {
    const chrome = dialog.querySelector('header').offsetHeight + content.offsetHeight - viewer.offsetHeight
      + pixels(frame, 'borderTopWidth', 'borderBottomWidth') + pixels(border, 'borderTopWidth', 'borderBottomWidth')
      + pixels(backdrop, 'paddingTop', 'paddingBottom');
    dialog.style.setProperty('--video-chrome', `${chrome}px`);
  }
}

export function bindMediaViewer() {
  document.addEventListener('loadedmetadata', event => {
    if (event.target instanceof HTMLVideoElement) fitVideoModal(event.target);
  }, true);
  window.addEventListener('resize', () => fitVideoModal(document.querySelector('.video-viewer video')));
}
