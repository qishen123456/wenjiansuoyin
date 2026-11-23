const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '../static/preview.js'), 'utf8');

function productionFunctions(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `Missing reader function boundary: ${start}`);
  return source.slice(from, to);
}

function createReader() {
  const requests = [];
  const messages = [];
  const permissionNode = {};
  const context = {
    state: { permission: null, permissionSequence: 0, disposed: false, authRequiredSent: false },
    lifetime: new AbortController(),
    embedded: true,
    validId: true,
    fileId: 42,
    location: { origin: 'http://localhost:8765' },
    authHeaders: () => ({}),
    get: () => permissionNode,
    renderPermission: () => {},
    fetch: () => new Promise(resolve => requests.push(resolve)),
    window: { parent: { postMessage: (message, origin) => messages.push({ message, origin }) } },
  };
  vm.createContext(context);
  // Execute the production request/permission functions with controlled I/O.
  // Deferring responses makes the ordering regression deterministic without a server.
  vm.runInContext(
    productionFunctions('  async function api(', '  function reportHeight(')
      + productionFunctions('  async function loadPermissions(', '  async function saveDownload('),
    context,
  );
  return { context, requests, messages };
}

const response = (status, data) => ({ status, ok: status < 400, json: async () => data });

test('401 asks the same-origin workspace to clear authenticated file state', async () => {
  const { context, requests, messages } = createReader();
  const result = context.api('/api/file/42/preview-data');
  requests[0](response(401, { detail: '未登录' }));

  await assert.rejects(result, { status: 401 });
  assert.equal(messages.length, 1);
  assert.equal(messages[0].message.type, 'file-workspace:auth-required');
  assert.equal(messages[0].message.fileId, 42);
  assert.equal(messages[0].origin, 'http://localhost:8765');
});

test('older permissions cannot overwrite newer UI state, while callers receive their own result', async () => {
  const { context, requests } = createReader();
  const oldRequest = context.loadPermissions();
  const newRequest = context.loadPermissions();

  requests[1](response(200, { download: 'deny' }));
  assert.equal(await newRequest, 'deny');
  requests[0](response(200, { download: 'direct' }));
  assert.equal(await oldRequest, 'direct');
  assert.equal(context.state.permission, 'deny');
});

function createContentReader(data) {
  const nodes = new Map();
  const mediaRequests = [];
  const revoked = [];
  function node(tag = 'div') {
    const classes = new Set();
    return {
      tagName: tag.toUpperCase(), attributes: {}, children: [], dataset: {}, style: {},
      hidden: false, disabled: false, textContent: '', innerHTML: '', clientWidth: 900,
      naturalWidth: 600, naturalHeight: 800,
      classList: {
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        contains: name => classes.has(name),
        toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
      },
      setAttribute(name, value) { this.attributes[name] = value; },
      replaceChildren(...children) { this.children = children; },
      querySelectorAll() { return []; },
    };
  }
  const get = id => {
    if (!nodes.has(id)) { const element = node(); element.parentElement = node(); nodes.set(id, element); }
    return nodes.get(id);
  };
  class ReaderURL extends URL {}
  ReaderURL.createObjectURL = () => 'blob:preview';
  ReaderURL.revokeObjectURL = url => revoked.push(url);
  const context = {
    state: { sheet: '', page: 1, name: '', sequence: 0, disposed: false, blobURL: '', zoom: false, rotation: 0 },
    validId: true, fileId: 42, location: { origin: 'http://localhost:8765' },
    document: { createElement: node }, get, AbortController, URL: ReaderURL, URLSearchParams,
    authHeaders: () => ({}), reportHeight() {}, sendToWorkspace() {}, notifyAuthenticationRequired() {},
    getComputedStyle: () => ({ paddingLeft: '24', paddingRight: '24' }),
    api: async () => data,
    fetch: async url => { mediaRequests.push(url); return { ok: true, blob: async () => ({}) }; },
  };
  vm.createContext(context);
  vm.runInContext(
    productionFunctions('  const escapeHTML =', '  const state =')
      + productionFunctions('  function setName(', '  function renderPermission('),
    context,
  );
  return { context, get, mediaRequests, revoked };
}

