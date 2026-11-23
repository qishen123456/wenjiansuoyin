const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// The controller has no framework dependency. This DOM boundary keeps its public
// tab, history and message behavior testable without browser/network fixtures.
function createWorkspace({ query = '', role = 'super_admin', initialize } = {}) {
  let document;
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.dataset = {};
      this.style = {};
      this.attributes = {};
      this.hidden = false;
      this.className = '';
      this.scrollLeft = 0;
      this.listeners = new Map();
      this.contentWindow = tag === 'iframe' ? {} : undefined;
      this.classList = {
        contains: name => this.className.split(' ').includes(name),
        toggle: (name, force) => {
          const names = new Set(this.className.split(' ').filter(Boolean));
          const enabled = force ?? !names.has(name);
          if (enabled) names.add(name); else names.delete(name);
          this.className = [...names].join(' ');
          return enabled;
        },
        remove: name => this.classList.toggle(name, false),
      };
    }
    append(...children) {
      children.forEach(child => { child.parentElement = this; this.children.push(child); });
    }
    appendChild(child) { this.append(child); }
    replaceChildren(...children) {
      this.children.forEach(child => { child.parentElement = null; });
      this.children = [];
      this.append(...children);
    }
    add(option) { this.append(option); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    getBoundingClientRect() {
      if (this.dataset.workspaceId && this.parentElement) {
        const left = this.parentElement.children.indexOf(this) * 180 - this.parentElement.scrollLeft;
        return { left, right: left + 180, width: 180 };
      }
      return { left: 0, right: 500, width: 500 };
    }
    focus() { document.activeElement = this; }
    remove() {
      if (this.parentElement) {
        this.parentElement.children = this.parentElement.children.filter(child => child !== this);
        this.parentElement = null;
      }
    }
    closest(selector) {
      if (selector === '[data-workspace-id]' && this.dataset.workspaceId !== undefined) return this;
      return this.parentElement?.closest(selector) || null;
    }
    querySelectorAll() { return []; }
    querySelector(selector) {
      if (selector === '.active .workspace-tab-open') return this.children.find(child => child.classList.contains('active'))?.querySelector('.workspace-tab-open') || null;
      for (const child of this.children) {
        if (selector.split(', ').some(item => child.classList.contains(item.slice(1)))) return child;
        const nested = child.querySelector(selector);
        if (nested) return nested;
      }
      return null;
    }
  }

  const ids = Object.fromEntries([
    'workspaceSources', 'documentWorkspace', 'workspaceTabs',
    'workspaceMobileSelect', 'workspaceMobileClose', 'workspaceAnnouncement', 'workspaceEmpty', 'q',
  ].map(id => [id, new Element()]));
  const navigation = new Element('nav');
  document = {
    body: new Element('body'), activeElement: null,
    getElementById: id => ids[id],
    createElement: tag => new Element(tag),
    querySelectorAll: () => [],
    querySelector: selector => selector === '.portal-navigation' ? navigation : null,
    addEventListener() {},
  };
  const listeners = new Map();
  const frames = new Map();
  let nextFrame = 0;
  const context = {
    document, console, URLSearchParams,
    location: { origin: 'http://workspace.test', search: query },
    history: {
      state: {},
      replaceState(state) { this.state = state; },
      pushState(state) { this.state = state; },
    },
    MutationObserver: class { observe() {} },
    Option: function (text, value) { this.text = text; this.value = value; },
    requestAnimationFrame(callback) { const id = ++nextFrame; frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { frames.delete(id); },
    getComputedStyle: () => ({ display: 'block' }),
    scrollY: 0,
    scrollTo({ top }) { context.scrollY = top; },
    addEventListener(name, callback) { listeners.set(name, callback); },
  };
  const loads = [], activations = [], searches = [];
  context.canOpenWorkspacePage = mode => mode !== 'permission' || role === 'super_admin';
  context.activateWorkspacePage = mode => { activations.push(mode); };
  context.initializeWorkspacePage = mode => { loads.push(mode); return initialize?.(mode); };
  context.search = page => { searches.push({ page, query: ids.q.value }); };
  context.window = context;
  const flush = () => {
    const pending = [...frames.values()];
    frames.clear();
    pending.forEach(callback => callback());
  };
  vm.runInNewContext(readFileSync(join(__dirname, '../static/workspace.js'), 'utf8'), context);
  flush();
  return {
    context, ids, flush, loads, activations, searches, workspace: context.FileWorkspace,
    sendMessage(event) { listeners.get('message')(event); flush(); },
    open(id, name = `File ${id}`) { context.FileWorkspace.open(id, name); flush(); },
    close(id) { context.FileWorkspace.close(id); flush(); },
    showSource(mode = 'search') { const result = context.FileWorkspace.showSource(mode); flush(); return result; },
    pop(state) { listeners.get('popstate')({ state }); flush(); },
    activeFrame() { return ids.documentWorkspace.children.find(frame => !frame.hidden); },
  };
}

