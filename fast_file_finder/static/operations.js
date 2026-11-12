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
    panel.innerHTML='<h3>飞书企业应用</h3>';
    if(!status.configured){
      panel.insertAdjacentHTML('beforeend','<p>尚未配置企业应用。请在服务环境中设置应用 ID、密钥与登录回调地址，再同步通讯录。</p>');
      return;
    }
    panel.insertAdjacentHTML('beforeend',`<p>最近同步 ${esc(portalMonitorTime(status.last_sync))} · ${status.members.length} 名成员</p><button id="syncFeishu" class="tree-tool-btn">同步通讯录</button><div class="grant-toolbar"><label>本地账号 <select id="localFeishuUser">${permState.users.map(u=>`<option value="${u.id}">${esc(u.name)}</option>`).join('')}</select></label><label>飞书成员 <select id="remoteFeishuUser"><option value="">解除绑定</option>${status.members.filter(m=>m.active).map(m=>`<option value="${esc(m.open_id)}">${esc(m.name)} · ${esc(m.department)}</option>`).join('')}</select></label><button id="bindFeishu" class="tree-tool-btn">保存绑定</button></div><p id="bindingStatus"></p>`);
    const showBinding=()=>{$('remoteFeishuUser').value=permState.users.find(u=>String(u.id)===$('localFeishuUser').value)?.feishu_id || '';};
    $('localFeishuUser').onchange=showBinding;showBinding();
    $('syncFeishu').onclick=async()=>{const b=$('syncFeishu');b.disabled=true;try{await api('/api/system/feishu/sync',{method:'POST',timeoutMs:180000});await loadFeishuPanel();}catch(e){$('bindingStatus').textContent=e.message;b.disabled=false;}};
    $('bindFeishu').onclick=async()=>{try{await api('/api/system/feishu/bind',{method:'POST',body:{user_id:Number($('localFeishuUser').value),open_id:$('remoteFeishuUser').value}});toast('绑定已更新，该成员下次访问时需要重新登录');await loadPermissionOverview();}catch(e){$('bindingStatus').textContent=e.message;}};
  }catch(e){panel.textContent=e.message;}
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
