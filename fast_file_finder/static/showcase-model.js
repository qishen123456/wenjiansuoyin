/* Independent presentation data. No business API, cookies or browser storage. */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ShowcaseModel = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const clone = value => JSON.parse(JSON.stringify(value));
  const timestamp = () => new Date().toISOString();
  const extension = name => (String(name).match(/\.[^.]+$/)?.[0] || '').toLowerCase();
  const mimeTypes = Object.freeze({
    '.pdf': 'application/pdf', '.html': 'text/html', '.htm': 'text/html',
    '.csv': 'text/csv', '.txt': 'text/plain', '.png': 'image/png',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
  });
  const csvCurrent = '供应商,服务内容,结算金额（元）,核对状态\n供应商 A,系统维护,96000,已核对\n供应商 B,数据服务,128000,已核对\n供应商 C,交付支持,76000,待补材料\n合计,,300000,模拟数据\n';
  const csvPrevious = '供应商,服务内容,结算金额（元）,核对状态\n供应商 A,系统维护,96000,已核对\n供应商 B,数据服务,128000,待核对\n供应商 C,交付支持,76000,待补材料\n合计,,300000,模拟数据\n';

  function seedFeishu() {
    return {
      sessionId: null,
      syncedAt: '',
      people: [
        { id: 'feishu-admin', memberId: 'member-1', name: '演示管理员', department: '系统管理', job: '系统管理员' },
        { id: 'feishu-finance', memberId: 'member-2', name: '林晓', department: '财务分析', job: '结算专员' },
        { id: 'feishu-operations', memberId: 'member-3', name: '陈宁', department: '业务运营', job: '运营专员' },
        { id: 'feishu-new', memberId: null, name: '许言', department: '交付支持', job: '交付专员' },
      ],
      groups: [{ id: 'group-operations', name: '运营日报群' }, { id: 'group-finance', name: '财务核对群' }],
      grants: [{ type: 'department', subject: '业务运营', folder: '经营分析', access: 'read' }],
      tasks: [{ id: 'notify-1', name: '经营文件归档日报', recipient: 'group-operations', time: '09:00', enabled: true, lastSent: '' }],
      reports: [
        { date: '2026-11-13', title: '供应商结算明细', owner: '财务分析', status: 'updated', updated: '08:40', fileId: 'file-5' },
        { date: '2026-11-13', title: '项目交付凭证', owner: '交付支持', status: 'missing', updated: '', fileId: 'file-7' },
        { date: '2026-11-13', title: '季度经营报告', owner: '业务运营', status: 'waiting', updated: '', fileId: 'file-1' },
        { date: '2026-11-12', title: '供应商结算明细', owner: '财务分析', status: 'updated', updated: '08:50', fileId: 'file-5' },
        { date: '2026-11-12', title: '项目交付凭证', owner: '交付支持', status: 'updated', updated: '09:10', fileId: 'file-7' },
        { date: '2026-11-12', title: '季度经营报告', owner: '业务运营', status: 'missing', updated: '', fileId: 'file-1' },
      ],
    };
  }

  function seedState() {
    const modified = '2026-11-13T01:30:00.000Z';
    const file = (id, name, folder, tags, fields) => ({
      id: `file-${id}`, name, ext: extension(name), type: mimeTypes[extension(name)] || '',
      folder, tags, size: 0, modified, versions: [], ...fields,
    });
    const files = [
      file(1, '演示案例_季度经营分析报告.pdf', '经营分析', ['季度', '收入', '经营', '分析', '报告'], { size: 120871, url: '/static/demo-documents/quarterly.pdf' }),
      file(2, '演示案例_供应商结算核对单.pdf', '供应商结算', ['供应商', '结算', '核对', '采购'], { size: 94244, url: '/static/demo-documents/settlement.pdf' }),
      file(3, '演示案例_季度经营简报.html', '经营分析', ['季度', '经营', '收入', '网页', '简报'], { size: 4490, url: '/static/demo-documents/brief.html' }),
      file(4, '演示案例_归档检查清单.htm', '归档材料', ['归档', '检查', '清单', '网页'], { size: 6189, url: '/static/demo-documents/archive.htm' }),
      file(5, '2026年10月_供应商结算明细.csv', '供应商结算', ['结算', '供应商', '10月', '明细', '财务'], {
        content: csvCurrent, size: 362,
        versions: [{ id: 'file-5', name: '2026年10月_供应商结算明细.csv', ext: '.csv', type: 'text/csv', folder: '供应商结算', tags: ['结算', '供应商', '10月'], content: csvPrevious, size: 362, modified: '2026-11-12T08:00:00.000Z', version: 1, savedAt: modified }],
      }),
      file(6, '2026年第三季度_业务收入.csv', '经营分析', ['季度', '收入', '经营', '业绩', '销售'], {
        content: '业务单元,本期收入（万元）,上期收入（万元）,同比\n销售一组,1200,1000,20%\n销售二组,900,800,12.5%\n线上渠道,1000,900,11.1%\n区域服务,680,600,13.3%\n合作渠道,700,700,0%\n合计,4480,4000,12%\n', size: 338,
      }),
      file(7, '项目交付_验收材料清单.csv', '归档材料', ['项目', '验收', '交付', '归档', '材料'], {
        content: '材料,负责人,状态\n交付清单,交付支持,已提交\n验收凭证,交付支持,待补充\n审批记录,财务分析,待确认\n归档目录,资料管理,已确认\n', size: 211,
      }),
      file(8, '演示使用说明.txt', '使用说明', ['演示', '说明', '帮助'], {
        content: '文件资产中心交互演示\n\n全部业务内容为模拟数据。\n上传、归档、审批、成员权限和备份仅改变当前页面的演示状态。\n刷新或重置后恢复初始案例，不影响正式系统。\n\n可以搜索“结算”或“经营”，打开多个文件并切换阅读。\nAI 区使用本地文件名和标签检索，不调用外部模型。\n', size: 331,
      }),
    ];
    return {
      files,
      archiveTasks: [
        { id: 'archive-1', title: '10月供应商结算材料归档', folder: '供应商结算', owner: '财务分析', due: '2026-11-15', status: 'pending', fileId: 'file-5' },
        { id: 'archive-2', title: '项目交付凭证补齐', folder: '归档材料', owner: '交付支持', due: '2026-11-16', status: 'pending', fileId: 'file-7' },
        { id: 'archive-3', title: '季度经营报告归档', folder: '经营分析', owner: '业务运营', due: '2026-11-12', status: 'complete', fileId: 'file-1' },
      ],
      approvals: [
        { id: 'approval-1', fileId: 'file-2', fileName: files[1].name, applicant: '林晓', reason: '供应商月度结算核对', status: 'pending' },
        { id: 'approval-2', fileId: 'file-1', fileName: files[0].name, applicant: '陈宁', reason: '季度经营例会阅读材料', status: 'pending' },
      ],
      members: [
        { id: 'member-1', name: '演示管理员', department: '系统管理', role: 'admin' },
        { id: 'member-2', name: '林晓', department: '财务分析', role: 'editor' },
        { id: 'member-3', name: '陈宁', department: '业务运营', role: 'viewer' },
      ],
      feishu: seedFeishu(),
      backups: [],
    };
  }

  function createDemo() {
    let state = seedState();
    let nextFile = 9;
    let nextBackup = 1;
    let nextFeishuTask = 2;
    const get = id => state.files.find(file => file.id === String(id)) || null;
    const required = (items, id, label) => {
      const item = items.find(entry => entry.id === String(id));
      if (!item) throw new Error(`${label}不存在，请重新选择`);
      return item;
    };

    function list({ query = '', type = '', folder = '' } = {}) {
      const terms = String(query).trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      const fileType = String(type).toLocaleLowerCase().replace(/^\./, '');
      return state.files.filter(file => {
        const haystack = [file.name, file.folder, ...file.tags].join(' ').toLocaleLowerCase();
        return terms.every(term => haystack.includes(term))
          && (!folder || file.folder === folder)
          && (!fileType || fileType === 'all' || file.ext.slice(1) === fileType || (fileType === 'html' && file.ext === '.htm'));
      });
    }

    function normalizeInput(input) {
      const name = String(input?.name || '').trim();
      if (!name || name.length > 500 || /[\u0000-\u001f]/.test(name)) throw new Error('请选择名称有效的文件');
      const size = Number(input.size ?? 0);
      if (!Number.isSafeInteger(size) || size < 0) throw new Error('文件大小无效');
      const ext = extension(name);
      const normalized = {
        name, ext, size, type: mimeTypes[ext] || String(input.type || ''),
        modified: timestamp(),
      };
      if (typeof input.content === 'string') normalized.content = input.content;
      if (typeof input.url === 'string' && input.url) normalized.url = input.url;
      return normalized;
    }

    function addFile(input) {
      const fields = normalizeInput(input);
      const file = { ...fields, id: `file-${nextFile++}`, folder: '本次上传', tags: ['上传', '本地'], versions: [] };
      state.files.unshift(file);
      return file;
    }

    function replaceFile(id, input) {
      const file = required(state.files, id, '文件');
      const fields = normalizeInput(input);
      const { versions, ...previous } = file;
      const version = versions.length + 1;
      versions.push({ ...clone(previous), version, savedAt: timestamp() });
      // Replacing content must not retain the previous file's URL or text.
      delete file.content;
      delete file.url;
      Object.assign(file, fields);
      return file;
    }

    function completeArchive(id) {
      const task = required(state.archiveTasks, id, '归档任务');
      task.status = 'complete';
      return task;
    }

    function decideApproval(id, decision) {
      if (!['approved', 'rejected'].includes(decision)) throw new Error('请选择通过或拒绝');
      const approval = required(state.approvals, id, '审批申请');
      if (approval.status !== 'pending') throw new Error('该申请已经处理');
      approval.status = decision;
      return approval;
    }

    function setMemberRole(id, role) {
      if (!['admin', 'editor', 'viewer'].includes(role)) throw new Error('请选择有效角色');
      const member = required(state.members, id, '成员');
      if (member.role === 'admin' && role !== 'admin' && state.members.filter(item => item.role === 'admin').length === 1) {
        throw new Error('请至少保留一位演示管理员');
      }
      member.role = role;
      return member;
    }

    function signInWithFeishu(id) {
      const person = required(state.feishu.people, id, '演示飞书身份');
      const created = person.memberId === null;
      if (created) {
        person.memberId = person.id;
        state.members.push({ id: person.memberId, name: person.name, department: person.department, role: 'viewer' });
      }
      state.feishu.sessionId = person.id;
      return { person, created };
    }

    function signOutOfFeishu() {
      state.feishu.sessionId = null;
    }

    function syncFeishuDirectory() {
      state.feishu.syncedAt = timestamp();
      return state.feishu.people.length;
    }

    function listGrantSubjects(type) {
      if (!state.feishu.syncedAt) return [];
      if (type === 'department') return [...new Set(state.feishu.people.map(person => person.department))];
      if (type === 'job') return [...new Set(state.feishu.people.map(person => `${person.department} / ${person.job}`))];
      return [];
    }

    function setFeishuGrant({ type, subject, folder, access }) {
      if (!listGrantSubjects(type).includes(subject)) throw new Error('请先同步通讯录，再选择授予对象');
      if (!state.files.some(file => file.folder === folder)) throw new Error('请先选择受控目录');
      if (!['read', 'write', 'none'].includes(access)) throw new Error('请选择目录权限');
      const rules = state.feishu.grants;
      const index = rules.findIndex(rule => rule.type === type && rule.subject === subject && rule.folder === folder);
      if (index !== -1) rules.splice(index, 1);
      if (access !== 'none') rules.push({ type, subject, folder, access });
    }

    function feishuAccess(id) {
      const person = required(state.feishu.people, id, '演示飞书成员');
      const isAdmin = state.members.some(member => member.id === person.memberId && member.role === 'admin');
      return [...new Set(state.files.map(file => file.folder))].map(folder => {
        if (isAdmin) return { folder, access: 'write' };
        const rules = state.feishu.grants.filter(rule => rule.folder === folder && (
          (rule.type === 'department' && rule.subject === person.department) ||
          (rule.type === 'job' && rule.subject === `${person.department} / ${person.job}`)
        ));
        const access = person.memberId ? (rules.find(rule => rule.access === 'write') || rules[0])?.access : null;
        return { folder, access: access || 'none' };
      });
    }

    function feishuRecipients() {
      const { groups, people, syncedAt } = state.feishu;
      return [...groups, ...(syncedAt ? people : [])];
    }

    function saveFeishuTask({ id, name, recipient, time, enabled }) {
      name = String(name || '').trim();
      if (!name || name.length > 80) throw new Error('请填写 1–80 字的任务名称');
      required(feishuRecipients(), recipient, '接收对象');
      if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time)) throw new Error('请选择有效的推送时间');
      const task = id ? required(state.feishu.tasks, id, '推送任务') : { id: `notify-${nextFeishuTask++}`, lastSent: '' };
      if (!id) {
        if (state.feishu.tasks.length >= 20) throw new Error('本次演示最多保留 20 条推送任务');
        state.feishu.tasks.push(task);
      }
      Object.assign(task, { name, recipient, time, enabled: Boolean(enabled) });
      return task;
    }

    function toggleFeishuTask(id) {
      const task = required(state.feishu.tasks, id, '推送任务');
      task.enabled = !task.enabled;
      return task;
    }

    function removeFeishuTask(id) {
      const task = required(state.feishu.tasks, id, '推送任务');
      state.feishu.tasks.splice(state.feishu.tasks.indexOf(task), 1);
    }

    function simulateFeishuSend(id) {
      const task = required(state.feishu.tasks, id, '推送任务');
      task.lastSent = timestamp();
      return task;
    }

    function createBackup() {
      const { backups, ...data } = state;
      const backup = {
        id: `backup-${nextBackup++}`, name: `演示快照 ${nextBackup - 1}`,
        createdAt: timestamp(), fileCount: data.files.length, snapshot: clone(data),
      };
      backups.unshift(backup);
      return backup;
    }

    function restoreBackup(id) {
      const backup = required(state.backups, id, '演示快照');
      state = { ...clone(backup.snapshot), backups: state.backups };
      return true;
    }

    function findForQuestion(question) {
      const text = String(question || '').trim().slice(0, 500).toLocaleLowerCase();
      if (!text) return { query: '', files: [], explanation: '请输入文件名、业务关键词或文件类型。' };
      const intents = ['供应商', '结算', '经营', '季度', '收入', '验收', '交付', '归档', '清单', 'pdf', 'html', 'csv'];
      const matched = intents.filter(term => text.includes(term));
      const type = ['pdf', 'html', 'csv'].find(term => matched.includes(term)) || '';
      const terms = matched.filter(term => term !== type);
      const fallback = text.replace(/^(请|麻烦)?(帮我)?(找一下|找一找|查找|搜索|找|查|看看)/, '').replace(/(相关的|相关|文件|材料|有哪些|有哪些呢|在哪里|在哪|一下|的|呢|吧|。|？|\?)/g, '').trim();
      const query = terms.join(' ') || (type ? '' : fallback);
      // An unrecognized question with no usable term must not return everything.
      const files = query || type ? list({ query, type }) : [];
      return {
        query: [query, type].filter(Boolean).join(' '), files,
        explanation: files.length ? `按文件名、目录和标签匹配到 ${files.length} 份演示文件。` : '当前演示文件中没有匹配结果。可以试试“结算”“经营”或“PDF”。',
      };
    }

    function reset() {
      state = seedState();
      nextFile = 9;
      nextBackup = 1;
      nextFeishuTask = 2;
      return state;
    }

    return Object.freeze({
      get state() { return state; }, list, get, addFile, replaceFile, reset,
      completeArchive, decideApproval, setMemberRole, createBackup, restoreBackup, findForQuestion,
      signInWithFeishu, signOutOfFeishu, syncFeishuDirectory, listGrantSubjects, setFeishuGrant, feishuAccess,
      feishuRecipients, saveFeishuTask, toggleFeishuTask, removeFeishuTask, simulateFeishuSend,
    });
  }

  return Object.freeze({ createDemo });
});
