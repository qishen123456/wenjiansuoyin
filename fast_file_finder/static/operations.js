/* File operations use server-side capability checks. */
let fileOperationReturnFocus = null;
let fileOperationBusy = false;

function closeFileOperation() {
  if (fileOperationBusy) return;
  document.getElementById('fileOperationModal')?.remove();
  fileOperationReturnFocus?.focus();
}

function operationDialog(title, body, action) {
  closeFileOperation();
  fileOperationReturnFocus = document.activeElement;
  document.body.insertAdjacentHTML('beforeend', `
    <div class="modal-overlay file-operation-overlay" id="fileOperationModal">
      <section class="file-operation-dialog" role="dialog" aria-modal="true" aria-labelledby="fileOperationTitle" tabindex="-1">
        <header class="file-operation-header"><h3 id="fileOperationTitle">${esc(title)}</h3><button class="file-operation-close" aria-label="关闭" onclick="closeFileOperation()">×</button></header>
        <div class="file-operation-body">${body}</div>
        <footer class="file-operation-footer"><p id="fileOperationStatus" role="status" aria-live="polite"></p><div><button class="file-operation-cancel" onclick="closeFileOperation()">取消</button>${action}</div></footer>
      </section>
    </div>`);
  const overlay = $('fileOperationModal');
  overlay.onclick = event => { if (event.target === overlay) closeFileOperation(); };
  overlay.onkeydown = event => {
    if (event.key === 'Escape') { event.preventDefault(); closeFileOperation(); }
    if (event.key !== 'Tab') return;
    const controls = [...overlay.querySelectorAll('button,input,[tabindex="0"]')].filter(el => !el.disabled && el.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  overlay.querySelector('.file-operation-dialog').focus();
}

function filePicker(id, expectedName = '') {
  const accept = expectedName ? `accept="${esc('.' + expectedName.split('.').pop())}"` : '';
  return `<label class="operation-dropzone" id="${id}Zone">
    <input type="file" id="${id}" ${accept} aria-label="选择${expectedName ? '同名替换' : '上传'}文件">
    <span class="operation-upload-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6M12 17v-5m-3 3 3-3 3 3"/></svg></span>
    <strong id="${id}Name">点击选择文件，或拖到这里</strong>
    <small id="${id}Meta">${expectedName ? '选择与当前文件同名的文件' : '选择要上传的文档或图片'}</small>
    <span class="operation-browse">选择文件</span>
  </label>`;
}

function wireFilePicker(id, buttonId, expectedName = '') {
  const input = $(id), zone = $(id + 'Zone'), button = $(buttonId);
  const change = () => {
    const file = input.files[0];
    const valid = Boolean(file && (!expectedName || file.name === expectedName));
    $(id + 'Name').textContent = file?.name || '点击选择文件，或拖到这里';
    $(id + 'Meta').textContent = file ? `${fmtBytes(file.size)} · 可重新选择` : '选择要上传的文档或图片';
    zone.classList.toggle('has-file', Boolean(file));
    button.disabled = !valid;
    $('fileOperationStatus').textContent = file && !valid ? '文件名需要与当前文件一致，请重新选择。' : '';
  };
  input.onchange = change;
  zone.ondragover = event => { event.preventDefault(); zone.classList.add('drag-over'); };
  zone.ondragleave = () => zone.classList.remove('drag-over');
  zone.ondrop = event => {
    event.preventDefault(); zone.classList.remove('drag-over');
    if (fileOperationBusy || !event.dataTransfer.files.length) return;
    const transfer = new DataTransfer(); transfer.items.add(event.dataTransfer.files[0]);
    input.files = transfer.files; change();
  };
}

function setFileOperationBusy(busy) {
  fileOperationBusy = busy;
  $('fileOperationModal')?.querySelectorAll('button,input').forEach(el => { el.disabled = busy; });
}

async function rawFileRequest(url, method, file) {
  const token = sessionStorage.getItem('fileFinderAccessToken') || '';
  const response = await fetch(url, { method, headers: { 'Content-Type': 'application/octet-stream', ...(token ? { 'X-App-Token': token } : {}) }, body: file });
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '文件写入失败，请重试');
  return data;
}

async function uploadToFolder() {
  operationDialog('上传文件', `
    <label class="operation-field">目标文件夹<input id="uploadFolderPath" class="operation-input" value="${esc(catalogState.selectedNode?.path || '')}" placeholder="输入已授权文件夹的完整路径"></label>
    ${filePicker('uploadSource')}
    <p class="operation-hint">上传后自动更新索引。同名文件请使用“更新／恢复”。</p>`,
    '<button class="file-operation-primary" id="submitFileUpload" disabled>上传文件</button>');
  wireFilePicker('uploadSource', 'submitFileUpload');
  $('submitFileUpload').onclick = async () => {
    const file = $('uploadSource').files[0], path = $('uploadFolderPath').value.trim();
    if (!file || !path) { $('fileOperationStatus').textContent = '请选择文件和目标文件夹。'; return; }
    setFileOperationBusy(true);
    try {
      const access = await api('/api/files/write-context?' + new URLSearchParams({ path }));
      if (!access.enabled) throw Error('当前文件夹为只读，请联系管理员。');
      if (!access.upload) throw Error('你没有此文件夹的上传权限。');
      if (file.size > access.max_mb * 1024 * 1024) throw Error(`文件超过 ${access.max_mb} MB 上传上限。`);
      $('fileOperationStatus').textContent = '正在上传并更新索引…';
      const result = await rawFileRequest('/api/files/upload?' + new URLSearchParams({ path, name: file.name }), 'POST', file);
      setFileOperationBusy(false); closeFileOperation(); toast(result.message);
      loadCatalogTree(); loadCatalogFiles();
    } catch (error) { setFileOperationBusy(false); $('fileOperationStatus').textContent = error.message; }
  };
}

async function updateFileContent(id) {
  try {
    const context = await api(`/api/file/${id}/write-version`);
    const versions = await api(`/api/file/${id}/versions`);
    operationDialog('更新文件', `
      <div class="operation-current-file"><span class="operation-extension">${esc(context.name.split('.').pop().toUpperCase())}</span><div><span>当前文件</span><strong>${esc(context.name)}</strong></div></div>
      <h4 class="operation-section-title">选择替换文件</h4>
      ${filePicker('updateSource', context.name)}
      <p class="operation-hint">更新前自动保留当前副本，原文件发生变化时会提示重新确认。</p>
      <section class="operation-versions"><div class="operation-version-heading"><h4>历史副本</h4><span>${versions.items.length} 份可恢复</span></div>
        ${versions.items.length ? `<div class="operation-version-list">${versions.items.map(v => `<div class="operation-version-row"><div><time>${esc(fmtDate(v.created_at).full)}</time><small>${esc(fmtBytes(v.size))}</small></div><button class="operation-restore" data-restore-version="${esc(v.id)}">恢复此副本</button></div>`).join('')}</div>` : '<div class="operation-empty"><strong>暂无历史副本</strong><span>首次更新后，当前文件会保存在这里。</span></div>'}
      </section>`, '<button class="file-operation-primary" id="submitFileUpdate" disabled>确认更新</button>');
    wireFilePicker('updateSource', 'submitFileUpdate', context.name);
    $('submitFileUpdate').onclick = async () => {
      const file = $('updateSource').files[0];
      if (!file || file.name !== context.name) return;
      setFileOperationBusy(true);
      try {
        $('fileOperationStatus').textContent = '正在保留副本并更新文件…';
        const data = await rawFileRequest(`/api/file/${id}/content?` + new URLSearchParams({ expected_version: context.version }), 'PUT', file);
        setFileOperationBusy(false); closeFileOperation(); toast(data.message); loadCatalogFiles();
      } catch (error) { setFileOperationBusy(false); $('fileOperationStatus').textContent = error.message; }
    };
    $('fileOperationModal').querySelectorAll('[data-restore-version]').forEach(button => {
      button.onclick = async () => {
        if (!confirm('恢复到此副本？当前文件也会保留为历史副本。')) return;
        setFileOperationBusy(true);
        try {
          const data = await api(`/api/file/${id}/versions/${button.dataset.restoreVersion}/restore?` + new URLSearchParams({ expected_version: context.version }), { method: 'POST' });
          setFileOperationBusy(false); closeFileOperation(); toast(data.message); loadCatalogFiles();
        } catch (error) { setFileOperationBusy(false); $('fileOperationStatus').textContent = error.message; }
      };
    });
  } catch (error) { toast(error.message); }
}

async function loadBackupPanel(){const panel=$('systemBackupPanel');if(!panel || panel.dataset.busy==='true')return;const previousStatus=$('backupStatus')?.textContent || '';if(currentUser?.role!=='super_admin'){panel.hidden=true;return;}panel.hidden=false;
  try{const data=await api('/api/system/backups');panel.innerHTML=`<div class="home-panel-heading"><h2>系统数据备份</h2><button class="tree-tool-btn" id="createSystemBackup">立即备份</button></div><p>${esc(data.description)}</p><p>${data.interval_hours>0?`每 ${data.interval_hours} 小时检查备份周期`:'定时备份已关闭'} · 存放于服务器备份目录</p><p>文件更新旧副本在文件的“更新／恢复”中管理。此处恢复验证不覆盖在线系统。</p><div class="grant-audit">${data.items.map(item=>`<div><b>${esc(portalMonitorTime(item.created_at))}</b> · ${esc(fmtBytes(item.size))}<button class="tree-tool-btn" data-verify-backup="${esc(item.name)}">验证恢复</button></div>`).join('')||'<p>尚无数据库备份</p>'}</div><div id="backupStatus" role="status">${esc(previousStatus)}</div>`;
    $('createSystemBackup').onclick=async()=>{const button=$('createSystemBackup');button.disabled=true;panel.dataset.busy='true';try{await api('/api/system/backups',{method:'POST',timeoutMs:120000});panel.dataset.busy='false';await loadBackupPanel();$('backupStatus').textContent='备份已完成并通过数据库完整性检查。';}catch(e){$('backupStatus').textContent=e.message;button.disabled=false;}finally{panel.dataset.busy='false';}};
    panel.querySelectorAll('[data-verify-backup]').forEach(button=>button.onclick=async()=>{button.disabled=true;panel.dataset.busy='true';try{const result=await api(`/api/system/backups/${button.dataset.verifyBackup}/verify`,{method:'POST',timeoutMs:120000});$('backupStatus').textContent=result.message;}catch(e){$('backupStatus').textContent=e.message;}finally{panel.dataset.busy='false';button.disabled=false;}});
  }catch(e){panel.textContent='备份信息读取失败：'+e.message;}
}

function feishuDailyDetails(daily, members) {
  if (!daily) return '';
  const recipient = daily.recipient || {type:'off', id:'', label:'', source:'environment'};
  const personal = recipient.type === 'open_id';
  const group = recipient.type === 'chat_id';
  const activeMembers = members.filter(member => member.active && member.open_id);
  const running = daily.task_enabled && (personal || group);
  const state = !running ? '已暂停' : (daily.configuration_error || daily.report_error) ? '执行异常' : '运行中';
  const lastSent = daily.last_sent_at
    ? new Date(daily.last_sent_at * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false})
    : '暂无成功记录';
  const table = daily.report_table_configured ? '报表明细已连接' : '报表明细未连接';
  const errors = [daily.configuration_error, daily.report_error, daily.table_error, daily.report_table_error].filter(Boolean);
  const label = personal ? `飞书私聊 · ${recipient.label || recipient.id}`
    : group ? `指定飞书群 · ${recipient.id}` : '暂无接收人';
  return `<section class="feishu-daily" id="feishuDailyPanel" aria-labelledby="feishuDailyHeading">
    <div class="feishu-daily-heading">
      <div><h4 id="feishuDailyHeading">飞书同步日报</h4>
        <p>汇总文件更新、索引进展与归档异常，定时推送到飞书。</p></div>
      <span class="feishu-task-state" data-running="${state === '运行中'}">${esc(state)}</span>
    </div>
    <dl class="feishu-task-facts">
      <div><dt>执行计划 · 北京时间</dt><dd>每天 ${esc(daily.schedule)}</dd></div>
      <div><dt>下次执行</dt><dd>${esc(daily.next_run || '暂无安排')}</dd></div>
      <div><dt>最近成功发送</dt><dd>${esc(lastSent)}</dd></div>
      <div><dt>多维表同步</dt><dd>${esc(table)}</dd></div>
    </dl>
    <div class="feishu-recipient-row">
      <div><span>推送目标</span><strong>${esc(label)}</strong></div>
      <div id="feishuRecipientActions" class="feishu-recipient-actions">
        <button id="editFeishuRecipient" type="button" class="btn-archive-primary" aria-controls="feishuDailyRecipientForm">${personal || group ? '编辑任务' : '添加接收人'}</button>
        ${personal || group ? `<button id="toggleFeishuDailyTask" type="button" class="tree-tool-btn">${running ? '暂停任务' : '恢复任务'}</button>
          <button id="deleteFeishuRecipient" type="button" class="tree-tool-btn feishu-danger" aria-controls="feishuRecipientRemoval">删除接收人</button>` : ''}
      </div>
    </div>
    <div id="feishuRecipientRemoval" class="feishu-recipient-actions" hidden>
      <p>确认删除「${esc(label)}」？删除后将停止向该对象发送日报。</p>
      <button id="confirmFeishuRemoval" type="button" class="tree-tool-btn feishu-danger">确认删除</button>
      <button id="cancelFeishuRemoval" type="button" class="tree-tool-btn">取消</button>
    </div>
    <form id="feishuDailyRecipientForm" hidden>
      <div class="feishu-daily-controls">
        <label>每日发送时间（北京时间）<input id="feishuDailyTime" type="time" required step="60" value="${esc(daily.schedule)}"></label>
        <label>接收方式<select id="feishuDailyRecipientType">
          <option value="open_id" ${personal ? 'selected' : ''}>个人私聊</option>
          <option value="chat_id" ${group ? 'selected' : ''}>指定飞书群</option>
        </select></label>
        <label id="feishuDailyMemberField" ${personal ? '' : 'hidden'}>飞书成员
          <select id="feishuDailyMember" ${personal ? 'required' : 'disabled'}>
            <option value="">${activeMembers.length ? '请选择一位成员' : '请先同步飞书通讯录'}</option>
            ${personal && !activeMembers.some(member => member.open_id === recipient.id)
              ? `<option value="${esc(recipient.id)}" selected>${esc(recipient.label || recipient.id)}（当前接收人）</option>` : ''}
            ${activeMembers.map(member => `<option value="${esc(member.open_id)}" ${personal && member.open_id === recipient.id ? 'selected' : ''}>
              ${esc([member.name, member.department, member.phone_tail ? '尾号 ' + member.phone_tail : ''].filter(Boolean).join(' · '))}
            </option>`).join('')}
          </select>
        </label>
        <label id="feishuDailyGroupField" ${group ? '' : 'hidden'}>飞书群标识
          <input id="feishuDailyGroup" type="text" placeholder="群设置中复制的 oc_ 标识" value="${group ? esc(recipient.id) : ''}" ${group ? 'required' : 'disabled'}>
        </label>
        <button id="saveFeishuDailyRecipient" type="submit" class="btn-archive-primary">保存任务</button>
        <button id="cancelFeishuEdit" type="button" class="tree-tool-btn">取消</button>
      </div>
      <div id="feishuDailyLookup" class="feishu-chat-lookup" hidden>
        <span>在飞书群右上角打开群设置，复制群 ID。</span>
        <button id="verifyFeishuChat" type="button" class="tree-tool-btn">核对群名</button>
        <span id="feishuChatFeedback" role="status"></span>
      </div>
      <p class="feishu-daily-note">每次定时推送只发给这里指定的人或群。当天已发送时，保存设置后从下一次定时推送生效。</p>
    </form>
    <p id="feishuDailySaveStatus" class="feishu-daily-save-status" role="status" aria-live="polite"></p>
    ${errors.map(error => `<p class="feishu-daily-error" role="status">${esc(error)}</p>`).join('')}
    ${daily.preview ? `<details class="feishu-daily-preview"><summary>查看日报内容</summary><pre>${esc(daily.preview)}</pre></details>` : ''}
  </section>`;
}

