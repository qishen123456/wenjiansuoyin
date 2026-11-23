/* Run in a fresh, named agent-browser session on /showcase, via eval --stdin.
 * Exercises only synthetic browser-local state. Resets after success.
 */
(async () => {
  const checks=[];
  const assert=(condition,message)=>{if(!condition)throw Error(message);checks.push(message);};
  const click=selector=>{const node=document.querySelector(selector);if(!node)throw Error(`Missing ${selector}`);node.click();};
  const nav=scene=>click(`#sceneNav [data-scene="${scene}"]`);
  const tick=()=>new Promise(resolve=>setTimeout(resolve,20));
  const wait=async condition=>{for(let i=0;i<150;i++){if(condition())return;await tick();}throw Error('Timed out waiting for demo UI');};
  const upload=async (files,selector='#uploadInput')=>{const dt=new DataTransfer();files.forEach(file=>dt.items.add(file));const input=document.querySelector(selector);input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));await wait(()=>!document.querySelector(selector)?.disabled && !document.querySelector('#uploadFeedback')?.textContent.includes('正在读取'));};
  const text=()=>document.getElementById('sceneContent').textContent;
  const errors=[];window.addEventListener('error',event=>errors.push(event.message));
  // Local upload, duplicate names, escaped names, and invalid PDF handling.
  nav('upload');
  await upload([new File(['alpha report'],'local-proof.txt',{type:'text/plain'}),new File(['beta report'],'local-proof.txt',{type:'text/plain'}),new File(['not a pdf'],'invalid.pdf',{type:'application/pdf'})]);
  assert(text().includes('不是有效的 PDF'),'invalid PDF is rejected with visible feedback');
  nav('search');const q=document.getElementById('searchQuery');q.value='local-proof';q.dispatchEvent(new Event('input',{bubbles:true}));
  assert(document.querySelectorAll('#searchResults tbody tr').length===2,'same-name uploads stay separate and searchable');
  click('#searchResults [data-open]');assert(document.getElementById('readerBody').textContent.includes('report'),'uploaded text is readable');
  const firstKey=document.querySelector('[data-reader]').dataset.reader;
  nav('preview');click('[data-open="file-1"]');
  nav('preview');click('[data-open="file-2"]');
  nav('preview');click('[data-open="file-3"]');await wait(()=>Boolean(document.querySelector('#readerBody iframe[sandbox]')));
  assert(document.querySelectorAll('.file-tab').length===4,'multiple document tabs coexist');
  const htmlFrame=document.querySelector('#readerBody iframe[sandbox]');
  assert(htmlFrame.getAttribute('sandbox')===''&&htmlFrame.srcdoc.includes('季度经营简报'),'HTML preview is sandboxed with actual content');
  click(`[data-reader="${firstKey}"]`);assert(document.getElementById('readerTitle').textContent==='local-proof.txt','switching tabs restores the selected document');
  click('[data-close="file-2"]');assert(document.getElementById('readerTitle').textContent==='local-proof.txt','closing background tab preserves active document');
  // Local assistant uses uploaded and seeded files as clickable evidence.
  nav('ai');click('[data-question="帮我找供应商结算文件"]');
  assert(document.querySelectorAll('.chat-results [data-open]').length===2,'assistant returns matching source files');
  click('.chat-results [data-open]');click('#backToScene');assert(text().includes('帮我找供应商结算文件'),'question is retained after source reading');
  // Versions retain prior content.
  nav('versions');await upload([new File(['new version content'],'updated.txt',{type:'text/plain'})],'#replaceInput');
  assert(text().includes('updated.txt')&&text().includes('历史副本 · 2'),'replacement retains previous versions');
  click('[data-version="1"]');assert(document.getElementById('readerBody').textContent.includes('300000'),'previous file content remains readable');
  // Backup, workflow changes, roles, and recovery share one state.
  nav('backup');click('[data-action="backup"]');
  nav('archive');click('[data-complete="archive-1"]');assert(document.querySelector('[data-complete="archive-1"]').disabled,'archive completion persists');
  nav('approval');click('[data-approval="approval-1"][data-decision="approved"]');assert(text().includes('已批准'),'approval changes visible status');
  nav('permissions');let role=document.querySelector('[data-role="member-1"]');role.value='viewer';role.dispatchEvent(new Event('change',{bubbles:true}));
  assert(document.querySelector('[data-role="member-1"]').value==='admin'&&document.getElementById('demoToast').textContent.includes('至少保留'),'rejected last-admin demotion restores control with feedback');
  role=document.querySelector('[data-role="member-3"]');role.value='editor';role.dispatchEvent(new Event('change',{bubbles:true}));assert(text().includes('上传、更新与阅读文件'),'accepted role updates capability description');
  nav('sync');click('[data-action="sync"]');assert(text().includes('检查完成'),'index check reports current demo files');
  nav('backup');click('[data-restore]');click('#confirmDialog button[value="confirm"]');await tick();
  nav('archive');assert(!document.querySelector('[data-complete="archive-1"]').disabled,'snapshot restores earlier task state');
  // Feishu scenes share the simulated identity, directory, grants, and task history.
  const form=id=>document.getElementById(id);
  const select=(field,value)=>{field.value=value;field.dispatchEvent(new Event('change',{bubbles:true}));};
  nav('feishu-login');
  form('feishuLoginForm').requestSubmit();
  assert(text().includes('许言')&&text().includes('只读成员')&&document.getElementById('demoToast').textContent.includes('已开通'),'first Feishu demo authorization opens a synthetic ordinary account');
  click('#sceneContent [data-scene="feishu-members"]');
  click('[data-feishu-action="sync"]');
  assert(text().includes('4 位成员')&&text().includes('交付专员'),'simulated contact sync exposes members and jobs');
  const grant=form('feishuGrantForm');
  select(grant.elements.type,'job');select(grant.elements.subject,'交付支持 / 交付专员');
  select(grant.elements.folder,'归档材料');select(grant.elements.access,'write');grant.requestSubmit();
  assert([...document.querySelectorAll('#feishuAccessList li')].some(row=>row.textContent.includes('归档材料阅读与维护')),'the signed-in member receives the matching job grant');
  select(document.getElementById('feishuAccessPerson'),'feishu-finance');
  assert([...document.querySelectorAll('#feishuAccessList li')].some(row=>row.textContent.includes('归档材料未授权')),'different departments cannot use this job grant');
  nav('feishu-push');
  const task=form('feishuTaskForm');task.elements.name.value='浏览器验收日报';task.elements.recipient.value='feishu-finance';task.elements.time.value='14:10';task.requestSubmit();
  const row='[data-task-row="notify-2"]';
  assert(document.querySelector(row).textContent.includes('林晓')&&document.querySelector(row).textContent.includes('14:10'),'daily task saves a synchronized recipient and schedule');
  click(`${row} [data-feishu-action="toggle"]`);assert(document.querySelector(row).textContent.includes('已暂停'),'pause updates the delivery plan');
  click(`${row} [data-feishu-action="edit"]`);form('feishuTaskForm').elements.time.value='15:20';form('feishuTaskForm').requestSubmit();
  assert(document.querySelector(row).textContent.includes('15:20'),'existing task schedule can be edited');
  click(`${row} [data-feishu-action="send"]`);
  assert(document.querySelector('#confirmDialog').open&&!document.querySelector(row).textContent.includes('模拟投递'),'manual send waits for explicit confirmation');
  click('#confirmDialog button[value="confirm"]');
  assert(document.querySelector(row).textContent.includes('模拟投递'),'confirmed send leaves a local receipt');
  click(`${row} [data-feishu-action="delete"]`);click('#confirmDialog button[value="confirm"]');
  assert(!document.querySelector(row),'deleting a simulated task only removes the chosen row');
  click('#sceneContent [data-scene="feishu-reports"]');
  click('[data-report-status="missing"]');
  assert(document.querySelectorAll('#sceneContent tbody tr').length===1&&text().includes('项目交付凭证'),'report status filters records for the selected date');
  select(document.getElementById('feishuReportDay'),'2026-11-12');
  assert(document.querySelectorAll('#sceneContent tbody tr').length===1&&text().includes('季度经营报告'),'changing report date keeps the status filter and updates records');
  const originalCreateURL=URL.createObjectURL;let exported;
  try {
    URL.createObjectURL=blob=>{exported=blob;return originalCreateURL.call(URL,blob);};
    click('[data-feishu-action="export"]');
    const csv=await exported.text();
    assert(csv.includes('2026-11-12')&&csv.includes('季度经营报告')&&!csv.includes('项目交付凭证'),'export contains the selected date and only abnormal records');
  } finally { URL.createObjectURL=originalCreateURL; }
  nav('archive');
  // Escape after a prior confirmation must never reset current state.
  click('[data-complete="archive-1"]');click('#resetDemo');document.getElementById('confirmDialog').dispatchEvent(new Event('cancel'));document.getElementById('confirmDialog').close();await tick();
  assert(location.hash==='#archive'&&document.querySelector('[data-complete="archive-1"]').disabled,'canceling reset preserves current state');
  // Stale HTML loads cannot hijack navigation.
  const originalFetch=window.fetch;let release;
  window.fetch=()=>new Promise(resolve=>{release=()=>resolve(new Response('<h1>delayed preview</h1>'));});
  nav('preview');click('[data-open="file-4"]');nav('archive');release();await tick();window.fetch=originalFetch;
  assert(!document.getElementById('sceneContent').hidden&&location.hash==='#archive','stale HTML response does not change active scene');
  // The skip link must not be interpreted as a new scene.
  click('.skip-link');await tick();assert(document.querySelector('#sceneNav [aria-current]').dataset.scene==='archive','skip link preserves selected scene');
  // Untrusted HTML: remove navigation, scripts and network-bearing markup.
  nav('upload');
  const hostile='<meta http-equiv="refresh" content="0;url=/__demo_must_not_request"><base href="/__demo_must_not_request"><script>parent.__demoXSS=true</script><img src="/__demo_must_not_request" onerror="parent.__demoXSS=true"><iframe src="/__demo_must_not_request"></iframe><form action="/__demo_must_not_request"><button>submit</button></form><style>@import "/__demo_must_not_request"</style><div style="background:url(/__demo_must_not_request)">safe text</div><a href="/__demo_must_not_request">link</a>';
  await upload([new File([hostile],'hostile-preview.html',{type:'text/html'})]);
  click('#sceneContent [data-open]');await tick();
  const src=document.querySelector('#readerBody > div:not([hidden]) iframe').srcdoc;
  assert(!src.includes('__demo_must_not_request')&&!src.includes('__demoXSS')&&src.includes('safe text'),'hostile HTML resources and executable markup are removed');
  assert(!window.__demoXSS,'uploaded HTML cannot execute in host');
  const resources=performance.getEntriesByType('resource').map(entry=>entry.name);
  assert(resources.every(url=>!new URL(url).pathname.startsWith('/api/')),'demo never calls business APIs');
  assert(errors.length===0,`no uncaught browser errors (${errors.length})`);
  click('#resetDemo');click('#confirmDialog button[value="confirm"]');await tick();
  nav('feishu-login');assert(Boolean(form('feishuLoginForm')),'reset clears the simulated Feishu authorization');
  nav('search');document.getElementById('searchQuery').value='local-proof';document.getElementById('searchQuery').dispatchEvent(new Event('input',{bubbles:true}));
  assert(document.querySelectorAll('#searchResults tbody tr').length===0&&document.querySelectorAll('.file-tab').length===0,'reset clears uploaded files and readers');
  nav('overview');return {passed:checks.length,checks};
})()
