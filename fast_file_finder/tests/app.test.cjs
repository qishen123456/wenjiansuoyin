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