function wireFeishuDailyRecipient(daily, members) {
  const form = $('feishuDailyRecipientForm');
  if (!form) return;
  const type = $('feishuDailyRecipientType');
  const member = $('feishuDailyMember');
  const group = $('feishuDailyGroup');
  const status = $('feishuDailySaveStatus');
  const updateFields = () => {
    const personal = type.value === 'open_id';
    const isGroup = type.value === 'chat_id';
    $('feishuDailyMemberField').hidden = member.disabled = !personal;
    $('feishuDailyGroupField').hidden = group.disabled = !isGroup;
    member.required = personal;
    group.required = isGroup;
    $('feishuDailyLookup').hidden = !isGroup;
  };
  const saved = daily.recipient;
  const actions = $('feishuRecipientActions');
  const removal = $('feishuRecipientRemoval');
  const edit = $('editFeishuRecipient');
  const remove = $('deleteFeishuRecipient');
  let saving = false;
  function closeEditor() {
    if (saving) return;
    form.hidden = removal.hidden = true;
    actions.hidden = false;
    status.textContent = '';
    edit.focus();
  }
  edit.onclick = () => {
    form.reset();
    $('feishuChatFeedback').textContent = '';
    updateFields();
    actions.hidden = true;
    form.hidden = false;
    status.textContent = '';
    $('feishuDailyTime').focus();
  };
  if (remove) remove.onclick = () => {
    actions.hidden = true;
    removal.hidden = false;
    status.textContent = '';
    $('cancelFeishuRemoval').focus();
  };
  $('cancelFeishuRemoval').onclick = $('cancelFeishuEdit').onclick = closeEditor;
  async function persist(kind, id, changes = {}) {
    if (saving) return;
    saving = true;
    const controls = [...form.elements, ...removal.querySelectorAll('button'), ...actions.querySelectorAll('button')];
    controls.forEach(control => { control.disabled = true; });
    status.textContent = kind === 'off' ? '正在删除…' : '正在保存…';
    try {
      const result = await api('/api/system/feishu/daily/recipient', {
        method:'PUT', body:{type:kind, id, expected_id:saved?.id || '', revision:daily.revision, ...changes},
      });
      if (!form.isConnected) return;
      $('feishuDailyPanel').outerHTML = feishuDailyDetails(result, members);
      wireFeishuDailyRecipient(result, members);
      $('feishuDailySaveStatus').textContent = kind === 'off' ? '已删除接收人，日报推送已暂停。' : '任务设置已保存';
      $('editFeishuRecipient').focus({preventScroll:true});
    } catch (error) {
      saving = false;
      controls.forEach(control => { control.disabled = false; });
      updateFields();
      status.textContent = error.message;
    }
  }
  const toggle = $('toggleFeishuDailyTask');
  if (toggle) toggle.onclick = () => persist(saved.type, saved.id, {task_enabled:!daily.task_enabled});
  $('confirmFeishuRemoval').onclick = () => persist('off', '');
  const feedback = $('feishuChatFeedback');
  group.oninput = () => { feedback.textContent = ''; };
  $('verifyFeishuChat').onclick = async () => {
    const id = group.value.trim();
    if (!/^oc_[A-Za-z0-9_-]+$/.test(id)) {
      feedback.textContent = '请先填写 oc_ 开头的群 ID。';
      return;
    }
    feedback.textContent = '正在核对…';
    try {
      const result = await api('/api/system/feishu/daily/verify-chat', {method:'POST', body:{chat_id:id}});
      if (group.value.trim() === id) feedback.textContent = '飞书群名：' + result.name;
    } catch (error) {
      if (group.value.trim() === id) feedback.textContent = error.message;
    }
  };
  type.onchange = updateFields;
  form.onsubmit = event => {
    event.preventDefault();
    if (saving) return;
    const kind = type.value;
    const id = kind === 'open_id' ? member.value : group.value.trim();
    if (kind === 'open_id' && !id) {
      status.textContent = '请从通讯录选择一位飞书成员。';
      return;
    }
    if (kind === 'chat_id' && !/^oc_[A-Za-z0-9_-]+$/.test(id)) {
      status.textContent = '请输入以 oc_ 开头的飞书群标识。';
      return;
    }
    if (kind === 'chat_id' && saved?.id !== id &&
      !window.confirm(`确认将同步日报发给飞书群 ${id}？请先核对群成员范围。`)) return;
    persist(kind, id, {schedule:$('feishuDailyTime').value, task_enabled:saved?.id ? daily.task_enabled : true});
  };
}

