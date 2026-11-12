/* Keep the assistant within the viewport and remember its position. */
(() => {
  const launcher = document.getElementById('aiRobotLauncher');
  const panel = document.getElementById('aiDrawerOverlay');
  if (!launcher || !panel) return;

  const storageKey = 'fileFinderRobotPosition';
  const margin = 12;
  let position = null;
  let gesture = null;
  let suppressClickUntil = 0;
  const clamp = (value, low, high) => Math.min(Math.max(low, high), Math.max(low, value));

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      position = {x: clamp(saved.x, 0, 1), y: clamp(saved.y, 0, 1)};
    }
  } catch { /* A blocked storage area still allows dragging for this visit. */ }

  function bounds() {
    return {
      width: Math.max(0, innerWidth - launcher.offsetWidth - margin * 2),
      height: Math.max(0, innerHeight - launcher.offsetHeight - margin * 2)
    };
  }

  function positionPanel() {
    if (panel.classList.contains('hidden')) return;
    const rect = launcher.getBoundingClientRect();
    const width = Math.min(420, innerWidth - margin * 2);
    const height = Math.min(620, innerHeight - margin * 2);
    const above = rect.top - margin;
    const below = innerHeight - rect.bottom - margin;
    let left = clamp(rect.right - width, margin, innerWidth - width - margin);
    let top = above >= below ? rect.top - height - 12 : rect.bottom + 12;
    // Place a tall panel beside the launcher where vertical space is limited.
    if (Math.max(above, below) < height + 12) {
      if (rect.left >= width + margin + 12) left = rect.left - width - 12;
      else if (innerWidth - rect.right >= width + margin + 12) left = rect.right + 12;
    }
    top = clamp(top, margin, innerHeight - height - margin);
    Object.assign(panel.style, {left: `${left}px`, top: `${top}px`, right: 'auto', bottom: 'auto', width: `${width}px`, height: `${height}px`});
  }
  window.positionRobotPanel = positionPanel;

  function move(left, top) {
    const range = bounds();
    const x = clamp(left, margin, margin + range.width);
    const y = clamp(top, margin, margin + range.height);
    Object.assign(launcher.style, {left: `${x}px`, top: `${y}px`, right: 'auto', bottom: 'auto'});
    position = {x: range.width ? (x - margin) / range.width : 0, y: range.height ? (y - margin) / range.height : 0};
    positionPanel();
  }

  function restorePosition() {
    if (position) {
      const range = bounds();
      move(margin + position.x * range.width, margin + position.y * range.height);
    }
    positionPanel();
  }

  launcher.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0) return;
    const rect = launcher.getBoundingClientRect();
    gesture = {id: event.pointerId, x: event.clientX, y: event.clientY, left: rect.left, top: rect.top, moved: false};
    launcher.setPointerCapture(event.pointerId);
  });
  launcher.addEventListener('pointermove', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
    if (!gesture.moved && Math.hypot(dx, dy) < 6) return;
    gesture.moved = true;
    launcher.classList.add('is-dragging');
    event.preventDefault();
    move(gesture.left + dx, gesture.top + dy);
  });
  function finish(event) {
    if (!gesture || gesture.id !== event.pointerId) return;
    if (gesture.moved) {
      suppressClickUntil = performance.now() + 400;
      try { localStorage.setItem(storageKey, JSON.stringify(position)); } catch {}
    }
    gesture = null;
    launcher.classList.remove('is-dragging');
    if (launcher.hasPointerCapture(event.pointerId)) launcher.releasePointerCapture(event.pointerId);
  }
  launcher.addEventListener('pointerup', finish);
  launcher.addEventListener('pointercancel', finish);
  launcher.addEventListener('lostpointercapture', finish);
  launcher.addEventListener('click', event => {
    if (event.detail !== 0 && performance.now() < suppressClickUntil) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  window.addEventListener('resize', restorePosition);
  restorePosition();
})();