test('closing a background tab preserves the active document scroll', () => {
  const app = createWorkspace();
  app.open(1);
  app.context.scrollY = 111;
  app.open(2);
  app.context.scrollY = 220;
  const current = app.activeFrame();
  app.close(1);
  assert.equal(app.activeFrame(), current);
  assert.equal(app.context.scrollY, 220);
});

test('closing a document while viewing sources preserves the source scroll', () => {
  const app = createWorkspace();
  app.open(1);
  app.showSource();
  app.context.scrollY = 330;
  app.close(1);
  assert.equal(app.ids.workspaceSources.hidden, false);
  assert.equal(app.context.scrollY, 330);
});

test('opening the same file retains its iframe and document state', () => {
  const app = createWorkspace();
  app.open(1);
  const first = app.activeFrame();
  app.open(2);
  app.open(1);
  assert.equal(app.ids.documentWorkspace.children.length, 2);
  assert.equal(app.activeFrame(), first);
  app.open(1);
  assert.equal(app.ids.documentWorkspace.children.length, 2);
});

test('closing the current file selects its neighbor, then returns to sources', () => {
  const app = createWorkspace();
  app.showSource();
  app.context.scrollY = 90;
  app.open(1);
  const first = app.activeFrame();
  app.open(2);
  app.open(3);
  const third = app.activeFrame();
  app.open(2);
  app.close(2);
  assert.equal(app.activeFrame(), third);
  app.close(3);
  assert.equal(app.activeFrame(), first);
  app.close(1);
  assert.equal(app.workspace.isReading(), false);
  assert.equal(app.ids.workspaceSources.hidden, false);
  assert.equal(app.ids.documentWorkspace.children.length, 0);
  assert.equal(app.context.scrollY, 90);
});

test('metadata is accepted only from the matching same-origin reader', () => {
  const app = createWorkspace();
  app.open(1, 'Original');
  const first = app.activeFrame();
  app.open(2, 'Other');
  const second = app.activeFrame();
  const data = { type: 'file-workspace:metadata', fileId: 1, name: 'Spoofed' };
  app.sendMessage({ origin: 'https://untrusted.test', source: first.contentWindow, data });
  app.sendMessage({ origin: app.context.location.origin, source: second.contentWindow, data });
  assert.equal(first.title, 'Original');
  app.sendMessage({ origin: app.context.location.origin, source: first.contentWindow, data: { ...data, name: 'Verified' } });
  assert.equal(first.title, 'Verified');
});

test('only a trusted reader can request login and clear retained documents', () => {
  const app = createWorkspace();
  app.open(1);
  const first = app.activeFrame();
  app.open(2);
  let loginRequests = 0;
  app.context.showLoginOverlay = () => { loginRequests += 1; app.workspace.clear(); };
  const data = { type: 'file-workspace:auth-required', fileId: 1 };
  app.sendMessage({ origin: 'https://untrusted.test', source: first.contentWindow, data });
  app.sendMessage({ origin: app.context.location.origin, source: {}, data });
  assert.equal(loginRequests, 0);
  assert.equal(app.ids.documentWorkspace.children.length, 2);
  app.sendMessage({ origin: app.context.location.origin, source: first.contentWindow, data });
  assert.equal(loginRequests, 1);
  assert.equal(app.workspace.isReading(), false);
  assert.equal(app.ids.documentWorkspace.children.length, 0);
  assert.equal(app.ids.workspaceSources.hidden, true);
  assert.equal(app.ids.workspaceEmpty.hidden, false);
});

test('business tabs coexist and reuse their initialization when activated again', async () => {
  const app = createWorkspace();
  await app.showSource('search');
  await app.showSource('catalog');
  await app.showSource('archive');
  await app.showSource('catalog');
  assert.deepEqual(app.ids.workspaceTabs.children.map(tab => tab.dataset.workspaceId), ['page:search', 'page:catalog', 'page:archive']);
  assert.equal(app.loads.filter(mode => mode === 'catalog').length, 1);
  assert.equal(app.activations.at(-1), 'catalog');
});

