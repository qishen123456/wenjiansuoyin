/* Retained business pages and document readers share one ordered workspace.
 * app.js owns page initialization, business requests and permission checks.
 */
(() => {
  'use strict';
  const labels = { search: '文件搜索', catalog: '目录浏览', archive: '归档合规', approval: '下载审批', members: '成员管理', access: '文件权限', notifications: '推送任务', 'report-audit': '报表更新明细', sync: '索引与同步' };
  const groups = { search: 'files', catalog: 'files', archive: 'tasks', approval: 'tasks', members: 'manage', access: 'manage', notifications: 'manage', 'report-audit': 'tasks', sync: 'manage' };
  const tabs = new Map();
  const sources = document.getElementById('workspaceSources');
  const reader = document.getElementById('documentWorkspace');
  const empty = document.getElementById('workspaceEmpty');
  const tabBar = document.getElementById('workspaceTabs');
  const mobileSelect = document.getElementById('workspaceMobileSelect');
  const mobileClose = document.getElementById('workspaceMobileClose');
  let activeKey = null;
  let restoringHistory = false;
  let routeRestored = false;
  let sessionReady = false;
  let sessionVersion = 0;
  let scrollFrame = 0;
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

  const validMode = mode => Object.hasOwn(labels, mode);
  const canOpenPage = mode => validMode(mode) && window.canOpenWorkspacePage?.(mode) === true;
  const fileKey = id => `file:${id}`;
  const normalizeKey = key => typeof key === 'number' || /^\d+$/.test(key) ? fileKey(Number(key)) : validMode(key) ? `page:${key}` : key;

  function savePosition() {
    const tab = tabs.get(activeKey);
    if (tab) tab.scroll = window.scrollY;
  }

  function remember(replace = false, navigate = false) {
    if (restoringHistory) return;
    const state = { ...history.state, fileWorkspace: { activeKey } };
    const tab = tabs.get(activeKey);
    const url = navigate && tab?.kind === 'page' ? window.workspacePageUrl?.(tab.mode) : undefined;
    if (replace) history.replaceState(state, '', url);
    else history.pushState(state, '', url);
  }

  function closeMenus() {
    document.querySelectorAll('.workspace-nav-group').forEach(group => {
      group.classList.remove('is-open');
      group.querySelector('.workspace-nav-trigger').setAttribute('aria-expanded', 'false');
      group.querySelector('.workspace-nav-menu').hidden = true;
    });
  }

  function announce(message) {
    document.getElementById('workspaceAnnouncement').textContent = message;
  }

  function makeButton(text, className, action) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    const label = document.createElement('span');
    label.textContent = text;
    button.append(label);
    button.addEventListener('click', action);
    return button;
  }

  function revealActiveTab() {
    const tab = tabBar.querySelector('.active');
    if (!tab) return;
    const bounds = tabBar.getBoundingClientRect();
    const item = tab.getBoundingClientRect();
    if (!bounds.width) return; // Mobile uses the select; never scroll the document.
    if (item.left < bounds.left) tabBar.scrollLeft += item.left - bounds.left;
    else if (item.right > bounds.right) tabBar.scrollLeft += item.right - bounds.right;
  }

  function renderTabs() {
    const oldFocus = document.activeElement;
    const focusKey = oldFocus?.closest('[data-workspace-id]')?.dataset.workspaceId;
    const focusClose = oldFocus?.classList.contains('workspace-tab-close');
    tabBar.replaceChildren();
    mobileSelect.replaceChildren();
    tabs.forEach((tab, key) => {
      const element = document.createElement('div');
      element.className = `workspace-tab${activeKey === key ? ' active' : ''}`;
      element.dataset.workspaceId = key;
      element.dataset.workspaceKind = tab.kind;
      const openButton = makeButton(tab.name, 'workspace-tab-open', () => activate(key));
      openButton.title = tab.name;
      if (activeKey === key) openButton.setAttribute('aria-current', 'page');
      const closeButton = makeButton('×', 'workspace-tab-close', () => close(key));
      closeButton.setAttribute('aria-label', `关闭 ${tab.name}`);
      element.append(openButton, closeButton);
      tabBar.append(element);
      mobileSelect.add(new Option(tab.name, key, false, activeKey === key));
    });
    if (!tabs.size) mobileSelect.add(new Option('暂无打开的页面', '', true, true));
    mobileSelect.disabled = tabs.size === 0;
    mobileClose.disabled = activeKey === null;
    mobileClose.hidden = activeKey === null;
    if (focusKey) {
      const currentTab = Array.from(tabBar.children).find(tab => tab.dataset.workspaceId === focusKey) || tabBar.querySelector('.active');
      currentTab?.querySelector(focusClose ? '.workspace-tab-close' : '.workspace-tab-open')?.focus({ preventScroll: true });
    }
    revealActiveTab();
  }

  function render({ restoreScroll = true } = {}) {
    const current = tabs.get(activeKey);
    const reading = current?.kind === 'file';
    const mode = current?.kind === 'page' ? current.mode : null;
    sources.hidden = !mode;
    reader.hidden = !reading;
    if (empty) empty.hidden = Boolean(current);
    document.body.classList.toggle('is-reading', reading);
    document.body.classList.toggle('is-workspace-empty', !current);
    tabs.forEach((tab, key) => { if (tab.frame) tab.frame.hidden = key !== activeKey; });
    // This only changes visibility and navigation state; it must never fetch data.
    window.activateWorkspacePage?.(mode);
    document.querySelectorAll('.workspace-nav-group').forEach(group => {
      group.classList.toggle('active', group.dataset.group === (reading ? 'files' : groups[mode]));
    });
    renderTabs();
    if (restoreScroll) {
      cancelAnimationFrame(scrollFrame);
      scrollFrame = requestAnimationFrame(() => window.scrollTo({ top: tabs.get(activeKey)?.scroll || 0, behavior: 'instant' }));
    }
  }

  function preparePage(tab) {
    if (!tab.ready) {
      try { tab.ready = Promise.resolve(window.initializeWorkspacePage?.(tab.mode)); }
      catch (error) { tab.ready = Promise.reject(error); }
      const pending = tab.ready;
      // Retain pending/successful work; only a failed attempt can be retried.
      pending.catch(error => {
        if (tab.ready === pending) tab.ready = null;
        window.toast?.(error.message || '页面加载失败，请重新打开此标签重试');
      });
    }
    return tab.ready;
  }

  function activate(key, pushHistory = true) {
    key = normalizeKey(key);
    const tab = tabs.get(key);
    if (!tab || (tab.kind === 'page' && !canOpenPage(tab.mode))) return Promise.resolve(false);
    if (activeKey !== key) {
      savePosition();
      activeKey = key;
      closeMenus();
      render();
      if (pushHistory) remember(false, true);
      announce(`已打开 ${tab.name}`);
    }
    return tab.kind === 'page' ? preparePage(tab) : Promise.resolve(true);
  }

  function showSource(mode = 'search', pushHistory = true) {
    if (!canOpenPage(mode)) return Promise.resolve(false);
    const key = `page:${mode}`;
    let tab = tabs.get(key);
    if (!tab) {
      tab = { kind: 'page', mode, name: labels[mode], scroll: 0, ready: null };
      tabs.set(key, tab);
    }
    return activate(key, pushHistory);
  }

  function open(fileId, name, pushHistory = true) {
    const id = Number(fileId);
    if (!Number.isSafeInteger(id) || id < 1) return;
    const key = fileKey(id);
    if (!tabs.has(key)) {
      const frame = document.createElement('iframe');
      frame.className = 'workspace-frame';
      frame.title = name || `文件 ${id}`;
      frame.src = `/static/preview.html?id=${id}&embedded=1`;
      frame.hidden = true;
      frame.setAttribute('allowfullscreen', '');
      reader.append(frame);
      tabs.set(key, { kind: 'file', name: name || `文件 ${id}`, frame, scroll: 0 });
    }
    return activate(key, pushHistory);
  }

  function close(key) {
    key = normalizeKey(key);
    const tab = tabs.get(key);
    if (!tab) return;
    savePosition();
    const keys = [...tabs.keys()];
    const index = keys.indexOf(key);
    const wasActive = activeKey === key;
    tab.frame?.remove();
    tabs.delete(key);
    if (wasActive) activeKey = keys[index + 1] || keys[index - 1] || null;
    render({ restoreScroll: wasActive });
    remember(true, wasActive);
    announce(`已关闭 ${tab.name}`);
    if (wasActive) (tabBar.querySelector('.active .workspace-tab-open') || empty?.querySelector('button'))?.focus({ preventScroll: true });
  }

  function clear() {
    tabs.forEach(tab => tab.frame?.remove());
    tabs.clear();
    activeKey = null;
    routeRestored = false;
    sessionReady = false;
    sessionVersion += 1;
    window.resetFeishuWorkspaceSession?.();
    closeMenus();
    render();
    remember(true);
  }

  // Invoked only after successful authentication. Keep the URL intact for login/reload.
  async function restoreRoute() {
    if (routeRestored) return;
    routeRestored = true;
    sessionReady = true;
    const version = sessionVersion;
    const params = new URLSearchParams(location.search);
    const requested = params.get('view') || params.get('tab') || 'search';
    const target = requested === 'permission'
      ? (['audit', 'grants'].includes(params.get('section')) ? 'access' : 'members')
      : requested;
    const mode = canOpenPage(target) ? target : 'search';
    if (mode === 'report-audit') window.prepareDailyReportRoute?.(params);
    await showSource(mode, false);
    if (version !== sessionVersion) return;
    remember(true);
    const query = (params.get('q') || '').trim().slice(0, 300);
    if (mode === 'report-audit' && params.get('download') === 'abnormal') window.downloadDailyReportFromRoute?.();
    if (requested === 'updates') await window.openDailyUpdateDetails?.();
    else if (mode === 'search' && query) {
      document.getElementById('q').value = query;
      await window.search?.(1);
    }
    if (version !== sessionVersion) return;
    // Upload starts from the toolbar after the user chooses a folder.
    if (mode === 'search' && params.get('panel') === 'ai') window.toggleAIChat?.(true);
  }

  document.querySelectorAll('.workspace-nav-trigger').forEach(button => {
    button.addEventListener('click', () => {
      const group = button.closest('.workspace-nav-group');
      const shouldOpen = !group.classList.contains('is-open');
      closeMenus();
      group.classList.toggle('is-open', shouldOpen);
      button.setAttribute('aria-expanded', String(shouldOpen));
      group.querySelector('.workspace-nav-menu').hidden = !shouldOpen;
    });
    button.addEventListener('keydown', event => {
      if (event.key !== 'ArrowDown') return;
      event.preventDefault();
      if (button.getAttribute('aria-expanded') === 'false') button.click();
      Array.from(button.parentElement.querySelectorAll('.nav-mode-btn')).find(item => getComputedStyle(item).display !== 'none')?.focus();
    });
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.workspace-nav-group')) closeMenus();
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const group = document.querySelector('.workspace-nav-group.is-open');
    if (!group) return;
    closeMenus();
    group.querySelector('.workspace-nav-trigger').focus();
  });
  mobileSelect.addEventListener('change', () => activate(mobileSelect.value));
  mobileClose.addEventListener('click', () => close(activeKey));

  // Trust only a matching same-origin reader window, never a file ID by itself.
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || !event.data || typeof event.data !== 'object') return;
    const tab = tabs.get(fileKey(Number(event.data.fileId)));
    if (!tab?.frame || event.source !== tab.frame.contentWindow) return;
    if (event.data.type === 'file-workspace:auth-required') {
      window.showLoginOverlay?.();
    } else if (event.data.type === 'file-workspace:metadata' && typeof event.data.name === 'string') {
      tab.name = event.data.name.slice(0, 500) || `文件 ${Number(event.data.fileId)}`;
      tab.frame.title = tab.name;
      renderTabs();
    } else if (event.data.type === 'file-workspace:resize' && Number.isFinite(event.data.height)) {
      tab.frame.style.height = `${Math.min(1800, Math.max(480, event.data.height))}px`;
    }
  });
  window.addEventListener('popstate', event => {
    const state = event.state?.fileWorkspace;
    if (!state || !sessionReady) return;
    restoringHistory = true;
    try {
      // Also accept history entries produced by the previous single-source workspace.
      const key = state.activeKey ?? (state.fileId ? fileKey(state.fileId) : state.sourceMode ? `page:${state.sourceMode}` : null);
      if (tabs.has(key)) activate(key, false);
      else if (typeof key === 'string' && key.startsWith('page:') && canOpenPage(key.slice(5))) showSource(key.slice(5), false);
      else if (typeof key === 'string' && /^file:[1-9]\d*$/.test(key)) open(Number(key.slice(5)), undefined, false);
      else {
        savePosition();
        activeKey = null;
        render();
      }
    } finally { restoringHistory = false; }
  });

  // Permissions still come from the existing role/capability UI and backend.
  const navigation = document.querySelector('.portal-navigation');
  new MutationObserver(() => {
    navigation.querySelectorAll('.workspace-nav-group').forEach(group => {
      group.hidden = Array.from(group.querySelectorAll('.nav-mode-btn')).every(button => button.style.display === 'none' || button.classList.contains('hidden'));
    });
    for (const [key, tab] of tabs) if (tab.kind === 'page' && !canOpenPage(tab.mode)) close(key);
  }).observe(navigation, { subtree: true, attributes: true, attributeFilter: ['style', 'class'] });

  window.FileWorkspace = Object.freeze({ open, showSource, close, clear, restoreRoute,
    isReading: () => tabs.get(activeKey)?.kind === 'file',
    isPageActive: mode => activeKey === `page:${mode}`,
  });
  render();
})();
