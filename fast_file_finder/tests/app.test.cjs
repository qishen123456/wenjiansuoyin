const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('password visibility follows the control and resets between login sessions', () => {
  const source = readFileSync(join(__dirname, '../static/app.js'), 'utf8');
  const password = { type: 'password', value: 'fixture', focus() { focused = this; } };
  const phone = { value: 'admin', focus() { focused = this; } };
  const toggle = { setAttribute(name, value) { this[name] = value; } };
  const overlay = { style: { display: 'none' } };
  const background = { inert: false };
  overlay.parentElement = { children: [background, overlay] };
  const nodes = { loginPassword: password, loginPhone: phone, loginPasswordToggle: toggle, loginOverlay: overlay };
  let focused;
  const context = { $: id => nodes[id], window: {}, currentUser: null };
  vm.runInNewContext(source.slice(source.indexOf('const loginBackground ='),
    source.indexOf('async function checkAuth(')), context);

  context.showLoginOverlay();
  assert.equal(focused, password);
  context.setLoginPasswordVisibility(true);
  assert.equal(password.type, 'text');
  assert.equal(toggle['aria-pressed'], 'true');
  assert.equal(password.value, 'fixture');
  context.hideLoginOverlay();
  assert.equal(password.type, 'password');
  assert.equal(toggle['aria-pressed'], 'false');
  assert.equal(background.inert, false);
  context.showLoginOverlay();
  assert.equal(password.type, 'password');
  assert.equal(focused, password);
});

test('login blocks overlapping requests and restores the submit button for retry', async () => {
  const source = readFileSync(join(__dirname, '../static/app.js'), 'utf8');
  const nodes = {
    loginPhone: { value: 'admin' }, loginPassword: { value: '' },
    loginError: { textContent: '' }, loginSubmit: { disabled: false, textContent: '登录' },
  };
  const pending = [];
  let completed = false;
  const context = {
    $: id => nodes[id], currentUser: null, window: {},
    api: () => new Promise((resolve, reject) => pending.push({ resolve, reject })),
    hideLoginOverlay: () => { completed = true; },
    applyUserUI() {}, showHomeCockpit() {}, initHomeDataCockpit() {},
    loadArchiveOverview() {}, loadStatus() {}, loadRecent() {},
  };
  vm.runInNewContext(source.slice(source.indexOf('async function doLogin('),
    source.indexOf('async function doLogout(')), context);

  await context.doLogin();
  assert.equal(pending.length, 0);
  assert.equal(nodes.loginSubmit.disabled, false);

  nodes.loginPassword.value = 'fixture';
  const failed = context.doLogin();
  assert.equal(nodes.loginSubmit.disabled, true);
  assert.equal(nodes.loginSubmit.textContent, '正在登录…');
  await context.doLogin();
  assert.equal(pending.length, 1);
  pending[0].reject(new Error('账号或密码不正确'));
  await failed;
  assert.equal(nodes.loginError.textContent, '账号或密码不正确');
  assert.equal(nodes.loginSubmit.disabled, false);
  assert.equal(nodes.loginSubmit.textContent, '登录');

  const retry = context.doLogin();
  assert.equal(pending.length, 2);
  pending[1].resolve({ user: { role: 'super_admin' } });
  await retry;
  assert.equal(completed, true);
  assert.equal(nodes.loginPassword.value, '');
  assert.equal(nodes.loginSubmit.disabled, false);
  assert.equal(nodes.loginSubmit.textContent, '登录');
});

test('opening a file from approval details dismisses the modal covering the reader', () => {
  const source = readFileSync(join(__dirname, '../static/app.js'), 'utf8');
  const start = source.indexOf('function closeApprovalDetail(');
  const end = source.indexOf('async function reviewRequest(', start);
  assert.ok(start >= 0 && end > start);
  let modal = null;
  let openedFile = null;
  const context = {
    approvalRows: [{ id: 1, file_id: 42, status: 'approved', file_name: '示例.xlsx', user_name: '测试用户', reason: '验证文件入口', workflow_events: [] }],
    approvalState: () => 'ready', approvalStateLabels: { ready: '已通过' },
    isAdminUser: () => false, esc: value => String(value ?? ''),
    fmtDate: () => ({ full: '' }),
    $: id => id === 'approvalDetailModal' ? modal : null,
    document: { body: { insertAdjacentHTML: (_position, html) => { modal = { html, remove: () => { modal = null; } }; } } },
    previewFile: id => { openedFile = id; },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  context.openApprovalDetail(1);
  assert.ok(modal, 'approval details opened');
  const action = modal.html.match(/onclick="([^"]+)">查看文件<\/button>/);
  assert.ok(action, 'file action exists');
  vm.runInContext(action[1], context);
  assert.equal(openedFile, 42);
  assert.equal(modal, null, 'the document must not remain covered by approval details');
});

test('member and access pages finish independently while another retained tab is active', async () => {
  const source = readFileSync(join(__dirname, '../static/app.js'), 'utf8');
  const pending = [], renders = [];
  const nodes = { memberLoadStatus: {}, permissionLoadStatus: {}, directoryGrantPanel: {replaceChildren() {}} };
  const context = {
    currentUser: {role: 'super_admin'}, currentNavMode: 'members', URLSearchParams,
    permissionOverviewRequests: {members: 0, access: 0}, permState: {},
    $: id => nodes[id], location: {search: '?tab=access&section=audit'},
    permissionTabs: {querySelectorAll: () => [{dataset: {section: 'audit'}}]},
    api: () => new Promise(resolve => pending.push(resolve)),
    renderMemberPanel: () => renders.push('members'),
    showPermissionSection: section => renders.push(section),
  };
  vm.runInNewContext(source.slice(source.indexOf('async function loadPermissionOverview('),
    source.indexOf('function openResetPasswordModal(')), context);
  const members = context.loadPermissionOverview('members');
  context.currentNavMode = 'access';
  const access = context.loadPermissionOverview('access');
  context.currentNavMode = 'catalog';
  context.location.search = '?tab=catalog';
  pending[1]({ok: true, users: []}); await access;
  pending[0]({ok: true, users: []}); await members;
  assert.deepEqual(renders, ['audit', 'members']);
});

test('notification list survives a tab switch and discards an earlier session’s response', async () => {
  const source = readFileSync(join(__dirname, '../static/operations.js'), 'utf8');
  const pending = [], nodes = {};
  const context = {
    $: id => nodes[id] ||= {replaceChildren() { this.textContent = this.innerHTML = ''; }},
    currentUser: {role: 'super_admin'}, currentNavMode: 'notifications',
    notificationState: {request: 0}, notificationTasksUrl: '/api/feishu/tasks',
    notificationRow: task => task.name,
    api: () => new Promise(resolve => pending.push(resolve)),
  };
  vm.runInNewContext(source.slice(source.indexOf('function resetNotificationSession('),
    source.indexOf('function selectNotificationType(')), context);
  const first = context.loadNotificationTasks();
  context.currentNavMode = 'members';
  pending[0]({tasks: [{name: '日报'}], preview: '示例'}); await first;
  assert.equal(nodes.notificationTaskRows.innerHTML, '日报');
  assert.equal(nodes.newNotificationTask.disabled, false);
  const second = context.loadNotificationTasks();
  context.resetNotificationSession();
  pending[1]({tasks: [{name: '旧会话'}]}); await second;
  assert.equal(nodes.notificationTaskRows.innerHTML, '');
  assert.equal(nodes.notificationPreviewText.textContent, '');
  assert.equal(nodes.newNotificationTask.disabled, true);
  assert.equal(context.notificationState.tasks.length, 0);
});