test('each business page keeps its scroll and closing every tab produces an empty workspace', () => {
  const app = createWorkspace();
  app.showSource('search'); app.context.scrollY = 160;
  app.showSource('catalog'); app.context.scrollY = 310;
  app.showSource('search');
  assert.equal(app.context.scrollY, 160);
  app.close('page:catalog');
  assert.equal(app.context.scrollY, 160);
  app.close('page:search');
  assert.equal(app.ids.workspaceTabs.children.length, 0);
  assert.equal(app.ids.workspaceEmpty.hidden, false);
  assert.equal(app.ids.workspaceSources.hidden, true);
  assert.equal(app.ids.documentWorkspace.hidden, true);
  assert.equal(app.ids.workspaceMobileSelect.disabled, true);
  assert.equal(app.activations.at(-1), null);
});

test('a business tab can be awaited while its first initialization is still pending', async () => {
  let complete;
  const app = createWorkspace({ initialize: mode => mode === 'catalog' ? new Promise(resolve => { complete = resolve; }) : undefined });
  const first = app.showSource('catalog');
  const repeated = app.showSource('catalog');
  assert.equal(typeof first?.then, 'function');
  assert.equal(first, repeated);
  assert.equal(app.loads.filter(mode => mode === 'catalog').length, 1);
  complete();
  await first;
});

test('a failed business page retries on the next navigation, then caches success', async () => {
  let attempts = 0;
  const app = createWorkspace({ initialize: () => {
    if (++attempts === 1) return Promise.reject(new Error('Temporary failure'));
  } });
  await assert.rejects(app.showSource('catalog'), /Temporary failure/);
  await app.showSource('catalog');
  await app.showSource('catalog');
  assert.equal(attempts, 2);
  assert.equal(app.ids.workspaceTabs.children.length, 1);
});

test('clicking an existing failed tab retries without closing the tab', async () => {
  let attempts = 0;
  const app = createWorkspace({ initialize: mode => {
    if (mode === 'catalog' && ++attempts === 1) throw new Error('Temporary failure');
  } });
  await assert.rejects(app.showSource('catalog'), /Temporary failure/);
  await app.showSource('search');
  const catalog = app.ids.workspaceTabs.children.find(tab => tab.dataset.workspaceId === 'page:catalog');
  await catalog.querySelector('.workspace-tab-open').listeners.get('click')(); app.flush();
  assert.equal(attempts, 2);
  assert.equal(app.workspace.isPageActive('catalog'), true);
  assert.equal(app.ids.workspaceTabs.children.length, 2);
});

test('safe routes consume a trimmed search only once', async () => {
  const app = createWorkspace({ query: '?view=search&q=%20%E7%BB%93%E7%AE%97%20' });
  await app.workspace.restoreRoute(); app.flush();
  await app.workspace.restoreRoute();
  assert.deepEqual(app.searches, [{ page: 1, query: '结算' }]);
  assert.equal(app.ids.workspaceTabs.children[0].dataset.workspaceId, 'page:search');
});

test('permission routes cannot open the administrator page for ordinary members', async () => {
  const app = createWorkspace({ query: '?view=permission', role: 'user' });
  await app.workspace.restoreRoute(); app.flush();
  await app.showSource('permission');
  assert.equal(app.loads.includes('permission'), false);
  assert.deepEqual(app.ids.workspaceTabs.children.map(tab => tab.dataset.workspaceId), ['page:search']);
});

test('history restores page/file activation and scroll without repeating initial requests', async () => {
  const app = createWorkspace();
  await app.workspace.restoreRoute(); app.flush();
  app.context.scrollY = 110;
  const searchState = app.context.history.state;
  await app.showSource('catalog'); app.context.scrollY = 240;
  const catalogState = app.context.history.state;
  app.open(7); app.context.scrollY = 80;
  const fileState = app.context.history.state;
  app.pop(catalogState);
  assert.equal(app.workspace.isPageActive('catalog'), true);
  assert.equal(app.context.scrollY, 240);
  app.pop(searchState);
  assert.equal(app.workspace.isPageActive('search'), true);
  assert.equal(app.context.scrollY, 110);
  app.pop(fileState);
  assert.equal(app.workspace.isReading(), true);
  assert.equal(app.context.scrollY, 80);
  assert.equal(app.loads.filter(mode => mode === 'catalog').length, 1);
});