function mountFeishuDailyTask(host, daily, members) {
  if (!daily || !host) return;
  $('feishuDailyPanel')?.remove();
  host.innerHTML = feishuDailyDetails(daily, members);
  wireFeishuDailyRecipient(daily, members);
}

async function loadSyncFeishuTask() {
  const host = $('syncFeishuTaskHost');
  if (!host || currentUser?.role !== 'super_admin') return;
  try {
    const data = await api('/api/system/feishu');
    if (currentNavMode === 'sync' && data.source === 'feishu') mountFeishuDailyTask(host, data.daily, data.members);
  } catch (error) {
    if (currentNavMode === 'sync') host.textContent = '日报任务读取失败：' + error.message;
  }
}

const memberFilter = { query:'', source:'', department:'', page:1 };
const memberRoles = {super_admin:'超级管理员', dept_admin:'部门管理员', user:'普通成员'};
const memberSources = {linked:'飞书 + 平台', feishu:'仅飞书', local:'仅平台'};
const memberMatchLabels = {
  review:'待确认关联', conflict:'关联冲突', resync:'待重新同步', no_phone:'手机号不可见',
  no_account:'待开通', disabled:'账号已停用', inactive:'已离职',
};

function unifiedMembers(users, members) {
  const byId = new Map(users.map(user => [user.id, user]));
  const byFeishu = new Map(users.filter(user => user.feishu_id).map(user => [user.feishu_id, user]));
  const included = new Set();
  const rows = members.map(member => {
    const account = byId.get(member.bound_user_id) || byFeishu.get(member.open_id);
    if (account) included.add(account.id);
    return {member, account};
  });
  for (const account of users) {
    if (!included.has(account.id)) rows.push({account});
  }
  return rows.map(({member, account}) => {
    const source = member || account?.feishu_id ? (account ? 'linked' : 'feishu') : 'local';
    const name = member?.name || account?.display_name || account?.name || '未命名成员';
    const departments = (member?.department || account?.department || '').split('、').filter(Boolean);
    const job = member?.job_title || account?.job_title || '';
    const search = [name, account?.name, account?.phone, member?.phone_tail, ...departments, job].filter(Boolean).join(' ').toLowerCase();
    return {member, account, source, name, departments, job, search};
  }).sort((a,b) => a.name.localeCompare(b.name, 'zh-CN'));
}

