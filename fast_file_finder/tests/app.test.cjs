const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

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
