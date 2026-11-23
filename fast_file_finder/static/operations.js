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

fetch('/integrations/feishu/status').then(r=>r.json()).then(s=>{
  if(s.configured) $('feishuLoginLink').hidden=false;
}).catch(()=>{});

async function loadFeishuPanel(){
  const panel=$('feishuPanel');
  if(!panel || currentUser?.role!=='super_admin') return;
  try{
    const status=await api('/api/system/feishu');
    panel.innerHTML='<div class="home-panel-heading"><h3>飞书企业应用</h3><button type="button" id="refreshFeishu" class="tree-tool-btn">刷新状态</button></div>';
    $('refreshFeishu').onclick=loadFeishuPanel;
    const daily=status.daily;
    if(daily){
      const reportState=daily.chat_configured
        ? (daily.report_sent_at ? `已发送 · ${portalMonitorTime(daily.report_sent_at)}` : '今日尚未发送')
        : '未配置接收群';
      const tableState=daily.table_configured
        ? (daily.table_pending ? `${daily.table_pending} 条待写入` : '无待写入记录')
        : '未配置数据表';
      const errors=[daily.configuration_error, daily.report_error, daily.table_error].filter(Boolean);
      panel.insertAdjacentHTML('beforeend', `<section class="feishu-daily" aria-label="同步日报">
        <div class="feishu-daily-heading"><h4>同步日报</h4><span>${daily.enabled ? '已启用' : '未启用'}</span></div>
        ${daily.enabled ? `<dl><div><dt>发送时间</dt><dd>${esc(daily.schedule)}（北京时间）</dd></div><div><dt>日报状态</dt><dd>${esc(reportState)}</dd></div><div><dt>多维表清单</dt><dd>${esc(tableState)}</dd></div></dl>
        ${errors.map(msg=>`<p class="feishu-daily-error" role="status">${esc(msg)}</p>`).join('')}
        ${daily.preview ? `<details><summary>查看今日数据</summary><pre>${esc(daily.preview)}</pre></details>` : ''}` : '<p>在服务环境中配置接收群和多维表格，启用后按日期归档新增文件。</p>'}
      </section>`);
    }
    if(!status.configured){
      panel.insertAdjacentHTML('beforeend','<p>尚未配置企业应用。请在服务环境中设置应用 ID、密钥与登录回调地址，再同步通讯录。</p>');
      return;
    }
    const members = status.members;
    const matched = members.filter(member => member.match_status === 'matched').length;
    const noPhone = members.filter(member => member.active && member.match_status === 'no_phone').length;
    panel.insertAdjacentHTML('beforeend', `<section class="feishu-directory">
      <div class="home-panel-heading"><div><h4>通讯录匹配</h4>
        <p>最近同步 ${esc(portalMonitorTime(status.last_sync))} · ${members.length} 位飞书成员 · ${matched} 位已匹配</p></div>
        <button type="button" id="syncFeishu" class="tree-tool-btn">同步并匹配</button></div>
      <p>同一手机号只匹配一个启用账号。部门和岗位可在「文件操作权限」中配置授权。</p>
      ${noPhone ? `<p class="feishu-daily-error">${noPhone} 位在职成员未返回手机号。请核对飞书的手机号字段权限和通讯录范围。</p>` : ''}
      <label class="feishu-directory-search"><span>查找匹配结果</span><input id="feishuMemberSearch" type="search" placeholder="姓名、部门、岗位或手机号尾号"></label>
      <div id="feishuMatchRows" class="feishu-match-rows"></div><p id="bindingStatus" role="status" aria-live="polite"></p>
    </section>`);
    const labels = {matched:'手机号已匹配', no_phone:'手机号不可见', no_account:'平台无同号账号',
      conflict:'号码或关联冲突', disabled:'平台账号已停用', inactive:'飞书成员不可用',
      review:'等待确认', resync:'待重新同步'};
    const render = () => {
      const query = $('feishuMemberSearch').value.trim().toLowerCase();
      const rows = members.filter(member => {
        const local = permState.users.find(user => user.id === (member.local_user_id || member.bound_user_id));
        return [member.name, member.department, member.job_title, member.phone_tail, local?.name]
          .some(value => value?.toLowerCase().includes(query));
      });
      $('feishuMatchRows').innerHTML = rows.slice(0, 100).map(member => {
        const local = permState.users.find(user => user.id === (member.local_user_id || member.bound_user_id));
        const bound = permState.users.find(user => user.id === member.bound_user_id);
        const matchLabel = labels[member.match_status] || '待同步';
        const tag = bound && member.match_status !== 'matched' && status.demo_mode
          ? '演示关联 · ' + matchLabel : matchLabel;
        const accountLabel = local ? (bound && bound.id !== local.id
          ? '候选 ' + local.name + ' · 当前关联 ' + bound.name : local.name) : '';
        const action = member.match_status === 'review' && member.local_user_id
          ? `<button type="button" class="tree-tool-btn" data-confirm-feishu="${esc(member.open_id)}" data-user-id="${member.local_user_id}">确认关联</button>`
          : member.bound_user_id
            ? `<button type="button" class="tree-tool-btn" data-unlink-feishu="${member.bound_user_id}">解除关联</button>`
            : local ? `<button type="button" class="tree-tool-btn" data-edit-match-user="${local.id}">编辑资料</button>` : '';
        return `<article class="feishu-match-row"><div><strong>${esc(member.name)}</strong>
          <small>${esc(member.department || '部门未返回')} · ${esc(member.job_title || '岗位未返回')}</small></div>
          <div><span class="feishu-match-status ${member.match_status === 'matched' ? 'matched' : ''}">${esc(tag)}</span>
          <small>${esc(member.phone_tail ? '手机尾号 ' + member.phone_tail : '手机号不可见')}${accountLabel ? ' · ' + esc(accountLabel) : ''}</small></div>
          <div>${action}</div></article>`;
      }).join('') || '<p>没有符合条件的成员</p>';
      if (rows.length > 100) $('feishuMatchRows').insertAdjacentHTML('beforeend', '<p>显示前 100 条，请输入关键词缩小范围。</p>');
      $('feishuMatchRows').querySelectorAll('[data-confirm-feishu], [data-unlink-feishu]').forEach(button => {
        button.onclick = async () => {
          const unlink = Boolean(button.dataset.unlinkFeishu);
          const userId = Number(button.dataset.unlinkFeishu || button.dataset.userId);
          if (unlink && !confirm('解除这位成员的飞书登录关联？')) return;
          if (!unlink && !confirm('将该飞书身份关联到手机号相同的平台账号，并使用该账号权限？')) return;
          button.disabled = true;
          try {
            await api('/api/system/feishu/bind', {method:'POST', body:{user_id:userId, open_id:button.dataset.confirmFeishu || ''}});
            toast(unlink ? '关联已解除' : '关联已生效');
            await loadPermissionOverview();
          } catch (error) {
            $('bindingStatus').textContent = error.message;
            button.disabled = false;
          }
        };
      });
      $('feishuMatchRows').querySelectorAll('[data-edit-match-user]').forEach(button => {
        button.onclick = () => openEditAccountProfile(Number(button.dataset.editMatchUser));
      });
    };
    $('feishuMemberSearch').oninput = render;
    render();
    $('syncFeishu').onclick = async () => {
      const button = $('syncFeishu');
      button.disabled = true;
      $('bindingStatus').textContent = '正在读取授权范围并校验手机号…';
      try {
        const result = await api('/api/system/feishu/sync', {method:'POST', timeoutMs:180000});
        toast(`同步 ${result.users} 位成员，已匹配 ${result.matched} 位`);
        await loadPermissionOverview();
      } catch (error) {
        $('bindingStatus').textContent = error.message;
        button.disabled = false;
      }
    };
  }catch(e){panel.textContent=e.message;}
}

function openEditAccountProfile(userId) {
  const user = permState.users.find(member => member.id === userId);
  if (!user) return;
  const input = (id, label, type, value, length) =>
    `<label for="${id}">${label}<input id="${id}" type="${type}" value="${esc(value || '')}" maxlength="${length}"></label>`;
  const form = `<div class="account-profile-grid">
    ${input('profileName', '姓名', 'text', user.name, 50)}
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