function renderMemberPanel() {
  const directory = permState.directory;
  const members = unifiedMembers(permState.users, directory.members || []);
  const departments = [...new Set(members.flatMap(row => row.departments))].sort((a,b) => a.localeCompare(b,'zh-CN'));
  const connected = directory.source === 'feishu';
  const panel = $('memberPanel');
  panel.innerHTML = `<div class="permission-panel-toolbar"><strong>成员名单</strong>
    <div class="permission-roster-actions">${connected ? '<button id="syncFeishu" class="tree-tool-btn" type="button">同步飞书</button>' : ''}
      <button id="createMemberAccount" type="button" class="btn-archive-primary">新建平台账号</button></div></div>
    <div class="permission-roster-filters">
      <label>查找成员<input type="search" id="memberQuery" placeholder="姓名、手机号或岗位"></label>
      <label>账号来源<select id="memberSource"><option value="">全部来源</option>
        ${Object.entries(memberSources).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')}</select></label>
      <label>部门<select id="memberDept"><option value="">全部部门</option>
        ${departments.map(dept=>`<option value="${esc(dept)}">${esc(dept)}</option>`).join('')}</select></label>
    </div>
    <div class="permission-roster-scroll"><table class="catalog-table permission-members-table"><thead>
      <tr><th>成员</th><th>账号来源</th><th>部门 / 岗位</th><th>平台角色</th><th>状态</th><th>操作</th></tr>
      </thead><tbody id="memberRows"></tbody></table></div>
    <div class="permission-member-footer"><span id="memberCount"></span><div id="memberPaging" hidden>
      <button type="button" id="memberPrev" class="tree-tool-btn">上一页</button>
      <button type="button" id="memberNext" class="tree-tool-btn">下一页</button></div></div>
    <p id="bindingStatus" role="status" aria-live="polite"></p>`;
  const query = $('memberQuery'), source = $('memberSource'), department = $('memberDept');
  query.value = memberFilter.query;
  source.value = memberFilter.source;
  department.value = departments.includes(memberFilter.department) ? memberFilter.department : '';
  const pageSize = 25;
  let visible = [];

  function renderRows() {
    Object.assign(memberFilter, {query:query.value, source:source.value, department:department.value});
    const needle = query.value.trim().toLowerCase();
    const filtered = members.filter(row => (!source.value || source.value === row.source)
      && (!department.value || row.departments.includes(department.value)) && row.search.includes(needle));
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    memberFilter.page = Math.min(memberFilter.page, pages);
    visible = filtered.slice((memberFilter.page - 1)*pageSize, memberFilter.page*pageSize);
    $('memberRows').innerHTML = visible.map(({member:m, account:u, name, source, departments, job}, index) => {
      const role = u ? `<select aria-label="平台角色" data-role>${Object.entries(memberRoles).map(([k,v])=>
        `<option value="${k}" ${k === u.role ? 'selected' : ''}>${v}</option>`).join('')}</select>` : '—';
      const actions = [];
      if (u) actions.push(['reset','重置密码'], ['status',u.status === 'active' ? '停用账号' : '启用账号']);
      if (u?.feishu_id) actions.push(['unlink','解除飞书关联']);
      if (u) actions.push(['delete','移除平台账号']);
      const candidate = !u && m?.match_status === 'review' && m.local_user_id
        ? permState.users.find(user => user.id === m.local_user_id) : null;
      const extra = m && memberMatchLabels[m.match_status];
      const status = u ? (u.status === 'active' ? '已启用' : '已停用') : (m.active ? '在职' : '已离职');
      const phone = u?.phone || (m?.phone_tail ? '手机尾号 ' + m.phone_tail : '');
      return `<tr data-row="${index}"><td><strong>${esc(name)}</strong><small>${esc(phone)}</small></td>
        <td><span class="member-source" data-source="${source}">${memberSources[source]}</span></td>
        <td>${esc(departments.join('、') || '—')}<small>${esc(job)}</small></td><td>${role}</td>
        <td>${status}${extra && status !== extra ? `<small>${esc(extra)}</small>` : ''}${candidate ? `<small>候选账号 · ${esc(candidate.name)}</small>` : ''}</td>
        <td><div class="permission-roster-actions">${u ? '<button type="button" class="tree-tool-btn" data-action="edit">编辑资料</button>' : ''}
          ${candidate ? '<button type="button" class="tree-tool-btn" data-action="bind">确认关联</button>' : ''}
          ${!u && m?.active && m.match_status === 'no_account' ? '<button type="button" class="tree-tool-btn" data-action="create">开通账号</button>' : ''}
          ${actions.length ? `<select data-more aria-label="更多账号操作"><option value="">更多操作</option>${actions.map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select>` : ''}
        </div></td></tr>`;
    }).join('') || '<tr><td colspan="6" class="permission-roster-empty">没有符合条件的成员</td></tr>';
    $('memberCount').textContent = `共 ${filtered.length} 位成员` + (pages > 1 ? ` · 第 ${memberFilter.page} / ${pages} 页` : '');
    $('memberPaging').hidden = pages <= 1;
    $('memberPrev').disabled = memberFilter.page <= 1;
    $('memberNext').disabled = memberFilter.page >= pages;
  }

  async function act(control, action) {
    const {member, account} = visible[Number(control.closest('tr').dataset.row)];
    if (action === 'edit') return openEditAccountProfile(account.id);
    if (action === 'create') return openCreateUserModal(member);
    if (action === 'reset') return openResetPasswordModal(account.id);
    if (action === 'delete') return deleteUser(account.id);
    if (action === 'status') return toggleUserStatus(account.id);
    if (action === 'role') return changeUserRole(account.id, control.value);
    const unlink = action === 'unlink';
    if (!confirm(unlink ? '解除这个账号的飞书登录关联？' : '确认关联到该候选平台账号？')) return;
    control.disabled = true;
    try {
      await api('/api/system/feishu/bind', {method:'POST', body:{
        user_id:unlink ? account.id : member.local_user_id, open_id:unlink ? '' : member.open_id}});
      toast(unlink ? '关联已解除' : '关联已生效');
      await loadPermissionOverview();
    } catch (error) {
      $('bindingStatus').textContent = error.message;
      control.disabled = false;
    }
  }
  $('memberRows').onclick = event => {
    const control = event.target.closest('button[data-action]');
    if (control) act(control, control.dataset.action);
  };
  $('memberRows').onchange = event => {
    const control = event.target;
    if (control.matches('[data-role]')) return act(control, 'role');
    if (!control.matches('[data-more]') || !control.value) return;
    const action = control.value;
    control.value = '';
    act(control, action);
  };
  $('createMemberAccount').onclick = () => openCreateUserModal();
  if (connected) $('syncFeishu').onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    $('bindingStatus').textContent = '正在同步飞书通讯录…';
    try {
      const result = await api('/api/system/feishu/sync', {method:'POST', timeoutMs:180000});
      toast(`已同步 ${result.users} 位成员`);
      await loadPermissionOverview();
    } catch (error) {
      $('bindingStatus').textContent = error.message;
      button.disabled = false;
    }
  };
  query.oninput = source.onchange = department.onchange = () => {memberFilter.page = 1; renderRows();};
  $('memberPrev').onclick = () => {memberFilter.page--; renderRows();};
  $('memberNext').onclick = () => {memberFilter.page++; renderRows();};
  renderRows();
}