test('HTML renders in an isolated srcdoc frame without a media request or active tools', async () => {
  const html = '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"></head><body><h1>报表</h1></body></html>';
  const { context, get, mediaRequests } = createContentReader({ kind: 'html', name: '报表.html', html, page: 1, page_count: 1 });
  await context.load();
  assert.equal(mediaRequests.length, 0);
  const frame = get('grid').children[0];
  assert.equal(frame?.tagName, 'IFRAME');
  assert.equal(frame.attributes.sandbox, '');
  assert.equal(frame.attributes.referrerpolicy, 'no-referrer');
  assert.equal(frame.srcdoc, html);
  assert.equal(get('mediaTools').hidden, true);
  assert.equal(get('sheets').hidden, true);
  assert.equal(get('prev').disabled, true);
  assert.equal(get('next').disabled, true);
  assert.match(get('info').textContent, /HTML/);
});

test('PDF uses authenticated media pages and preserves zoom/rotation while paging', async () => {
  const { context, get, mediaRequests } = createContentReader({ kind: 'pdf', name: '合同.pdf', page: 2, page_count: 3, asset_url: '/api/file/42/preview-media?page=2' });
  context.state.zoom = true;
  context.state.rotation = 90;
  await context.load({ page: 2 });
  context.layoutMedia();
  assert.equal(mediaRequests.length, 1);
  assert.equal(get('mediaTools').hidden, false);
  assert.equal(get('prev').disabled, false);
  assert.equal(get('next').disabled, false);
  assert.equal(get('page').textContent, '第 2 / 3 页');
  assert.match(get('info').textContent, /PDF/);
  assert.match(get('notice').textContent, /PDF/);
  assert.equal(context.state.zoom, true);
  assert.match(get('previewMedia').style.transform, /rotate\(90deg\)/);
});

test('unknown preview kinds fail explicitly without fetching a media asset', async () => {
  const { context, get, mediaRequests } = createContentReader({ kind: 'future-format', name: '未知文件', asset_url: '/api/file/42/preview-media' });
  await context.load();
  assert.equal(mediaRequests.length, 0);
  assert.match(get('grid').innerHTML, /不支持.*预览类型/);
  assert.equal(get('next').disabled, true);
});

test('legacy spreadsheet responses without kind still render escaped table cells', async () => {
  const { context, get, mediaRequests } = createContentReader({ name: '账单.xlsx', sheets: ['汇总'], sheet: '汇总', page: 1, rows: [['<script>']], column_count: 1, start_row: 1, row_count: 1, has_more: false });
  await context.load();
  assert.equal(mediaRequests.length, 0);
  assert.match(get('grid').innerHTML, /&lt;script&gt;/);
  assert.equal(get('sheets').hidden, false);
});

test('malformed HTML payload reports an error instead of inserting a document', async () => {
  const { context, get, mediaRequests } = createContentReader({ kind: 'html', name: '报表.htm', html: null, page: 1, page_count: 1 });
  await context.load();
  assert.equal(get('grid').children.length, 0);
  assert.equal(mediaRequests.length, 0);
  assert.match(get('grid').innerHTML, /HTML 预览内容为空或格式无效/);
  assert.equal(get('next').disabled, true);
});

test('a stale HTML response cannot replace a newer PDF page', async () => {
  const { context, get } = createContentReader();
  const requests = [];
  context.api = () => new Promise(resolve => requests.push(resolve));
  const oldRequest = context.load();
  const oldController = context.state.previewController;
  const newRequest = context.load({ page: 2 });
  assert.equal(oldController.signal.aborted, true);
  requests[1]({ kind: 'pdf', name: '新版.pdf', page: 2, page_count: 2, asset_url: '/api/file/42/preview-media?page=2' });
  await newRequest;
  requests[0]({ kind: 'html', name: '旧版.html', html: '<html><body>旧内容</body></html>', page: 1, page_count: 1 });
  await oldRequest;
  assert.equal(context.state.data.kind, 'pdf');
  assert.equal(get('grid').classList.contains('is-html'), false);
  assert.equal(get('page').textContent, '第 2 / 2 页');
  assert.equal(get('next').disabled, true);
});

test('existing image and presentation previews retain distinct labels and media controls', async () => {
  for (const [kind, expected] of [['image', '图片预览'], ['slides', '演示文稿']]) {
    const { context, get } = createContentReader({ kind, name: '原有文件', page: 1, page_count: 1, asset_url: '/api/file/42/preview-media?page=1' });
    await context.load();
    assert.match(get('info').textContent, new RegExp(expected));
    assert.equal(get('mediaTools').hidden, false);
    assert.equal(get('sheets').hidden, true);
  }
});
