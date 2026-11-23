/* Interactive presentation. All mutations live in memory; never call business APIs. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const { escapeHTML: esc, formatBytes, buildHTMLPreview, parseCSV } = window.ShowcaseDocuments;
  const demo = window.ShowcaseModel.createDemo();
  const scenes = [
    ['overview','总览','开始','◈','一页，走完文件的完整旅程。','直接操作下面的工作区。所有案例和操作均独立于正式系统。'],
    ['upload','上传文件','文件入库','↑','把文件放进来','选取或拖入自己的文件，随后就能搜索与预览。文件只留在当前浏览器页面。'],
    ['versions','更新与历史副本','文件入库','↺','更新内容，保留上一版','用一个新文件替换演示文件，旧内容保留为可打开的历史副本。'],
    ['ai','AI 辅助查找','发现与阅读','✧','用一句话，找到文件','这里演示提问、匹配依据与来源打开；使用本地文件名与标签检索，未连接大模型。'],
    ['search','文件搜索','发现与阅读','⌕','让文件更容易找到','按名称、标签和格式筛选，点击结果直接阅读。上传的文件也会出现在这里。'],
    ['catalog','目录浏览','发现与阅读','▤','按业务目录整理','切换目录查看文件，阅读器与搜索共享同一份演示数据。'],
    ['preview','PDF 与 HTML 阅读','发现与阅读','▧','文件直接在这里展开','多页 PDF、静态 HTML、表格与图片可以同时打开，标签随时切换和关闭。'],
    ['archive','归档合规','协作与管理','✓','从待办到完成','打开关联文件核对，然后完成一项归档。这里的状态不会提交到正式系统。'],
    ['approval','下载审批','协作与管理','⇄','每一次审批都有结果','查看模拟申请与关联文件，再选择批准或驳回。'],
    ['permissions','成员与权限','协作与管理','♙','明确谁可以做什么','切换演示成员的角色，查看相应能力说明；不修改真实账号与权限。'],
    ['sync','索引与同步','运行与恢复','⟳','让文件与索引保持一致','检查当前页面里的演示文件，查看按格式汇总的索引结果。'],
    ['backup','备份与恢复','运行与恢复','◷','变化之后，仍能回到之前','保存当前演示状态，继续操作后再恢复。快照仅存于本页面内存。'],
  ];
  const state = { scene: 'overview', query: '', type: '', folder: '', question: '', answers: [], versionFile: 'file-5', sync: null };
  const readers = new Map();
  const urls = new Set();
  let activeReader = null, generation = 0, uploading = false, toastTimer, pendingConfirm = null;
  let retainedUploadBytes = 0, uploadCount = 0;
  const ext = file => file.ext.replace(/^\./, '').toLowerCase();
  const badge = file => `<span class="file-icon ${esc(ext(file))}" aria-hidden="true">${esc(ext(file).toUpperCase() || 'FILE')}</span>`;
  const roles = {admin:'演示管理员',editor:'文件维护员',viewer:'只读成员'};
  const roleHelp = {admin:'管理文件、审批与角色配置',editor:'上传、更新与阅读文件',viewer:'查找与阅读文件'};
  const navButton = (key, text, cls='') => `<button type="button" class="${cls}" data-scene="${key}">${text}</button>`;

  function notify(text) { clearTimeout(toastTimer); $('demoToast').textContent=text; $('demoToast').hidden=false; toastTimer=setTimeout(() => { $('demoToast').hidden=true; },4000); }
  function confirmAction(title,text,action) { pendingConfirm=action; $('confirmDialog').returnValue=''; $('confirmTitle').textContent=title; $('confirmText').textContent=text; $('confirmDialog').showModal(); }
  $('confirmDialog').addEventListener('cancel', () => { pendingConfirm=null; });
  $('confirmDialog').querySelector('form').addEventListener('submit', event => {
    event.preventDefault();
    const confirmed=event.submitter?.value==='confirm';
    const action=pendingConfirm;pendingConfirm=null;
    $('confirmDialog').close(confirmed?'confirm':'cancel');
    if(confirmed){try{action?.();}catch(error){notify(error.message);}}
  });
  function renderNav() {
    let group='';
    $('sceneNav').innerHTML=scenes.map(([id,title,section,icon]) => {const head=section!==group ? `<div class="nav-group-title">${section}</div>` : ''; group=section; return `${head}<button type="button" class="scene-nav-button" data-scene="${id}" ${state.scene===id?'aria-current="page"':''}><span class="nav-icon" aria-hidden="true">${icon}</span>${title}</button>`;}).join('');
  }
  function showScene(id, updateURL=true) {
    const scene=scenes.find(item=>item[0]===id)||scenes[0];
    state.scene=scene[0]; activeReader=null;
    if(updateURL && location.hash!==`#${scene[0]}`) history.pushState(null,'',`#${scene[0]}`);
    $('sceneEyebrow').textContent=`${scene[2]} / 独立交互演示`;
    $('sceneTitle').textContent=scene[4]; $('sceneDescription').textContent=scene[5];
    $('nextScene').textContent=scene[0]==='backup'?'回到总览':'下一场景 →';
    renderNav(); renderScene(); renderTabs(); showReaderArea();
    window.scrollTo({top:0,behavior:'instant'});
  }
  function table(files,{actions=true}={}) {
    if(!files.length)return '<div class="empty">没有匹配的文件。试试其他关键词，或先上传一个文件。</div>';
    return `<div class="table-wrap"><table><thead><tr><th>文件名称</th><th>目录</th><th>大小</th>${actions?'<th>操作</th>':''}</tr></thead><tbody>${files.map(file=>`<tr><td><button class="file-name" data-open="${esc(file.id)}">${badge(file)}<span>${esc(file.name)}<small>${esc((file.tags||[]).join(' · '))}</small></span></button></td><td>${esc(file.folder)}</td><td>${formatBytes(file.size)}</td>${actions?`<td><button type="button" data-open="${esc(file.id)}">预览</button></td>`:''}</tr>`).join('')}</tbody></table></div>`;
  }
  function renderScene() {
    const files=demo.state.files, pending=demo.state.archiveTasks.filter(t=>t.status==='pending').length;
    const renderers={
      overview:()=>`<div class="hero-grid"><section class="start-panel"><span class="tag">无需登录 · 可直接操作</span><h2>从上传一份文件开始。</h2><p>文件进来以后，可以继续查找、打开、更新、归档。左侧所有场景互相连通，切换时保留当前演示状态。</p><div class="start-actions">${navButton('upload','上传一个文件','primary')}${navButton('preview','先看 PDF / HTML 案例')}</div></section><div class="journey">${[['upload','文件入库','上传、更新与历史副本'],['ai','发现与阅读','提问、检索与多文件预览'],['archive','协作与治理','归档、审批与恢复']].map(([key,title,desc])=>navButton(key,`<span><strong>${title}</strong><small>${desc}</small></span><span aria-hidden="true">↗</span>`)).join('')}</div></div><div class="stats"><div class="stat"><strong>${files.length}</strong><span>当前演示文件</span></div><div class="stat"><strong>${pending}</strong><span>待完成归档</span></div><div class="stat"><strong>${readers.size}</strong><span>已打开的阅读标签</span></div></div><section class="panel"><div class="panel-heading"><div><h2>可以直接打开的案例</h2><p>真实文档内容，点击即可阅读。</p></div>${navButton('search','查看全部')}</div>${table(files.slice(0,4))}</section>`,
      upload:()=>`<section class="panel"><div class="upload-grid"><div><div class="dropzone" id="dropzone"><h3>拖入文件，在这里继续操作</h3><p>支持 PDF、HTML、CSV、文本与常见图片预览</p><label>选择本地文件<input id="uploadInput" type="file" multiple aria-label="选择本地文件" ${uploading?'disabled':''}></label></div><div id="uploadFeedback" class="upload-feedback" role="status">${uploading?'正在读取本地文件…':'单文件不超过 20 MB；本次演示累计最多读取 50 份本地文件、100 MB（含历史版本与快照引用）。'}</div></div><div class="upload-help"><h3>文件不会离开这个页面</h3><p>选取后立即加入演示目录，可在文件搜索、AI 查找和阅读器中继续使用。</p><p>同名文件分别保留；如需保留历史版本，请使用“更新与历史副本”。</p><p>刷新、关闭或重置后，本次上传会清除。其他格式可以入库，但不提供内容预览。</p></div></div></section><section class="panel"><div class="panel-heading"><h2>当前文件</h2>${navButton('search','去搜索')}</div>${table(files)}</section>`,
      search:()=>`<section class="panel"><form id="searchForm" class="toolbar"><label class="search-label">文件名或标签<input id="searchQuery" type="search" value="${esc(state.query)}" placeholder="试试：结算、季度、演示案例"></label><label>文件格式<select id="typeFilter"><option value="">全部格式</option>${[...new Set(files.map(f=>f.ext))].sort().map(type=>`<option value="${esc(type)}" ${state.type===type?'selected':''}>${esc(type.toUpperCase())}</option>`).join('')}</select></label><button class="primary">搜索</button><button type="button" data-action="clear-search">清空</button></form><p class="muted" id="resultCount"></p><div id="searchResults"></div></section>`,
      catalog:()=>`<section class="panel"><div class="folder-picks"><button data-folder="" aria-pressed="${!state.folder}">全部目录</button>${[...new Set(files.map(f=>f.folder))].map(folder=>`<button data-folder="${esc(folder)}" aria-pressed="${state.folder===folder}">${esc(folder)}</button>`).join('')}</div>${table(demo.list({folder:state.folder}))}</section>`,
      preview:()=>`<div class="notice">先打开一份 PDF，再打开 HTML；上方的文件标签会保留阅读状态。PDF 使用浏览器阅读器，HTML 仅展示静态内容。</div><section class="panel"><div class="panel-heading"><h2>文档阅读案例</h2>${navButton('upload','上传自己的文件')}</div><div class="document-picks">${files.filter(f=>['pdf','html','htm','csv'].includes(ext(f))).slice(0,8).map(file=>`<button class="document-pick" data-open="${esc(file.id)}">${badge(file)}<span><strong>${esc(file.name)}</strong><small>${esc(file.folder)} · ${formatBytes(file.size)}</small><small>打开阅读器 ↗</small></span></button>`).join('')}</div></section>`,
      ai:()=>`<div class="split"><section class="panel"><div class="panel-heading"><div><h2>告诉我，你想找什么文件</h2><p>演示检索助手 · 本地匹配，不生成模型回答</p></div><span class="tag">演示</span></div><form id="aiForm"><label class="field-label" for="aiQuestion">文件问题</label><textarea id="aiQuestion" maxlength="300" placeholder="帮我找供应商结算文件">${esc(state.question)}</textarea><div class="actions-end"><button class="primary" type="submit">查找文件</button></div></form><div class="suggestions">${['帮我找供应商结算文件','我想看季度经营报告','找归档清单'].map(q=>`<button data-question="${q}">${q}</button>`).join('')}</div><div id="aiAnswers">${state.answers.map(answer=>`<article class="chat-message"><h3>${esc(answer.question)}</h3><p>${esc(answer.explanation)}</p><div class="chat-results">${answer.ids.map(id=>demo.get(id)).filter(Boolean).map(f=>`<button data-open="${esc(f.id)}">${esc(f.name)}<small>来源：${esc(f.folder)} · 点击核对原文</small></button>`).join('')||'<p>未找到匹配文件。换一个文件名关键词，或先上传文件。</p>'}</div></article>`).join('')}</div></section><aside class="panel"><h2>这一步可以讲什么</h2><ul class="detail-list"><li>自然语言输入<small>围绕文件名、目录和标签寻找资料。</small></li><li>有来源的结果<small>点击匹配文件，直接在阅读标签里核对。</small></li><li>上下文继续保留<small>看完文件返回这里，问题与结果仍在。</small></li><li>真实系统的模型能力<small>完整系统配置模型服务后可启用增强能力；此处不发送请求到外部模型。</small></li></ul></aside></div>`,
      versions:()=>{const file=demo.get(state.versionFile)||files[0];state.versionFile=file.id;return `<section class="panel"><label class="field-label" for="versionFile">选择要更新的演示文件</label><select id="versionFile">${files.map(f=>`<option value="${esc(f.id)}" ${f.id===file.id?'selected':''}>${esc(f.name)}</option>`).join('')}</select><div class="version-item"><div><strong>当前版本：${esc(file.name)}</strong><small>${formatBytes(file.size)} · ${esc(file.modified.slice(0,10))}</small></div><button data-open="${esc(file.id)}">打开当前版本</button></div><div class="dropzone"><h3>选择替换内容，保留上一版</h3><p>更新后文件名称与内容采用新文件，旧版本继续可读。</p><label>选择新版本<input type="file" id="replaceInput" aria-label="选择新版本" ${uploading?'disabled':''}></label></div><div id="uploadFeedback" class="upload-feedback" role="status"></div><div class="panel-heading" style="margin-top:24px"><h2>历史副本 · ${file.versions.length}</h2></div>${file.versions.map((version,i)=>`<div class="version-item"><div><strong>${esc(version.name)}</strong><small>版本 ${i+1} · ${formatBytes(version.size)}</small></div><button data-version="${i}" data-file="${esc(file.id)}">阅读历史副本</button></div>`).join('')||'<p class="muted">还没有历史副本。选择新版本后，当前内容会保留在这里。</p>'}</section>`;},
      archive:()=>`<section class="panel"><div class="panel-heading"><h2>归档任务</h2><span class="tag">${pending} 项待处理</span></div><div class="table-wrap"><table><thead><tr><th>任务与负责人</th><th>目录</th><th>状态</th><th>操作</th></tr></thead><tbody>${demo.state.archiveTasks.map(t=>`<tr><td>${esc(t.title)}<small>${esc(t.owner)} · ${esc(t.due)}</small></td><td>${esc(t.folder)}</td><td><span class="tag ${t.status==='pending'?'pending':''}">${t.status==='pending'?'待归档':'已完成'}</span></td><td><div class="row-actions"><button data-open="${esc(t.fileId)}">核对文件</button><button data-complete="${esc(t.id)}" ${t.status!=='pending'?'disabled':''}>完成归档</button></div></td></tr>`).join('')}</tbody></table></div></section>`,
      approval:()=>`<section class="panel"><div class="panel-heading"><h2>下载申请</h2><span class="muted">模拟审批，不触发真实下载授权</span></div><div class="table-wrap"><table><thead><tr><th>申请人 / 文件</th><th>用途</th><th>状态</th><th>处理</th></tr></thead><tbody>${demo.state.approvals.map(a=>`<tr><td>${esc(a.applicant)}<small><button class="file-name" data-open="${esc(a.fileId)}">${esc(a.fileName)}</button></small></td><td>${esc(a.reason)}</td><td><span class="tag ${a.status==='pending'?'pending':a.status==='rejected'?'danger':''}">${({pending:'待审批',approved:'已批准',rejected:'已驳回'})[a.status]}</span></td><td><div class="row-actions"><button data-approval="${esc(a.id)}" data-decision="approved" ${a.status!=='pending'?'disabled':''}>批准</button><button data-approval="${esc(a.id)}" data-decision="rejected" ${a.status!=='pending'?'disabled':''}>驳回</button></div></td></tr>`).join('')}</tbody></table></div></section>`,
      permissions:()=>`<div class="notice">这里只演示角色配置及能力说明。切换成员角色不会改变你的演示权限，也不会影响真实账号。</div><section class="panel"><div class="table-wrap"><table><thead><tr><th>演示成员</th><th>部门</th><th>角色</th><th>对应能力</th></tr></thead><tbody>${demo.state.members.map(m=>`<tr><td>${esc(m.name)}</td><td>${esc(m.department)}</td><td><select aria-label="${esc(m.name)}的演示角色" data-role="${esc(m.id)}">${Object.entries(roles).map(([key,label])=>`<option value="${key}" ${key===m.role?'selected':''}>${label}</option>`).join('')}</select></td><td>${roleHelp[m.role]}</td></tr>`).join('')}</tbody></table></div></section>`,
      sync:()=>`<section class="panel"><div class="panel-heading"><div><h2>本地演示索引</h2><p>仅检查当前页面的演示文件，不扫描服务器或共享盘。</p></div><button class="primary" data-action="sync">检查演示索引</button></div><div class="stats"><div class="stat"><strong>${files.length}</strong><span>已加入索引</span></div><div class="stat"><strong>${new Set(files.map(f=>f.ext)).size}</strong><span>文件格式</span></div><div class="stat"><strong>${new Set(files.map(f=>f.folder)).size}</strong><span>演示目录</span></div></div>${state.sync?`<div class="notice">${esc(state.sync)}</div>`:'<p class="muted">上传与更新会立即反映到演示索引中。点击检查查看当前结果。</p>'}<ul class="detail-list">${[...new Set(files.map(f=>f.ext))].map(type=>`<li>${esc(type.toUpperCase())}<small>${files.filter(f=>f.ext===type).length} 份文件</small></li>`).join('')}</ul></section>`,
      backup:()=>`<section class="panel"><div class="panel-heading"><div><h2>演示状态快照</h2><p>保存文件列表、版本、归档、审批与成员角色。最多保留 10 个快照；刷新页面后失效。</p></div><button class="primary" data-action="backup">保存快照</button></div>${demo.state.backups.map(b=>`<div class="version-item"><div><strong>${esc(b.name)}</strong><small>${b.fileCount} 份文件 · ${esc(b.createdAt.slice(0,19).replace('T',' '))}</small></div><button data-restore="${esc(b.id)}">恢复这个快照</button></div>`).join('')||'<div class="empty">还没有快照。先保存一次，再去上传文件或处理待办，随后回来恢复。</div>'}</section>`,
    };
    $('sceneContent').innerHTML=renderers[state.scene]();
    if(state.scene==='search')renderSearch();
    bindForms();
  }
  function renderSearch() {const files=demo.list({query:state.query,type:state.type});$('resultCount').textContent=`找到 ${files.length} 份文件`;$('searchResults').innerHTML=table(files);}
  function renderTabs() {
    $('readerTabs').innerHTML=[...readers.entries()].map(([key,r])=>`<div class="file-tab ${activeReader===key?'active':''}"><button data-reader="${esc(key)}" ${activeReader===key?'aria-current="page"':''} title="${esc(r.name)}">${esc(r.name)}</button><button data-close="${esc(key)}" aria-label="关闭 ${esc(r.name)}">×</button></div>`).join('');
  }
  function showReaderArea() {$('sceneContent').hidden=Boolean(activeReader);$('readerArea').hidden=!activeReader;readers.forEach((r,key)=>{r.element.hidden=key!==activeReader;});if(activeReader){const r=readers.get(activeReader);$('readerTitle').textContent=r.name;$('readerType').textContent=r.description;}}
  async function openFile(file, key=file?.id) {
    if(!file){notify('该文件已不存在，请返回场景重新选择。');return;}
    if(!readers.has(key)){
      const element=document.createElement('div');
      const r={name:file.name,description:`${ext(file).toUpperCase()} · 独立演示文件`,element};readers.set(key,r);$('readerBody').append(element);
      element.innerHTML='<p class="empty">正在准备预览…</p>';
      activeReader=key;renderTabs();showReaderArea();window.scrollTo({top:0,behavior:'instant'});
      // Only seeded asset paths or local object URLs may be read. No business API.
      const safeURL=file.url && (/^\/static\/demo-documents\/[a-z.-]+$/.test(file.url)||urls.has(file.url)) ? file.url : null;
      try {
        if(['html','htm'].includes(ext(file))){
          const content=file.content ?? (safeURL?await fetch(safeURL).then(res=>{if(!res.ok)throw Error('案例读取失败');return res.text();}):'');
          if(readers.get(key)!==r)return;
          const frame=document.createElement('iframe');frame.title=file.name;frame.setAttribute('sandbox','');frame.referrerPolicy='no-referrer';frame.srcdoc=buildHTMLPreview(content,{title:file.name});element.replaceChildren(frame);
        }else if(ext(file)==='pdf'&&safeURL){
          const fallback=document.createElement('div');fallback.className='reader-fallback';fallback.textContent='可使用 PDF 阅读器工具栏翻页、缩放。若当前浏览器未显示正文：';
          const link=document.createElement('a');link.href=safeURL;link.download=file.name;link.textContent='下载 PDF 查看';fallback.append(link);
          const frame=document.createElement('iframe');frame.title=file.name;frame.src=safeURL;frame.referrerPolicy='no-referrer';element.replaceChildren(fallback,frame);
        }else if(['png','jpg','jpeg','gif','webp'].includes(ext(file))&&safeURL){const image=document.createElement('img');image.alt=file.name;image.src=safeURL;element.replaceChildren(image);
        }else if(ext(file)==='csv'&&file.content!==undefined){
          const {rows,truncated}=parseCSV(file.content);
          element.innerHTML=`<div class="reader-fallback">${truncated?'预览前 200 行、50 列；文件内容保持完整。':'CSV 表格 · 仅展示文件原始值'}</div><div class="table-wrap"><table>${rows.map((row,index)=>`<tr>${row.map(cell=>index===0?`<th scope="col">${esc(cell)}</th>`:`<td>${esc(cell)}</td>`).join('')}</tr>`).join('')}</table></div>`;
        }else if(file.content!==undefined){const pre=document.createElement('pre');pre.textContent=file.content;element.replaceChildren(pre);
        }else{element.innerHTML='<div class="empty">文件已加入演示索引。此格式暂不支持内容预览，可上传 PDF、HTML、CSV、文本或常见图片。</div>';}
      }catch(error){element.replaceChildren();const message=document.createElement('p');message.className='empty demo-error';message.textContent=`预览失败：${error.message}。关闭此标签后可重新打开。`;element.append(message);}
      return; // Async completion updates content, never changes the selected scene.
    }
    activeReader=key;renderTabs();showReaderArea();
  }
  function closeReader(key){const r=readers.get(key);if(!r)return;r.element.remove();readers.delete(key);if(activeReader===key)activeReader=[...readers.keys()].at(-1)||null;renderTabs();showReaderArea();}
  function clearReaders(){readers.forEach(r=>r.element.remove());readers.clear();activeReader=null;renderTabs();showReaderArea();}
  function ask(question){state.question=question.trim().slice(0,300);if(!state.question){notify('先输入想找的文件。');return;}const result=demo.findForQuestion(state.question);state.answers.unshift({question:state.question,explanation:result.explanation,ids:result.files.map(f=>f.id)});state.answers=state.answers.slice(0,8);renderScene();}
  async function upload(selected, replaceId=null){
    if(uploading)return;uploading=true;const version=generation;const feedback=$('uploadFeedback');if(feedback)feedback.textContent='正在读取本地文件…';
    const messages=[];let success=0;const batch=Array.from(selected);
    for(const file of batch){
      if(generation!==version)break;
      try{
        if(file.size>20*1024*1024)throw Error('超过单文件 20 MB 限制');
        if(uploadCount>=50)throw Error('已达到演示文件数量上限');
        // Include blobs retained by historical versions and restored snapshots.
        if(retainedUploadBytes+file.size>100*1024*1024)throw Error('演示文件总大小超过 100 MB');
        const suffix=file.name.split('.').pop().toLowerCase();let content;
        if(suffix==='pdf'){const signature=await file.slice(0,5).text();if(signature!=='%PDF-')throw Error('不是有效的 PDF 文件');}
        if(['html','htm','csv','txt','md','json'].includes(suffix)){
          if(file.size>2*1024*1024)throw Error('文本预览文件不得超过 2 MB');content=await file.text();
        }
        if(generation!==version)break;
        const blob=new Blob([file],{type:suffix==='pdf'?'application/pdf':file.type||'application/octet-stream'});const url=URL.createObjectURL(blob);urls.add(url);
        const input={name:file.name,size:file.size,type:blob.type,url,...(content===undefined?{}:{content})};
        try{if(replaceId){demo.replaceFile(replaceId,input);closeReader(replaceId);}else demo.addFile(input);}catch(error){URL.revokeObjectURL(url);urls.delete(url);throw error;}
        retainedUploadBytes+=file.size;uploadCount++;success++;
      }catch(error){messages.push(`${file.name}：${error.message}`);}
    }
    if(generation!==version)return;
    uploading=false;renderScene();
    const result=[success?`已${replaceId?'更新':'加入'} ${success} 份文件，可以直接搜索和预览。`:'',...messages].filter(Boolean).join('\n');
    const target=$('uploadFeedback');if(target){target.textContent=result;target.classList.toggle('error',messages.length>0);}notify(success?`已${replaceId?'更新':'加入'} ${success} 份演示文件${messages.length?`，${messages.length} 份未加入`:''}`:messages[0]||'未选择文件');
  }
  function bindForms(){
    $('searchForm')?.addEventListener('submit',event=>{event.preventDefault();state.query=$('searchQuery').value;state.type=$('typeFilter').value;renderSearch();});
    $('searchQuery')?.addEventListener('input',event=>{state.query=event.target.value;renderSearch();});
    $('typeFilter')?.addEventListener('change',event=>{state.type=event.target.value;renderSearch();});
    $('aiQuestion')?.addEventListener('input',event=>{state.question=event.target.value;});
    $('aiForm')?.addEventListener('submit',event=>{event.preventDefault();ask($('aiQuestion').value);});
    $('uploadInput')?.addEventListener('change',event=>upload(event.target.files));
    $('replaceInput')?.addEventListener('change',event=>upload(event.target.files,state.versionFile));
    $('versionFile')?.addEventListener('change',event=>{state.versionFile=event.target.value;renderScene();});
    const zone=$('dropzone');if(zone){zone.addEventListener('dragover',event=>{event.preventDefault();zone.classList.add('dragging');});zone.addEventListener('dragleave',()=>zone.classList.remove('dragging'));zone.addEventListener('drop',event=>{event.preventDefault();zone.classList.remove('dragging');upload(event.dataTransfer.files);});}
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button)return;
    const d=button.dataset;
    try{
      if(d.scene){showScene(d.scene);return;}
      if(d.open){openFile(demo.get(d.open));return;}
      if(d.reader){activeReader=d.reader;renderTabs();showReaderArea();return;}
      if(d.close){closeReader(d.close);return;}
      if(d.version!==undefined){const file=demo.get(d.file);openFile(file.versions[Number(d.version)],`${file.id}:version:${d.version}`);return;}
      if(d.folder!==undefined){state.folder=d.folder;renderScene();return;}
      if(d.question){ask(d.question);return;}
      if(d.complete){demo.completeArchive(d.complete);renderScene();notify('这项演示归档已完成。');return;}
      if(d.approval){demo.decideApproval(d.approval,d.decision);renderScene();notify(d.decision==='approved'?'演示申请已批准。':'演示申请已驳回。');return;}
      if(d.restore){confirmAction('恢复这个演示快照？','当前演示文件、待办、审批与角色将恢复到保存时的状态。正式系统不受影响。',()=>{generation++;uploading=false;demo.restoreBackup(d.restore);state.answers=[];state.sync=null;clearReaders();renderScene();notify('已恢复演示快照。');});return;}
      if(d.action==='clear-search'){state.query='';state.type='';renderScene();}
      if(d.action==='backup'){if(demo.state.backups.length>=10){notify('最多保留 10 个演示快照，请重置后再试。');return;}demo.createBackup();renderScene();notify('当前演示状态已保存。');}
      if(d.action==='sync'){state.sync=`检查完成：当前 ${demo.state.files.length} 份演示文件均可检索。未访问服务器目录。`;renderScene();notify('演示索引检查完成。');}
    }catch(error){notify(error.message);}
  });
  document.addEventListener('change',event=>{if(event.target.dataset.role){try{demo.setMemberRole(event.target.dataset.role,event.target.value);notify('演示角色已更新。');}catch(error){notify(error.message);}renderScene();}});
  $('backToScene').addEventListener('click',()=>{activeReader=null;renderScene();renderTabs();showReaderArea();});
  $('nextScene').addEventListener('click',()=>showScene(scenes[(scenes.findIndex(s=>s[0]===state.scene)+1)%scenes.length][0]));
  $('resetDemo').addEventListener('click',()=>confirmAction('重置整个演示？','本次上传、已打开文件、待办变更和快照将被清除，恢复初始案例。',()=>{generation++;uploading=false;clearReaders();urls.forEach(url=>URL.revokeObjectURL(url));urls.clear();retainedUploadBytes=0;uploadCount=0;demo.reset();Object.assign(state,{query:'',type:'',folder:'',question:'',answers:[],versionFile:'file-5',sync:null});showScene('overview');notify('演示已重置，可以重新开始。');}));
  window.addEventListener('hashchange',()=>{const key=location.hash.slice(1);if(scenes.some(s=>s[0]===key))showScene(key,false);});
  window.addEventListener('pagehide',event=>{if(!event.persisted)urls.forEach(url=>URL.revokeObjectURL(url));});
  showScene(location.hash.slice(1)||'overview',false);
})();