function openEditAccountProfile(userId) {
  const user = permState.users.find(member => member.id === userId);
  if (!user) return;
  const input = (id, label, type, value, length) =>
    `<label for="${id}">${label}<input id="${id}" type="${type}" value="${esc(value || '')}" maxlength="${length}"></label>`;
  const form = `<div class="account-profile-grid">
    ${input('profileName', '平台账号名称', 'text', user.name, 50)}
    ${input('profilePhone', '登录及匹配手机号', 'tel', user.phone === '--' ? '' : user.phone, 30)}
    ${input('profileDepartment', '自定义部门', 'text', user.department, 100)}
    ${input('profileJob', '自定义岗位', 'text', user.job_title, 80)}
  </div><p>请填与飞书通讯录一致的手机号。修改号码会解除当前关联，并要求重新登录。</p>`;
  operationDialog('编辑平台账号', form, '<button type="button" class="btn-archive-primary" id="saveAccountProfile">保存资料</button>');
  $('saveAccountProfile').onclick = async () => {
    const body = {name:$('profileName').value.trim(), phone:$('profilePhone').value.trim(),
      department:$('profileDepartment').value.trim(), job_title:$('profileJob').value.trim()};
    if (!body.name || !body.phone) {
      $('fileOperationStatus').textContent = '请填写姓名和手机号。';
      return;
    }
    setFileOperationBusy(true);
    try {
      const result = await api(`/api/system/permissions/users/${userId}/profile`, {method:'PUT', body});
      setFileOperationBusy(false);
      closeFileOperation();
      toast(result.message);
      await loadPermissionOverview();
    } catch (error) {
      setFileOperationBusy(false);
      $('fileOperationStatus').textContent = error.message;
    }
  };
}

async function saveApprovedDownload(fileId,ticket=''){
  const url='/api/file/'+encodeURIComponent(fileId)+'/download?'+new URLSearchParams({ticket});
  const token=sessionStorage.getItem('fileFinderAccessToken') || '';
  if(!token){location.assign(url);return;}
  const response=await fetch(url,{headers:{'X-App-Token':token}});
  if(!response.ok){const data=await response.json();throw Error(data.detail || '下载失败');}
  const blob=URL.createObjectURL(await response.blob());
  const link=document.createElement('a');link.href=blob;
  const encoded=response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''(.+)/i);
  link.download=encoded?decodeURIComponent(encoded[1]):'文件';
  link.click();setTimeout(()=>URL.revokeObjectURL(blob),60000);
}
document.addEventListener('click',event=>{
  const link=event.target.closest('a[download]');
  if(!link || !sessionStorage.getItem('fileFinderAccessToken'))return;
  const url=new URL(link.href,location.origin);
  const match=url.pathname.match(/^\/api\/file\/(\d+)\/download$/);
  if(match && url.origin===location.origin){
    event.preventDefault();saveApprovedDownload(match[1],url.searchParams.get('ticket')||'').catch(e=>toast(e.message));
  }
});
