const assert = require('node:assert/strict');
const test = require('node:test');
const { createDemo } = require('../static/showcase-model.js');

test('instances and reset isolate uploads and all workflow changes', () => {
  const demo = createDemo();
  const another = createDemo();
  demo.addFile({ name: '本地样本.txt', size: 3, content: 'test' });
  demo.completeArchive('archive-1');
  demo.decideApproval('approval-1', 'approved');
  demo.setMemberRole('member-2', 'viewer');
  demo.createBackup();
  assert.equal(another.state.files.length, 8);
  assert.equal(another.state.archiveTasks[0].status, 'pending');
  assert.deepEqual(demo.reset(), another.state);
});

test('search combines terms, type and folder, without executing uploaded names', () => {
  const demo = createDemo();
  assert.equal(demo.list({ query: '供应商 结算', type: 'PDF' }).length, 1);
  assert.equal(demo.list({ query: '季度', folder: '经营分析' }).length, 3);
  assert.equal(demo.list({ type: 'html' }).length, 2);
  assert.equal(demo.list({ query: '不存在' }).length, 0);
  const file = demo.addFile({ name: '<script>alert(1)</script>.txt', size: 1, content: 'x' });
  assert.equal(demo.list({ query: 'alert' })[0].id, file.id);
});

test('uploads with duplicate names remain separate; invalid input is atomic', () => {
  const demo = createDemo();
  const existing = demo.get('file-1');
  const added = demo.addFile({ name: existing.name, size: 2, url: 'blob:uploaded' });
  assert.notEqual(added.id, existing.id);
  assert.equal(demo.get('file-1').url, '/static/demo-documents/quarterly.pdf');
  assert.throws(() => demo.addFile({ name: ' ', size: 0 }));
  assert.throws(() => demo.addFile({ name: 'bad.txt', size: -1 }));
  assert.equal(demo.state.files.length, 9);
});

test('file replacement keeps readable old versions and removes stale URL/content', () => {
  const demo = createDemo();
  const file = demo.addFile({ name: 'v1.txt', size: 1, content: 'one', url: 'blob:first' });
  demo.replaceFile(file.id, { name: 'v2.pdf', size: 2, url: 'blob:second' });
  assert.equal(file.content, undefined);
  assert.equal(file.versions[0].content, 'one');
  assert.equal(file.versions[0].url, 'blob:first');
  demo.replaceFile(file.id, { name: 'v3.txt', size: 3, content: 'three' });
  assert.equal(file.url, undefined);
  assert.equal(file.versions[1].url, 'blob:second');
  assert.equal(file.versions.length, 2);
  assert.throws(() => demo.replaceFile(file.id, { name: '', size: 0 }));
  assert.equal(file.versions.length, 2);
});

test('archive, approval and role operations reject invalid or unsafe transitions', () => {
  const demo = createDemo();
  assert.throws(() => demo.completeArchive('not-found'));
  assert.throws(() => demo.decideApproval('approval-1', 'maybe'));
  demo.decideApproval('approval-1', 'rejected');
  assert.throws(() => demo.decideApproval('approval-1', 'approved'));
  assert.throws(() => demo.setMemberRole('member-1', 'viewer'));
  demo.setMemberRole('member-2', 'admin');
  demo.setMemberRole('member-1', 'viewer');
  assert.equal(demo.state.members[0].role, 'viewer');
  assert.throws(() => demo.setMemberRole('member-2', 'unknown'));
});