test('mobile selector switches and closes both business and file tabs', () => {
  const app = createWorkspace();
  app.showSource('catalog');
  app.open(9);
  const select = app.ids.workspaceMobileSelect;
  assert.deepEqual(select.children.map(option => option.value), ['page:catalog', 'file:9']);
  select.value = 'page:catalog'; select.listeners.get('change')(); app.flush();
  assert.equal(app.workspace.isPageActive('catalog'), true);
  app.ids.workspaceMobileClose.listeners.get('click')(); app.flush();
  assert.equal(app.workspace.isReading(), true);
  assert.deepEqual(select.children.map(option => option.value), ['file:9']);
});

test('new tabs reveal themselves horizontally without changing vertical position on reactivation', async () => {
  const app = createWorkspace();
  app.showSource('search'); app.context.scrollY = 155;
  app.showSource('catalog');
  app.showSource('archive');
  app.showSource('approval');
  assert.ok(app.ids.workspaceTabs.scrollLeft > 0);
  app.showSource('search');
  assert.equal(app.context.scrollY, 155);
  assert.equal(app.ids.workspaceTabs.scrollLeft, 0);
});

test('invalid routes are allowlisted and search queries are capped at 300 characters', async () => {
  const app = createWorkspace({ query: '?view=constructor&q=' + 'a'.repeat(301) });
  await app.workspace.restoreRoute(); app.flush();
  assert.deepEqual(app.loads, ['search']);
  assert.equal(app.searches[0].query.length, 300);
  await app.showSource('__proto__');
  assert.equal(app.ids.workspaceTabs.children.length, 1);
});

test('cleared authentication cannot reopen old history before the next successful login', async () => {
  const app = createWorkspace({ query: '?view=catalog' });
  await app.workspace.restoreRoute(); app.flush();
  const previous = app.context.history.state;
  app.workspace.clear(); app.flush();
  app.pop(previous);
  assert.equal(app.ids.workspaceTabs.children.length, 0);
  await app.workspace.restoreRoute(); app.flush();
  assert.equal(app.workspace.isPageActive('catalog'), true);
});

test('app navigation preserves await semantics, avoids repeat sync initialization and enforces admin access', async () => {
  const app = createWorkspace();
  const counts = { catalog: 0, sync: 0, permission: 0 };
  Object.assign(app.context, {
    currentUser: { role: 'user' },
    $: id => app.ids[id] ||= app.context.document.createElement('div'),
    togglePortalSidebar() {},
    updatePortalNavigation() { throw new Error('Legacy navigation must not refresh an existing tab'); },
    initCatalogDashboard: async () => { counts.catalog += 1; },
    refreshPortalSync: async () => { counts.sync += 1; },
    initPermissionDashboard: async () => { counts.permission += 1; },
  });
  const source = readFileSync(join(__dirname, '../static/app.js'), 'utf8');
  vm.runInNewContext(source.slice(source.indexOf('function canOpenWorkspacePage('), source.indexOf('async function initCatalogDashboard(')), app.context);
  await app.context.switchNavMode('catalog');
  await app.context.switchNavMode('sync');
  await app.context.switchNavMode('catalog');
  await app.context.switchNavMode('sync');
  assert.deepEqual(counts, { catalog: 1, sync: 1, permission: 0 });
  assert.equal(await app.context.switchNavMode('permission'), false);
  app.context.currentUser.role = 'super_admin';
  await app.context.switchNavMode('permission');
  assert.equal(counts.permission, 1);
  app.close('page:catalog'); app.close('page:sync'); app.close('page:permission');
  assert.equal(app.context.currentNavMode, '');
  assert.equal(app.ids.searchView.classList.contains('hidden'), true);
});

test('showcase links only open allowed panels once on their matching page', async () => {
  for (const [query, expected] of [['?view=search&panel=ai','ai'], ['?view=catalog&panel=upload','upload'], ['?view=archive&panel=upload',null], ['?view=search&panel=delete',null]]) {
    const app = createWorkspace({query});
    const opened = [];
    app.context.toggleAIChat = value => { assert.equal(value,true); opened.push('ai'); };
    app.context.uploadToFolder = () => opened.push('upload');
    await app.workspace.restoreRoute(); await app.workspace.restoreRoute();
    assert.deepEqual(opened, expected ? [expected] : []);
  }
});