test('backup restore recovers files and workflow state without mutable snapshot aliases', () => {
  const demo = createDemo();
  const uploaded = demo.addFile({ name: 'original.txt', size: 3, content: 'old', url: 'blob:original' });
  const backup = demo.createBackup();
  demo.replaceFile(uploaded.id, { name: 'changed.txt', size: 4, content: 'new' });
  demo.completeArchive('archive-1');
  const afterSnapshot = demo.addFile({ name: 'late.txt', size: 1 });
  assert.equal(demo.restoreBackup(backup.id), true);
  assert.equal(demo.get(uploaded.id).content, 'old');
  assert.equal(demo.get(uploaded.id).url, 'blob:original');
  assert.equal(demo.get(afterSnapshot.id), null);
  assert.equal(demo.state.archiveTasks[0].status, 'pending');
  assert.equal(demo.state.backups.length, 1);
  demo.get(uploaded.id).content = 'modified again';
  demo.restoreBackup(backup.id);
  assert.equal(demo.get(uploaded.id).content, 'old');
  const next = demo.addFile({ name: 'new.txt', size: 1 });
  assert.notEqual(next.id, afterSnapshot.id);
});

test('the local assistant extracts supported intent and returns real matching file records', () => {
  const demo = createDemo();
  const result = demo.findForQuestion('帮我找一下供应商结算 PDF');
  assert.deepEqual(result.files.map(file => file.id), ['file-2']);
  assert.equal(demo.findForQuestion('找结算').files.length, 2);
  assert.equal(demo.findForQuestion('有哪些 HTML 文件？').files.length, 2);
  assert.equal(demo.findForQuestion('').files.length, 0);
  assert.equal(demo.findForQuestion('今天天气怎么样').files.length, 0);
  demo.addFile({ name: '新客户合同.txt', size: 10 });
  assert.equal(demo.findForQuestion('找新客户合同').files.length, 1);
});

test('first Feishu demo login provisions the ordinary account once and does not share state', () => {
  const demo = createDemo();
  assert.throws(() => demo.signInWithFeishu('unknown'), /演示飞书身份不存在/);
  const first = demo.signInWithFeishu('feishu-new');
  assert.equal(first.created, true);
  assert.equal(demo.state.members.find(member => member.id === first.person.memberId).role, 'viewer');
  assert.equal(demo.signInWithFeishu('feishu-new').created, false);
  assert.equal(demo.state.members.filter(member => member.id === first.person.memberId).length, 1);
  demo.signOutOfFeishu();
  assert.equal(demo.state.feishu.sessionId, null);
  assert.equal(createDemo().state.feishu.people.at(-1).memberId, null);
});

test('Feishu demo grants require directory sync and match the selected department or its specific job', () => {
  const demo = createDemo();
  const grant = { type: 'job', subject: '财务分析 / 结算专员', folder: '供应商结算', access: 'write' };
  const access = (person, folder) => demo.feishuAccess(person).find(item => item.folder === folder).access;
  assert.throws(() => demo.setFeishuGrant(grant), /请先同步通讯录/);
  assert.equal(demo.syncFeishuDirectory(), 4);
  demo.setFeishuGrant(grant);
  assert.equal(access('feishu-finance', grant.folder), 'write');
  assert.equal(access('feishu-operations', grant.folder), 'none');
  assert.equal(access('feishu-operations', '经营分析'), 'read');
  demo.setFeishuGrant({ ...grant, access: 'none' });
  assert.equal(access('feishu-finance', grant.folder), 'none');
});

test('Feishu demo push tasks validate the recipient and time and participate in backup and reset', () => {
  const demo = createDemo();
  const input = { name: '会议资料日报', recipient: 'feishu-finance', time: '09:30', enabled: true };
  assert.throws(() => demo.saveFeishuTask(input), /接收对象不存在/);
  demo.syncFeishuDirectory();
  assert.throws(() => demo.saveFeishuTask({ ...input, time: '27:15' }), /推送时间/);
  const task = demo.saveFeishuTask(input);
  demo.toggleFeishuTask(task.id);
  assert.equal(task.enabled, false);
  demo.simulateFeishuSend(task.id);
  assert.ok(task.lastSent);
  const backup = demo.createBackup();
  demo.removeFeishuTask(task.id);
  assert.equal(demo.state.feishu.tasks.length, 1);
  demo.restoreBackup(backup.id);
  assert.equal(demo.state.feishu.tasks.length, 2);
  demo.reset();
  assert.equal(demo.state.feishu.tasks.length, 1);
  assert.equal(demo.state.feishu.syncedAt, '');
});
