const portalProductSessionId = (() => {
  try {
    const existing = sessionStorage.getItem('portalProductSession');
    if (existing) return existing;
    const created = typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `session_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    sessionStorage.setItem('portalProductSession', created);
    return created;
  } catch { return `session_${Date.now()}_${Math.random().toString(36).slice(2)}`; }
})();
let portalSearchEventId = null;
/* UI for the reference redesign. Existing search, authentication, catalog,
 * downloads and archive dialogs remain owned by index.html. */
let portalAiEnabled = false;
let portalAiConfigured = false;
let portalLastStatus = null;
let portalLastHomeData = null;
let portalLastTasks = [];
let portalSyncTimer = null;
let portalFileCheckSequence = 0;
let portalArchivePage = 1;
let portalArchiveTotal = 0;
let portalKnownPending = null;

function portalText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

function portalOrganizationLabel(name) {
  return String(name || '').replace(/^\d{1,2}[-－]/,'').replace(/^([^-/]+)-/,'$1 · ');
}
function portalRelativeDirectory(path) {
  const root = catalogState.tree?.path || '';
  const relative = root && (path === root || path.startsWith(root + '/')) ? path.slice(root.length) : path;
  return relative.replace(/^\//,'').split('/').filter(Boolean).map(portalOrganizationLabel).join(' / ') || '根目录';
}
function renderPortalChildFolders(node, expanded = false) {
  const container = $('catalogChildFolders');
  if (!container) return;
  const children = (node?.children || []).slice().sort((a,b) => a.name.localeCompare(b.name,'zh-CN',{numeric:true}));
  container.classList.toggle('hidden', children.length === 0);
  const visible = expanded ? children : children.slice(0,4);
  container.innerHTML = visible.map((child, index) => `<button class="catalog-folder-card" data-folder-index="${index}" title="${esc(child.path)}"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><use href="#i-folder"/></svg><span>${esc(portalOrganizationLabel(child.name))}<small>${fmtNumber(child.file_count)} 份文件</small></span><b>›</b></button>`).join('') + (children.length > 4 ? `<button class="catalog-show-folders" id="catalogShowFolders">${expanded ? '收起下级目录' : `查看全部 ${children.length} 个下级目录`} <span>${expanded ? '⌃' : '⌄'}</span></button>` : '');
  container.querySelectorAll('[data-folder-index]').forEach(button => {
    button.onclick = () => {
      const child = visible[Number(button.dataset.folderIndex)];
      catalogState.expandedPaths.add(node.path);
      if (child.children?.length) catalogState.expandedPaths.add(child.path);
      selectTreeNode(child.path, child.name, child.file_count, child.today_added);
      renderCatalogTree();
    };
  });
  $('catalogShowFolders')?.addEventListener('click', () => renderPortalChildFolders(node,!expanded));
}

function highlightPortalQuery(value) {
  const tokens = currentQuery.trim().split(/\s+/).filter(Boolean).slice(0, 8);
  if (!tokens.length) return esc(value);
  const pattern = tokens.map(token => token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const exact = new Set(tokens.map(token => token.toLocaleLowerCase()));
  return String(value).split(new RegExp(`(${pattern})`, 'gi')).map(part =>
    exact.has(part.toLocaleLowerCase()) ? `<mark>${esc(part)}</mark>` : esc(part)
  ).join('');
}

function clearPortalSearchFilters() {
  resetPortalExtraFilters();
  selectedExt = '';
  dateFrom = '';
  dateTo = '';
  matchMode = 'all';
  searchTarget = 'title';
  $('dateFromInput').value = '';
  $('dateToInput').value = '';
  $('btnFilterAnd').classList.add('active');
  $('btnFilterOr').classList.remove('active');
  document.querySelectorAll('.popover-ext-btn').forEach(item => item.classList.toggle('active', !item.dataset.ext));
  refreshDateQuickTags('all');
  _updateDateHint('', '', 'all');
  syncQuickFilterStrip();
  updateFilterToggleState();
}

function togglePortalSidebar(force) {
  const open = force ?? !document.body.classList.contains('sidebar-open');
  document.body.classList.toggle('sidebar-open', open);
  $('sidebarBackdrop').classList.toggle('hidden', !open);
}

function updatePortalNavigation(mode) {
  $('syncView').classList.toggle('hidden', mode !== 'sync');
  $('navSyncTab').classList.toggle('active', mode === 'sync');
  document.querySelectorAll('.portal-navigation .nav-mode-btn').forEach(button => {
    button.setAttribute('aria-current', button.getAttribute('onclick').includes(`'${mode}'`) ? 'page' : 'false');
  });
  togglePortalSidebar(false);
  if (mode === 'sync') refreshPortalSync();
  if (mode === 'archive') renderPortalArchiveNotice();
}

function togglePortalAI() {
  if (!portalAiConfigured) {
    toast('尚未配置 AI 服务，当前使用本地索引检索');
    return;
  }
  portalAiEnabled = !portalAiEnabled;
  const button = $('portalAiToggle');
  button.setAttribute('aria-pressed', String(portalAiEnabled));
  button.textContent = `AI 理解 · ${portalAiEnabled ? '开' : '关'}`;
  if (currentNavMode === 'search' && $('searchView').classList.contains('has-results') && $('q').value.trim()) search(1);
}

function portalDate(value) {
  if (!value) return '尚未同步';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace('T', ' ');
  const now = new Date();
  const time = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  return date.toDateString() === now.toDateString() ? `今天 ${time}` : `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}

function updatePortalStatus(data) {
  const wasRunning = portalLastStatus?.running;
  portalLastStatus = data;
  portalAiConfigured = !!data.ai_configured;
  if (!portalAiConfigured) portalAiEnabled = false;
  $('portalAiToggle').setAttribute('aria-pressed', String(portalAiEnabled));
  $('portalAiToggle').textContent = `AI 理解 · ${portalAiEnabled ? '开' : '关'}`;
  $('portalAiToggle').title = portalAiConfigured ? '启用或关闭 AI 关键词扩展' : 'AI 服务尚未配置，使用本地索引';
  const warning = ['error', 'partial'].includes(data.phase);
  const state = data.phase === 'scoped' ? '已授权范围' : data.running ? '同步中' : warning ? '需检查' : !data.root_configured ? '未配置目录' : !data.last_scan_at ? '待建立索引' : '正常';
  portalText('portalSyncState', state);
  portalText('portalCatalogSync', state);
  portalText('kpiSyncTime', portalDate(data.last_scan_at));
  portalText('portalSyncDescription', data.running ? '正在同步文件索引，已有搜索结果仍然可用。' : `搜索索引最近更新：${portalDate(data.last_scan_at)}。`);
  portalText('lastScan', portalDate(data.last_scan_at));
  const interval = Number(data.auto_scan_interval_minutes || 0);
  portalText('portalSyncInterval', interval > 0 ? `每 ${interval} 分钟` : '未启用');
  portalText('portalJobInterval', interval > 0 ? `每 ${interval} 分钟` : '手动运行');
  portalText('portalJobState', data.running ? '运行中' : interval > 0 ? '已启用' : '未启用');
  portalText('portalHomeIndexStatus', `${data.running ? '索引同步中' : warning ? '索引扫描需检查' : data.last_scan_at ? '索引正常' : '索引待建立'} · ${portalDate(data.last_scan_at)}${data.last_scan_at ? ' 更新' : ''}`);
  const detail = data.running ? `已检查 ${fmtNumber(data.seen || 0)} 份 · 新增 ${fmtNumber(data.indexed || 0)} · 更新 ${fmtNumber(data.updated || 0)}` : (data.message || state);
  portalText('scanStatusText', detail);
  portalText('portalScanProgressText', detail);
  $('portalScanProgress').classList.toggle('hidden', !data.running);
  document.querySelectorAll('[data-scan-button]').forEach(button => { button.disabled = !!data.running || (!!currentUser?.directory_acl_enabled && currentUser.role !== 'super_admin'); });
  portalText('indexBtn', data.running ? '正在刷新…' : '开始刷新');
  ['sidebarStatusDot', 'portalHomeStatusDot'].forEach(id => {
    $(id).style.background = warning ? '#f79009' : data.root_configured ? '#12b76a' : '#98a2b3';
  });
  if (data.phase === 'scoped') {
    portalText('portalHomeIndexStatus', `当前账号可访问 ${fmtNumber(data.indexed_files)} 份文件`);
    portalText('portalSyncDescription', '仅统计当前账号已授权的目录；全盘同步由超级管理员管理。');
  }
  if (isAdminUser() && data.scan_history) renderPortalScanReconciliation(data.scan_history);
  if (wasRunning && !data.running) { initHomeDataCockpit(); loadPortalGovernance(true); }
}

function portalStatusUnavailable(error) {
  portalText('serverStatusText', '暂时无法连接');
  portalText('portalSyncState', '连接异常');
  portalText('portalCatalogSync', '连接异常');
  portalText('portalHomeIndexStatus', '暂时无法读取索引状态，请重试');
  ['sidebarStatusDot', 'portalHomeStatusDot'].forEach(id => {
    $(id).style.background = '#f79009';
  });
  if (currentNavMode === 'sync') portalText('portalSyncDescription', error.message || '状态读取失败，请重试。');
}

function renderPortalTrend(months) {
  const el = $('portalAssetTrend');
  const entries = (months || []).filter(item => /^\d{4}-\d{2}$/.test(item.month) && Number.isFinite(Number(item.count)) && Number(item.count) >= 0);
  if (!entries.length) {
    el.innerHTML = '<div class="portal-empty dark">暂无文件分布，开始使用后会在这里显示。</div>';
    return;
  }
  const now = new Date();
  const currentMonth = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`;
  const partial = entries[entries.length-1].month === currentMonth;
  const maxValue = Math.max(1,...entries.map(item => Number(item.count)));
  const ceiling = Math.ceil(maxValue / (maxValue > 10000 ? 5000 : maxValue > 1000 ? 500 : maxValue > 100 ? 50 : 10)) * (maxValue > 10000 ? 5000 : maxValue > 1000 ? 500 : maxValue > 100 ? 50 : 10);
  const left=46, right=622, top=16, bottom=132;
  const points = entries.map((item,index) => ({x:entries.length === 1 ? (left+right)/2 : left+(right-left)*index/(entries.length-1),y:bottom-Number(item.count)/ceiling*(bottom-top)}));
  const pathFor = points => points.map((point,index) => `${index ? 'L' : 'M'}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
  const path = pathFor(points);
  const solid = pathFor(partial && points.length>1 ? points.slice(0,-1) : points);
  const dashed = partial && points.length>1 ? pathFor(points.slice(-2)) : '';
  const labels = new Set(entries.length <= 5 ? entries.map((_,i)=>i) : [0,Math.floor((entries.length-1)/3),Math.floor((entries.length-1)*2/3),entries.length-1]);
  const valueLabel = value => value >= 10000 ? `${(value/10000).toFixed(value%10000 ? 1 : 0)}万` : fmtNumber(value);
  el.innerHTML = `<div class="trend-readout" aria-live="polite"><span id="portalTrendMonth"></span><strong id="portalTrendValue"></strong><span class="trend-current-note hidden" id="portalTrendPartial">本月进行中</span><select id="portalTrendMonthPicker" class="trend-month-picker" aria-label="选择月份查看数量">${entries.map((item,index)=>`<option value="${index}">${esc(item.month.replace('-','.'))}</option>`).join('')}</select></div>
    <svg viewBox="0 0 640 164" preserveAspectRatio="xMidYMid meet" role="group" aria-label="月度文件分布，聚焦月份可查看数量">
      <defs><linearGradient id="portalTrendFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#7c9fff" stop-opacity=".22"/><stop offset="100%" stop-color="#7c9fff" stop-opacity=".01"/></linearGradient></defs>
      ${[0,.5,1].map(ratio => { const y=bottom-ratio*(bottom-top);return `<path d="M${left},${y}H${right}" stroke="#a9bedb12" stroke-dasharray="${ratio ? '3 5' : '0'}"/><text class="trend-y-label" x="34" y="${y+3}" text-anchor="end" fill="#8e9eb9" font-size="9">${valueLabel(ceiling*ratio)}</text>`; }).join('')}
      <path d="M${points[0].x},${bottom} ${path.replace(/^M/,'L')} L${points[points.length-1].x},${bottom} Z" fill="url(#portalTrendFill)"/>
      <path d="${solid}" fill="none" stroke="#85a8ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
      ${dashed ? `<path d="${dashed}" fill="none" stroke="#a7bceb" stroke-width="1.8" stroke-dasharray="4 5" stroke-linecap="round" vector-effect="non-scaling-stroke"/>` : ''}
      <path class="trend-cursor" id="portalTrendCursor" d="M${points[points.length-1].x},${top}V${bottom}"/>
      ${points.map((point,index) => `<g class="trend-month-target" data-month-index="${index}" tabindex="0" role="button" aria-label="${esc(entries[index].month)}，${fmtNumber(entries[index].count)} 份${entries[index].month===currentMonth ? '，本月进行中' : ''}" aria-pressed="${index===points.length-1}"><rect x="${Math.max(left-9,point.x-(right-left)/Math.max(1,points.length-1)/2)}" y="${top-3}" width="${Math.min(52,(right-left)/Math.max(1,points.length-1))}" height="${bottom-top+12}" fill="transparent"/><circle class="trend-focus-ring" cx="${point.x}" cy="${point.y}" r="9"/><circle class="trend-point" cx="${point.x}" cy="${point.y}" r="3.5"/>${labels.has(index) ? `<text class="trend-axis-label ${index===0 || index===points.length-1 ? 'edge' : 'middle'}" x="${point.x}" y="156" text-anchor="${index===0 ? 'start' : index===points.length-1 ? 'end' : 'middle'}" fill="#91a1bd" font-size="9.5">${esc(entries[index].month.replace('-','.'))}</text>` : ''}</g>`).join('')}
    </svg><div class="trend-footnote">${partial ? '本月尚未结束，虚线表示截至今天的数量。' : '按文件最后修改日期归集。'}悬停或聚焦月份查看明细。</div>`;
  let selected=entries.length-1;
  const showMonth = index => {
    const item=entries[index];
    portalText('portalTrendMonth',`${item.month.slice(0,4)}年${Number(item.month.slice(5))}月`);
    portalText('portalTrendValue',fmtNumber(item.count) + ' 份');
    $('portalTrendPartial').classList.toggle('hidden',item.month!==currentMonth);
    $('portalTrendMonthPicker').value=String(index);
    $('portalTrendCursor').setAttribute('d',`M${points[index].x},${top}V${bottom}`);
    el.querySelectorAll('[data-month-index]').forEach((node,i) => {
      node.classList.toggle('selected',i===index);
      node.setAttribute('aria-pressed',String(i===selected));
    });
  };
  el.querySelectorAll('[data-month-index]').forEach(node => {
    const index=Number(node.dataset.monthIndex);
    node.addEventListener('pointerenter',()=>showMonth(index));
    node.addEventListener('focus',()=>showMonth(index));
    node.addEventListener('click',()=>{selected=index;showMonth(index);});
    node.addEventListener('keydown',event=>{
      if (event.key==='Enter' || event.key===' ') {event.preventDefault();selected=index;showMonth(index);}
      if (event.key==='ArrowLeft' || event.key==='ArrowRight') {
        event.preventDefault();
        el.querySelector(`[data-month-index="${Math.max(0,Math.min(entries.length-1,index+(event.key==='ArrowRight' ? 1 : -1)))}"]`)?.focus();
      }
    });
  });
  $('portalTrendMonthPicker').addEventListener('change',event=>{selected=Number(event.target.value);showMonth(selected);});
  el.querySelector('svg').addEventListener('pointerleave',()=>showMonth(selected));
  showMonth(selected);
}

function renderPortalOverview(data) {
  portalLastHomeData = data;
  portalText('cockpitTotalFiles', fmtNumber(data.total_files || 0));
  portalText('cockpitTodayFiles', `+${fmtNumber(data.today_added || 0)}`);
  portalText('cockpitDeptCount', fmtNumber(data.departments?.length || 0));
  const departments = data.departments || [];
  const active = departments.filter(item => item.today_added > 0).length;
  portalText('portalActiveDepts', fmtNumber(active));
  $('portalDeptProgress').style.width = `${departments.length ? active / departments.length * 100 : 0}%`;
  portalText('portalTodayDescription', `共 +${fmtNumber(data.today_added || 0)} 份 · 覆盖 ${fmtNumber(active)} 个业务域`);
  const distribution = data.ext_distribution || [];
  const colors = ['#7aa2ff', '#4c74d9', '#2f4c9e', '#8c9dc4', '#65759b', '#465777'];
  $('portalFormatBar').innerHTML = distribution.map((item, index) => `<span style="flex:${Math.max(0, Number(item.count) || 0)};background:${colors[index % colors.length]}" title="${esc(item.ext)} · ${fmtNumber(item.count)} 份"></span>`).join('');
  $('portalFormatLegend').innerHTML = distribution.length ? distribution.slice(0, 3).map(item => `<span>${esc(item.ext.replace('.', '').toUpperCase())} ${data.total_files ? Math.round(item.count / data.total_files * 100) : 0}%</span>`).join('') : '<span>暂无格式数据</span>';
  const sorted = [...departments].sort((a, b) => b.today_added - a.today_added || b.total_files - a.total_files).slice(0, 6);
  const max = Math.max(1, ...sorted.map(item => item.today_added || 0));
  const list = $('portalDepartmentList');
  list.innerHTML = sorted.length ? sorted.map((item, index) => `<button class="department-row" data-dept-index="${index}" title="${esc(item.name)}"><span class="department-name">${esc(cleanDeptLabel(item.name))}</span><span class="department-track"><i style="width:${(item.today_added || 0) / max * 100}%"></i></span><span class="department-count">${item.today_added ? '+' : ''}${fmtNumber(item.today_added || 0)}</span></button>`).join('') : '<div class="portal-empty">暂无业务域，建立索引后显示</div>';
  list.querySelectorAll('[data-dept-index]').forEach(button => {
    button.onclick = () => {
      const department=sorted[Number(button.dataset.deptIndex)];
      if(department?.path) locateInCatalog(department.path,{todayOnly:department.today_added>0});
    };
  });
  if (!data.total_files) $('q').placeholder = '搜索报表、业务线、单号或指标…';
  else $('q').placeholder = `搜索 ${fmtNumber(data.total_files)} 份报表…`;
}

// Called by the existing authenticated page initializer and after login/scan.
async function initHomeDataCockpit() {
  const requests = await Promise.allSettled([
    api('/api/catalog/overview'), api('/api/dashboard/summary'), api('/api/archive/tasks?status=pending&page_size=4')
  ]);
  if (requests[0].status === 'fulfilled' && requests[0].value.ok) renderPortalOverview(requests[0].value);
  else {
    $('portalDepartmentList').innerHTML = '<div class="portal-empty">暂时无法读取资产数据<button class="portal-retry" onclick="initHomeDataCockpit()">重新加载</button></div>';
  }
  if (requests[1].status === 'fulfilled' && requests[1].value.ok) renderPortalTrend(requests[1].value.monthly);
  else $('portalAssetTrend').innerHTML = '<div class="portal-empty dark">暂时无法读取文件分布</div>';
  if (requests[2].status === 'fulfilled' && requests[2].value.ok) {
    const data = requests[2].value;
    portalLastTasks = data.items || [];
    renderPortalHomeTasks(data);
  } else $('portalHomeTasks').innerHTML = '<div class="portal-empty">暂时无法读取待办<button class="portal-retry" onclick="initHomeDataCockpit()">重新加载</button></div>';
}

const portalTaskDetails = new Map();
let portalMonitoringTask = null, portalMonitoringSequence = 0, portalMonitoringFocus = null;
function findPortalArchiveTask(id) {
  return portalTaskDetails.get(Number(id)) || archiveState.tasks.find(task => Number(task.id)===Number(id));
}
function portalMonitorView(task) {
  const assessment=task.assessment || {};
  let title=assessment.reason_title || assessment.reason || '需要核对监控规则';
  let reason=assessment.reason || '请打开任务，核对监控目录与报表要求。';
  let badge=assessment.reason_code==='empty_delivery' ? '空文件' : assessment.state==='overdue' ? '未更新' : '待核对';
  if (task.status==='submitted') {title='已提交凭证，等待审核';reason=task.submit_note || '责任人已提交完成说明，请等待审核。';badge='待审核';}
  else if (task.status==='rejected') {title='提交凭证被驳回，需要补正';reason=task.review_comment || '请核对要求后重新提交凭证。';badge='已驳回';}
  else if (['approved','auto_resolved'].includes(task.status)) {title=task.status==='approved' ? '归档已审核通过' : '已检测到更新，任务已关闭';reason=task.review_comment || task.resolved_file || '该次归档任务已完成。';badge='已关闭';}
  return {assessment,title,reason,badge,report:assessment.file_keyword || task.rule_name,
    frequency:assessment.frequency_label || task.period_desc,directory:assessment.directory_display || task.target_path};
}
function portalMonitorTime(value, full=false) {
  if (!value) return '暂无有效更新';
  return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',...(full ? {year:'numeric'} : {}),month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(Number(value)*1000));
}
function portalMonitorDelay(seconds) {
  if (!seconds) return '';
  const minutes=Math.max(1,Math.floor(seconds/60));
  return minutes<60 ? `${minutes} 分钟` : minutes<1440 ? `${Math.floor(minutes/60)} 小时${minutes%60 ? ` ${minutes%60} 分钟` : ''}` : `${Math.floor(minutes/1440)} 天${Math.floor(minutes%1440/60) ? ` ${Math.floor(minutes%1440/60)} 小时` : ''}`;
}
function renderPortalHomeTasks(data) {
  const list=$('portalHomeTasks'),tasks=data.items || [];
  tasks.forEach(task=>portalTaskDetails.set(Number(task.id),task));
  portalText('portalPendingTotal',fmtNumber(data.total || 0));
  portalText('portalTaskCount',`· ${fmtNumber(data.total || 0)}`);
  const seconds=Math.max(0,...tasks.map(task=>Number(task.assessment?.overdue_seconds || 0)));
  portalText('portalMaxOverdue',seconds ? `最长超时 ${portalMonitorDelay(seconds)}` : '');
  list.innerHTML=tasks.length ? tasks.slice(0,3).map(task=>{
    const view=portalMonitorView(task),a=view.assessment;
    return `<button class="monitor-home-alert" data-monitor-task="${Number(task.id)}" aria-label="查看监控任务：${esc(view.report)}"><span class="monitor-alert-heading"><strong>${esc(view.report)}</strong><span class="monitor-badge ${esc(task.status)}">${esc(view.badge)}</span></span><span class="monitor-alert-reason">${esc(view.title)}</span><span class="monitor-alert-meta">应更新：${esc(a.expected_label || view.frequency)}${a.expected_label ? ' 前' : ''}</span><span class="monitor-alert-meta">最近有效：${esc(portalMonitorTime(a.latest_time || task.last_file_time))}</span><span class="monitor-alert-owner">${esc(task.responsible_person || '未指定责任人')}<span>查看任务详情 ↗</span></span></button>`;
  }).join('')+(data.total>3 ? `<button class="home-task-more" onclick="switchNavMode('archive')">查看全部 ${fmtNumber(data.total)} 项监控待办 →</button>` : '') : '<div class="portal-empty">当前可访问的目录中暂无待处理归档</div>';
  list.querySelectorAll('[data-monitor-task]').forEach(button=>button.addEventListener('click',()=>openPortalMonitoringTask(Number(button.dataset.monitorTask))));
}
async function openPortalMonitoringTask(id) {
  const sequence=++portalMonitoringSequence;
  portalMonitoringFocus=document.activeElement;
  $('monitoringTaskModal').classList.remove('hidden');
  $('monitoringTaskTitle').textContent='正在读取监控任务…';
  $('monitoringTaskBody').innerHTML='<div class="portal-empty">正在核对报表、应更新时间与最新检测结果…</div>';
  $('monitoringTaskActions').innerHTML='<button class="tree-tool-btn" onclick="closePortalMonitoringTask()">关闭</button>';
  $('monitoringTaskModal').querySelector('.modal-close-btn').focus();
  try {
    if(currentNavMode!=='archive') await switchNavMode('archive');
    const response=await api(`/api/archive/tasks/${Number(id)}`);
    if(sequence!==portalMonitoringSequence)return;
    const task=response.task,view=portalMonitorView(task),a=view.assessment;
    portalMonitoringTask=task;portalTaskDetails.set(Number(task.id),task);
    $('monitoringTaskTitle').textContent=view.report;
    $('monitoringTaskEyebrow').textContent=`监控任务 · TSK-${String(task.id).padStart(4,'0')}`;
    const rows=[['监控任务',task.rule_name],['监控目录',view.directory],['匹配报表',a.file_keyword ? `文件名包含“${a.file_keyword}”` : '目录内任一报表'],['更新要求',view.frequency],['本期应更新',a.expected_at_display || '待核对'],['最近有效更新',portalMonitorTime(a.latest_time || task.last_file_time,true)],['最近有效文件',a.latest_name || task.last_file_name || '暂无'],['责任人 / 组',task.responsible_person || '未指定'],['最近检测',a.assessed_at_display || '待核对']];
    $('monitoringTaskBody').innerHTML=`<div class="monitor-reason-box ${esc(task.status)}"><span class="monitor-badge ${esc(task.status)}">${esc(view.badge)}</span><h4>${esc(view.title)}</h4><p>${esc(view.reason)}</p>${a.overdue_seconds>0 && ['pending','rejected'].includes(task.status) ? `<small>已超过本期截止时间 ${esc(portalMonitorDelay(a.overdue_seconds))}</small>` : ''}</div><dl class="monitor-task-facts">${rows.map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl><div class="monitor-next-step"><strong>建议处理</strong><p>${task.status==='submitted' ? '等待管理员核对交付文件与凭证。' : ['approved','auto_resolved'].includes(task.status) ? '该次任务已关闭，可以定位目录查看交付文件。' : '先检查报表是否已放入监控目录，确认文件非空并完成同步，再提交完成凭证。'}</p><small>提醒依据已同步文件的名称、修改时间及是否为空判断，不推断上游系统故障。</small></div>`;
    $('monitoringTaskActions').innerHTML=`<button class="tree-tool-btn" onclick="closePortalMonitoringTask()">关闭</button><button class="btn-action-secondary" data-monitor-action="locate">定位监控目录</button>${task.proof_image ? '<button class="tree-tool-btn" data-monitor-action="proof">查看凭证</button>' : ''}${['pending','rejected'].includes(task.status) ? '<button class="btn-archive-primary" data-monitor-action="submit">提交完成凭证</button>' : task.status==='submitted' && isAdminUser() ? '<button class="btn-archive-primary" data-monitor-action="review">审核凭证</button>' : ''}`;
    $('monitoringTaskActions').querySelectorAll('[data-monitor-action]').forEach(button=>button.addEventListener('click',()=>{
      const action=button.dataset.monitorAction;closePortalMonitoringTask();
      if(action==='locate')locateInCatalog(task.target_path,{fileKeyword:task.assessment?.file_keyword || ''});
      else if(action==='submit')openSubmitProofModal(Number(task.id));
      else if(action==='review')openAdminReviewModal(Number(task.id));
      else if(action==='proof')openImagePreview(task.proof_image);
    }));
  } catch(error) {if(sequence===portalMonitoringSequence){$('monitoringTaskTitle').textContent='暂时无法打开任务';$('monitoringTaskBody').textContent=error.message;}}
}
function closePortalMonitoringTask() {
  portalMonitoringSequence++;
  $('monitoringTaskModal').classList.add('hidden');
  (portalMonitoringFocus?.getClientRects().length ? portalMonitoringFocus : $('navArchiveTab'))?.focus();
}

function updatePortalArchive(data) {
  const pending = Number(data.pending_tasks || 0);
  if (portalKnownPending !== null && portalKnownPending !== pending) {
    api('/api/archive/tasks?status=pending&page_size=4').then(result => {
      if (result.ok) renderPortalHomeTasks(result);
    }).catch(() => {});
  }
  portalKnownPending = pending;
  const total = Number(data.pending_tasks || 0) + Number(data.submitted_tasks || 0);
  portalText('portalArchiveRate', `当前规则达标率：${data.compliance_rate == null ? '待核验' : `${fmtNumber(data.compliance_rate)}%`} · 可核验 ${fmtNumber(data.verifiable_rules || 0)} / ${fmtNumber(data.total_rules || 0)} 条`);
  portalText('portalPendingCount', fmtNumber(total));
  $('portalPendingCount').classList.toggle('hidden', total === 0);
  const when = data.scheduler?.last_run_display;
  portalText('portalArchiveDescription', `${fmtNumber(data.pending_tasks || 0)} 项待处理，${fmtNumber(data.submitted_tasks || 0)} 项待审核${when ? ` · 上次检测 ${when}` : ''}`);
  portalText('portalPendingTotal', fmtNumber(data.pending_tasks || 0));
  renderPortalArchiveNotice();
  if (data.scheduler) {
    portalText('portalArchiveInterval', `每 ${fmtNumber(data.scheduler.interval_seconds || 60)} 秒`);
    portalText('portalArchiveJobState', data.scheduler.enabled ? '已启用' : '未启用');
  }
}

function renderPortalArchiveNotice() {
  const notice = $('portalArchiveNotice');
  const issues = archiveState.overview?.rule_issues || [];
  notice.classList.toggle('hidden', issues.length === 0);
  if (!issues.length) return;
  const empty = issues.filter(issue => issue.state === 'no_content').length;
  notice.innerHTML = `<span>${issues.length} 条规则需核对${empty ? `，其中 ${empty} 条只有空占位文件` : ''}。这类规则不参与达标率，也不会自动关闭或产生逾期判断。</span>${isAdminUser() ? `<button onclick="switchArchiveSubTab('rules')">查看规则 →</button>` : ''}`;
}

// Queue layout uses the same task objects and existing submit/review dialogs.
function renderArchiveTasks(tasks) {
  const grid=$('archiveTaskGrid');renderPortalArchiveNotice();
  if(!tasks?.length){grid.innerHTML='<div class="home-panel portal-empty">当前分类暂无监控待办</div>';return;}
  tasks.forEach(task=>portalTaskDetails.set(Number(task.id),task));
  grid.innerHTML=`<div class="archive-task-table-wrap monitor-queue-wrap"><table class="archive-task-table monitor-queue"><thead><tr><th style="width:30%">监控任务 / 更新要求</th><th style="width:33%">异常原因 / 当前进度</th><th style="width:22%">最近有效更新</th><th style="width:15%;text-align:right">操作</th></tr></thead><tbody>${tasks.map(task=>{
    const view=portalMonitorView(task),a=view.assessment;
    return `<tr><td><button class="monitor-task-link" data-monitor-open="${Number(task.id)}">${esc(view.report)}</button><div class="monitor-queue-meta">${esc(view.frequency)} · ${esc(task.responsible_person || '未指定')}</div><div class="monitor-queue-directory" title="${esc(task.target_path)}">${esc(view.directory)}</div></td><td><div class="monitor-queue-reason ${esc(task.status)}">${esc(view.title)}</div><div class="monitor-queue-meta">${esc(a.expected_label ? `本期截止：${a.expected_label}` : view.reason)}</div><span class="monitor-badge ${esc(task.status)}">${esc(view.badge)}${a.overdue_seconds>0 && ['pending','rejected'].includes(task.status) ? ` · 超时 ${esc(portalMonitorDelay(a.overdue_seconds))}` : ''}</span></td><td><time class="monitor-queue-time">${esc(portalMonitorTime(a.latest_time || task.last_file_time))}</time><div class="monitor-queue-meta monitor-last-file">${esc(a.latest_name || task.last_file_name || '暂无非空报表')}</div></td><td><div class="monitor-queue-actions"><button class="table-action" data-monitor-open="${Number(task.id)}">任务详情</button><button class="table-link" data-monitor-locate="${Number(task.id)}">定位目录</button></div></td></tr>`;
  }).join('')}</tbody></table></div><div class="archive-queue-meta"><span>共 ${fmtNumber(portalArchiveTotal)} 项 · 第 ${portalArchivePage} 页</span><div><button class="page-btn" onclick="changePortalArchivePage(-1)" ${portalArchivePage<=1 ? 'disabled' : ''}>上一页</button><button class="page-btn" onclick="changePortalArchivePage(1)" ${portalArchivePage*25>=portalArchiveTotal ? 'disabled' : ''}>下一页</button></div></div>`;
  grid.querySelectorAll('[data-monitor-open]').forEach(button=>button.addEventListener('click',()=>openPortalMonitoringTask(Number(button.dataset.monitorOpen))));
  grid.querySelectorAll('[data-monitor-locate]').forEach(button=>button.addEventListener('click',()=>{const task=findPortalArchiveTask(Number(button.dataset.monitorLocate));locateInCatalog(task.target_path,{fileKeyword:task.assessment?.file_keyword || ''});}));
}

function changePortalArchivePage(delta) {
  const next = portalArchivePage + delta;
  if (next < 1 || next > Math.ceil(portalArchiveTotal / 25)) return;
  portalArchivePage = next;
  loadArchiveTasks();
}

async function refreshPortalSync() {
  await Promise.allSettled([loadStatus(), loadArchiveOverview(), loadRecent(), loadPortalGovernance()]);
}

async function runPortalArchiveCheck() {
  const button = $('portalRunArchiveCheck');
  button.disabled = true;
  try {
    await triggerArchiveCheck();
    await initHomeDataCockpit();
  } finally { button.disabled = false; }
}

async function checkPortalFile() {
  const value = $('portalCheckQuery').value.trim();
  if (!value) return;
  const sequence = ++portalFileCheckSequence;
  const button = $('portalCheckButton');
  const output = $('portalCheckResult');
  button.disabled = true;
  output.textContent = '正在查询索引…';
  try {
    const params = new URLSearchParams({ q: value, target: value.includes('/') || value.includes('\\') ? 'path' : 'title', enable_ai: 'false', limit: '5' });
    const result = await api(`/api/search?${params}`);
    if (sequence !== portalFileCheckSequence) return;
    const count = result.total_candidates ?? result.count ?? 0;
    output.innerHTML = count > 0 ? `找到 ${fmtNumber(count)} 份匹配文件。<button id="portalCheckOpen">查看搜索结果 →</button>` : '当前索引未找到匹配文件。请确认文件位于配置目录内、格式受支持，或刷新索引后重试。';
    if ($('portalCheckOpen')) $('portalCheckOpen').onclick = () => {
      switchNavMode('search');
      $('q').value = value;
      clearPortalSearchFilters();
      searchTarget = params.get('target');
      search(1).finally(() => { searchTarget = 'title'; });
    };
  } catch (error) { output.textContent = `检查失败：${error.message}`; }
  finally { if (sequence === portalFileCheckSequence) button.disabled = false; }
}

window.addEventListener('DOMContentLoaded', () => {
  // Poll only while the sync view is visible; the existing scan poller still
  // handles an active scan if the user moves to another workspace.
  portalSyncTimer = setInterval(() => {
    if (currentNavMode === 'sync' && !document.hidden) refreshPortalSync();
  }, 5000);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') togglePortalSidebar(false);
    if (event.key === '6' && !event.metaKey && !event.ctrlKey && !event.altKey && !['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(event.target.tagName)) switchNavMode('sync');
  });
});
window.addEventListener('pagehide', () => clearInterval(portalSyncTimer));

// Product foundation: explainable metrics, real account roles and privacy-safe events.
function applyPortalUserRole() {
  const admin = !!isAdminUser();
  archiveState.userRole = admin ? 'admin' : 'worker';
  portalText('portalCurrentArchiveRole', currentUser ? `${currentUser.name} · ${admin ? '管理员' : '责任人'}` : '请先登录');
  document.querySelectorAll('#runCheckBtn, #portalRunArchiveCheck, #subtabRules, [onclick="openCreateRuleModal()"], [onclick="openCreateRuleModalForSelectedNode()"], #portalGovernancePanel').forEach(element => element.classList.toggle('hidden', !admin));
  if (!admin && archiveState.currentSubTab === 'rules') switchArchiveSubTab('tasks');
  if (currentNavMode === 'archive') renderArchiveTasks(archiveState.tasks);
  $('portalGovernancePanel')?.classList.toggle('hidden', currentUser?.role !== 'super_admin');
  if (currentNavMode === 'sync' && currentUser?.role === 'super_admin') loadPortalGovernance(true);
  updateDirectoryAccessNotice();
  loadPortalFilterDirectories();
  updateDemoBanner();
}

function recordPortalEvent(eventType, fileId) {
  if (!fileId) return;
  api('/api/telemetry/events', { method: 'POST', body: { event_type: eventType, file_id: fileId, search_event_id: portalSearchEventId }, timeoutMs: 5000 }).catch(() => {});
}

function portalAssessmentLabel(assessment) {
  return { healthy: '更新正常', overdue: '已超过检测节点', scheduled:'尚未到检测节点',paused:'休息日暂停检测',calendar_unavailable:'工作日历待维护', no_files: '目录未匹配文件', no_content: '只有空占位文件', invalid_path: '目录路径待修正' }[assessment?.state] || '尚未核验';
}

function portalRuleNeedsCheck(task) {
  return task.assessment && !['healthy', 'overdue'].includes(task.assessment.state);
}

async function preflightPortalRule(mode) {
  const edit = mode === 'edit';
  const input = $(edit ? 'editRulePath' : 'newRulePath');
  const output = $(edit ? 'portalRulePreflightEdit' : 'portalRulePreflightCreate');
  const path = input.value.trim();
  const days = Number($(edit ? 'editRulePeriodDays' : 'newRulePeriodDays').value) || 7;
  if (!path) { output.textContent = '请先填写监控目录。'; return false; }
  output.textContent = '正在检查当前索引…';
  try {
    const result = await api('/api/archive/rules/preflight?' + new URLSearchParams({path,period_days:String(days),file_keyword:$(edit ? 'editRuleKeyword' : 'newRuleKeyword').value.trim(),period_type:$(edit ? 'editRulePeriodType' : 'newRulePeriodType').value,deadline_time:$(edit ? 'editRuleDeadline' : 'newRuleDeadline').value || '18:00',schedule_config:JSON.stringify(readRuleSchedule(edit?'edit':'new'))}));
    if (input.value.trim() !== path) return false;
    output.classList.toggle('warning', !result.can_save || result.state === 'no_content');
    output.innerHTML = `<b>${esc(portalAssessmentLabel(result))}</b><div>匹配 ${fmtNumber(result.matched_files)} 份 · 非空 ${fmtNumber(result.nonempty_files)} 份</div><div>${esc(result.reason)}</div>${result.latest_time ? `<div>最近有效更新：${esc(fmtDate(result.latest_time).full)}</div>` : ''}${result.state === 'no_content' ? '<div>可保存为待核验规则，空文件不会自动关闭任务或产生逾期判断。</div>' : ''}`;
    return result.can_save;
  } catch (error) { output.textContent = error.message || '目录检查失败，请重试'; return false; }
}

let portalGovernanceLoadedAt = 0;
async function loadPortalGovernance(force = false) {
  if (currentUser?.role !== 'super_admin' || (!force && Date.now() - portalGovernanceLoadedAt < 15000)) return;
  portalGovernanceLoadedAt = Date.now();
  try {
    const data = await api('/api/governance/summary');
    renderPortalScanReconciliation(data.scans || []);
    renderPortalProductBaseline(data.product || {});
    $('portalMetricDefinitions').innerHTML = (data.metrics || []).map(metric => `<div class="metric-definition"><b>${esc(metric.label)}</b><p>${esc(metric.definition)}</p></div>`).join('');
  } catch (error) {
    portalText('portalProductBaseline', `读取失败：${error.message}`);
    portalGovernanceLoadedAt = 0;
  }
}

function renderPortalScanReconciliation(scans) {
  const output = $('portalScanReconciliation');
  if (!scans.length) {
    output.innerHTML = '<div class="foundation-empty">暂无扫描对账记录。点击“开始刷新”完成一次扫描后显示；不据此推断网盘覆盖率。</div>';
    return;
  }
  const scan = scans[0], counts = scan.counts;
  const entries = [['已发现文件', counts.discovered], ['成功处理', counts.seen], ['明确排除', counts.excluded], ['文件读取异常', counts.errors]];
  const reasonLabels = { temporary: '临时文件', sensitive_name: '敏感名称排除', unsupported_format: '不支持的格式', symbolic_link: '文件符号链接', not_a_regular_file: '非普通文件' };
  output.innerHTML = `<div class="foundation-metric-grid">${entries.map(([label, value]) => `<div><span>${esc(label)}</span><strong>${fmtNumber(value)}</strong></div>`).join('')}</div><div class="foundation-reconciliation ${scan.phase === 'complete' && counts.reconciled ? 'success' : 'warning'}">${scan.phase === 'complete' && counts.reconciled ? '本次扫描对账一致' : '本次扫描未完整完成，请检查异常'} · ${esc(portalDate(scan.finished_at))}</div><div class="foundation-reasons">${Object.entries(counts.exclusion_reasons || {}).map(([reason, count]) => `<span>${esc(reasonLabels[reason] || reason)} ${fmtNumber(count)}</span>`).join('') || '<span>没有明确排除的文件</span>'}</div><p class="foundation-scope-note">另有 ${fmtNumber(counts.excluded_directories)} 个被排除目录、${fmtNumber(counts.directory_errors)} 个访问异常目录，其内部文件数量未知。以上只对本次可观察范围对账；未检查表格内容解析或整个网盘覆盖率。</p><details class="scan-history-details"><summary>最近 ${scans.length} 次扫描</summary>${scans.map(run => `<div><span>${esc(portalDate(run.finished_at))}</span><span>${esc({ complete: '完成', partial: '部分完成', error: '失败' }[run.phase] || run.phase)}</span><span>处理 ${fmtNumber(run.counts.seen)} 份</span></div>`).join('')}</details>`;
}

function renderPortalProductBaseline(product) {
  const format = (value, unit = '') => value == null ? '待采集' : `${fmtNumber(value)}${unit}`;
  const entries = [['搜索次数', format(product.searches)], ['零结果率', format(product.zero_result_rate, '%')], ['检索计算 P95', format(product.search_p95_ms, ' ms')], ['有效操作搜索占比', format(product.action_search_rate, '%')], ['首次有效操作 P50', format(product.first_action_p50_seconds, ' 秒')]];
  const eventNames = { search: '搜索', result_click: '结果选择', preview_open: '预览', path_copy: '复制路径', download_request: '下载申请', download_review: '下载审核', archive_submit: '归档提交', archive_review: '归档审核', index_start: '刷新触发', rule_create: '新建规则', rule_update: '修改规则', rule_toggle: '规则启停', rule_delete: '删除规则' };
  $('portalProductBaseline').innerHTML = `<div class="foundation-metric-grid">${entries.map(([label, value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('')}</div><div class="foundation-reasons">${Object.entries(product.event_counts || {}).map(([name, count]) => `<span>${esc(eventNames[name] || name)} ${fmtNumber(count)}</span>`).join('')}</div><p class="foundation-scope-note">${esc(product.privacy || '')} ${product.collection_started_at ? `自 ${esc(fmtDate(product.collection_started_at).full)} 开始采集；` : ''}数据不足两周时只展示已采集数据，不宣称达标。检索计算耗时分位数最多使用最近 ${fmtNumber(product.latency_sample_limit || 10000)} 次搜索，当前样本 ${fmtNumber(product.latency_sample_size || 0)} 次。</p>${(product.by_role || []).length ? `<div class="foundation-reasons">${product.by_role.map(role => `<span>${esc({ super_admin: '超级管理员', dept_admin: '部门管理员', user: '成员', anonymous: '未登录访客' }[role.actor_role] || role.actor_role)}：${fmtNumber(role.searches)} 次搜索 / ${fmtNumber(role.zero_results)} 次零结果</span>`).join('')}</div>` : ''}`;
}

// Chat actions are bound as data, never interpolated into executable handlers.
$('aiChatBody')?.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.aiSearch !== undefined) {
    $('q').value = button.dataset.aiSearch;
    switchNavMode('search');
    search(1);
    toggleAIChat(false);
  } else if (button.dataset.aiLocate !== undefined) {
    locateInCatalog(button.dataset.aiLocate);
    toggleAIChat(false);
  } else if (button.dataset.aiCopy !== undefined) copyPathDirect(button.dataset.aiCopy);
});
document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'j') { event.preventDefault(); toggleAIChat(); }
  if (event.key === 'Escape' && !$('aiDrawerOverlay').classList.contains('hidden')) toggleAIChat(false);
});

let directoryGrantData = [];
async function loadDirectoryGrantPanel() {
  const panel = $('directoryGrantPanel');
  if (!panel || currentUser?.role !== 'super_admin') return;
  panel.innerHTML = '<p>正在加载目录授权…</p>';
  try {
    const [permissions, tree] = await Promise.all([api('/api/system/directory-grants'), api('/api/catalog/tree')]);
    directoryGrantData = permissions.grants || [];
    const paths = [];
    const collect = node => {
      if (!node) return;
      paths.push([node.path, node.path === tree.tree?.path ? '整个文件根目录（包含所有子目录）' : node.path.replace(tree.tree?.path || '', '').replace(/^\//, '')]);
      (node.children || []).forEach(collect);
    };
    collect(tree.tree || tree);
    panel.innerHTML = `<h3>目录访问授权</h3><p>${permissions.enabled ? '授权自动覆盖所有子目录；拒绝优先。仅授权深层目录时，组织树显示必要的祖先路径，兄弟目录保持不可见。' : '目录权限校验当前已关闭，请在服务配置中开启 DIRECTORY_ACL_ENABLED。'}<br>超级管理员可查看全部；其他账号未授权时默认无文件权限。部门按成员资料中的部门精确匹配。</p>
      <div class="directory-grant-form"><label>授权给<select id="grantSubjectType"><option value="user">指定成员</option><option value="department">整个部门</option></select></label><label>成员 / 部门<select id="grantSubject"></select></label><label class="grant-path">目录（包含子目录）<select id="grantTarget"><option value="">请选择需要授权的目录</option>${paths.map(([path,label]) => `<option value="${esc(path)}">${esc(label)}</option>`).join('')}</select></label><label>访问规则<select id="grantEffect"><option value="allow">允许访问</option><option value="deny">拒绝访问</option></select></label><button class="btn-archive-primary" id="saveDirectoryGrant">保存授权</button></div><div id="directoryGrantStatus" role="status" style="font-size:12px;margin-top:10px;color:#5369d0"></div><div id="directoryGrantList" class="directory-grant-list"></div>`;
    const updateSubjects = () => {
      const department = $('grantSubjectType').value === 'department';
      const options = department ? [...new Set(permState.users.map(u => u.department).filter(Boolean))].map(d => [d,d]) : permState.users.filter(u => u.role !== 'super_admin').map(u => [u.id, `${u.name} · ${u.department || '未填部门'}`]);
      $('grantSubject').innerHTML = options.map(([key,label]) => `<option value="${esc(String(key))}">${esc(label)}</option>`).join('');
    };
    $('grantSubjectType').addEventListener('change', updateSubjects);
    updateSubjects();
    $('saveDirectoryGrant').addEventListener('click', saveDirectoryGrant);
    renderDirectoryGrants();
  } catch (error) { panel.textContent = `目录授权加载失败：${error.message}`; }
}
function renderDirectoryGrants() {
  const list = $('directoryGrantList');
  if (!directoryGrantData.length) { list.textContent = '暂无目录授权。普通账号需由超级管理员授权后才能访问文件。'; return; }
  list.innerHTML = directoryGrantData.map(grant => {
    const subject = grant.subject_type === 'user' ? permState.users.find(u => String(u.id) === grant.subject_key)?.name || `成员 #${grant.subject_key}` : `${grant.subject_key}（部门）`;
    return `<div class="directory-grant-row"><div><strong>${esc(subject)}</strong> · ${grant.effect === 'deny' ? '拒绝' : '允许'}<small>${esc(grant.target_path)} · 子目录继承</small></div><button class="tree-tool-btn" data-revoke-grant="${grant.id}">撤销</button></div>`;
  }).join('');
  list.querySelectorAll('[data-revoke-grant]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await api(`/api/system/directory-grants/${button.dataset.revokeGrant}`, { method:'DELETE' });
      directoryGrantData = directoryGrantData.filter(g => g.id !== Number(button.dataset.revokeGrant));
      renderDirectoryGrants();
      $('directoryGrantStatus').textContent = '已撤销；后续搜索、预览及下载立即按新权限校验。';
    } catch (error) { $('directoryGrantStatus').textContent = error.message; button.disabled = false; }
  }));
}
async function saveDirectoryGrant() {
  const status = $('directoryGrantStatus');
  if (!$('grantTarget').value || !$('grantSubject').value) { status.textContent = '请先选择授权对象和目录。'; return; }
  $('saveDirectoryGrant').disabled = true;
  try {
    const res = await api('/api/system/directory-grants', { method:'POST', body:{subject_type:$('grantSubjectType').value, subject_key:$('grantSubject').value, target_path:$('grantTarget').value, effect:$('grantEffect').value} });
    directoryGrantData = (await api('/api/system/directory-grants')).grants;
    renderDirectoryGrants();
    status.textContent = res.message;
  } catch (error) { status.textContent = error.message; }
  finally { $('saveDirectoryGrant').disabled = false; }
}

async function updateDirectoryAccessNotice() {
  let notice = $('directoryAccessNotice');
  if (!notice) {
    notice = document.createElement('div');
    notice.id = 'directoryAccessNotice';
    notice.className = 'directory-access-notice hidden';
    $('searchView').prepend(notice);
  }
  notice.classList.add('hidden');
  if (!currentUser?.directory_acl_enabled || currentUser.role === 'super_admin') return;
  const actor = currentUser.id;
  try {
    const scopes = await api('/api/auth/scopes');
    if (currentUser?.id !== actor) return;
    if (!scopes.unrestricted && !scopes.grants.some(grant => grant.effect === 'allow')) {
      notice.textContent = '当前账号尚未获授文件目录，请联系超级管理员在「人员权限 → 目录访问授权」中配置。';
      notice.classList.remove('hidden');
    }
  } catch (_) {}
}

function updateDemoBanner() {
  $('portalDemoBanner').classList.toggle('hidden', !currentUser?.demo_mode);
  $('portalDemoDate').textContent = new Date().toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric'});
}
let portalDemoDateKey = new Date().toDateString();
async function refreshDemoAfterMidnight() {
  if (!currentUser?.demo_mode || document.visibilityState !== 'visible') return;
  const today = new Date().toDateString();
  if (today === portalDemoDateKey) return;
  portalDemoDateKey = today;
  await loadStatus();
  updateDemoBanner();
  initHomeDataCockpit();
  loadArchiveOverview();
  if (currentNavMode === 'catalog') initCatalogDashboard();
}
document.addEventListener('visibilitychange',refreshDemoAfterMidnight);
window.addEventListener('focus',refreshDemoAfterMidnight);

const portalExtraSearchFilters={directory:'',size:'',content_state:'all'};
let portalFilterDirectoryActor=null;
function setPortalExtraFilter(key,value){portalExtraSearchFilters[key]=value;syncPortalAdvancedFilters();if($('q').value.trim())search(1);}
function resetPortalExtraFilters(){Object.assign(portalExtraSearchFilters,{directory:'',size:'',content_state:'all'});syncPortalAdvancedFilters();}
function appendPortalExtraFilters(params){
  if(portalExtraSearchFilters.directory)params.set('directory',portalExtraSearchFilters.directory);
  const ranges={small:[0,102399],medium:[102400,1048575],large:[1048576,10485759],huge:[10485760,null]};
  const range=ranges[portalExtraSearchFilters.size];
  if(range){params.set('min_size',String(range[0]));if(range[1]!==null)params.set('max_size',String(range[1]));}
  if(portalExtraSearchFilters.content_state!=='all')params.set('content_state',portalExtraSearchFilters.content_state);
}
function syncPortalAdvancedFilters(){
  if(!$('searchDirectoryFilter'))return;
  $('searchDirectoryFilter').value=portalExtraSearchFilters.directory;
  $('searchSizeFilter').value=portalExtraSearchFilters.size;
  $('searchContentFilter').value=portalExtraSearchFilters.content_state;
  $('sidebarDateField').value=dateField;
  $('sidebarDateFrom').value=dateFrom;$('sidebarDateTo').value=dateTo;
  const count=Number(!!portalExtraSearchFilters.directory)+Number(!!portalExtraSearchFilters.size)+Number(portalExtraSearchFilters.content_state!=='all');
  $('extraSearchFilterSummary').textContent=count ? `已应用 ${count} 项目录 / 大小 / 状态条件` : '条件可组合使用';
}
function setPortalSidebarDates(){
  const from=$('sidebarDateFrom').value,to=$('sidebarDateTo').value;
  if(from&&to&&from>to){toast('开始日期不能晚于结束日期');return;}
  $('dateFromInput').value=from;$('dateToInput').value=to;onDateFilterChange();
}
async function loadPortalFilterDirectories(){
  if(!currentUser || portalFilterDirectoryActor===currentUser.id)return;
  const actor=currentUser.id;
  try{
    const result=await api('/api/catalog/overview');
    if(currentUser?.id!==actor)return;
    $('searchDirectoryFilter').innerHTML='<option value="">全部可访问目录</option>'+(result.departments || []).map(item=>`<option value="${esc(item.path)}">${esc(portalOrganizationLabel(item.name))}</option>`).join('');
    portalFilterDirectoryActor=actor;syncPortalAdvancedFilters();
  }catch(error){portalFilterDirectoryActor=null;}
}

// Reporting periods and delivery deadlines share one validated server-side calculator.
const ruleScheduleTimers = {};
const ruleScheduleVersions = {};
function readRuleSchedule(prefix) {
  const dates = suffix => ($(prefix + suffix).value || '').split(/[\s,，;；]+/).filter(Boolean);
  return {mode:$(prefix+'ScheduleMode').value,due_period:$(prefix+'DuePeriod').value,
    due_day:Number($(prefix+'DueDay').value),day_basis:$(prefix+'DayBasis').value,
    grace_days:Number($(prefix+'GraceDays').value),holiday_policy:$(prefix+'HolidayPolicy').value,
    calendar:$(prefix+'WorkCalendar').value,extra_holidays:dates('ExtraHolidays'),extra_workdays:dates('ExtraWorkdays')};
}
function fillRuleSchedule(prefix, value={}) {
  const values = {ScheduleMode:value.mode || 'calendar',DuePeriod:value.due_period || 'next',DueDay:value.due_day || 1,
    DayBasis:value.day_basis || 'calendar',GraceDays:value.grace_days || 0,HolidayPolicy:value.holiday_policy || 'keep',
    WorkCalendar:value.calendar || 'cn',ExtraHolidays:(value.extra_holidays || []).join(', '),ExtraWorkdays:(value.extra_workdays || []).join(', ')};
  Object.entries(values).forEach(([suffix,v])=>{$(prefix+suffix).value=v;});
  syncRuleScheduleFields(prefix);
}
function syncRuleScheduleFields(prefix, periodChanged=false) {
  const period=$(prefix+'RulePeriodType').value, mode=$(prefix+'ScheduleMode');
  if(period==='custom') mode.value='rolling';
  const calendar=mode.value==='calendar';
  $(prefix+'RuleScheduleEditor').querySelectorAll('[data-calendar-field]').forEach(el=>el.classList.toggle('hidden',!calendar));
  $(prefix+'RuleDeadline').disabled=!calendar && period!=='daily';
  $(prefix+'RulePeriodDays').disabled=period!=='custom';
  const due=$(prefix+'DuePeriod');
  const labels={daily:['当天','次日'],weekly:['本周','下周'],monthly:['本月','下个月'],custom:['本周期','下一周期']}[period];
  [...due.options].forEach((o,i)=>o.textContent=labels[i]);
  if(periodChanged){$(prefix+'DueDay').value=period==='weekly'?2:period==='monthly'?5:1;}
  $(prefix+'DueDay').max=period==='weekly'?7:31;
  queueRuleSchedulePreview(prefix);
}
function queueRuleSchedulePreview(prefix) {
  clearTimeout(ruleScheduleTimers[prefix]);
  ruleScheduleVersions[prefix]=(ruleScheduleVersions[prefix] || 0)+1;
  const version=ruleScheduleVersions[prefix];
  ruleScheduleTimers[prefix]=setTimeout(()=>refreshRuleSchedulePreview(prefix,version),250);
}
async function refreshRuleSchedulePreview(prefix,version) {
  const output=$(prefix+'SchedulePreview');
  output.textContent='正在计算最近三次检测时间…';
  try {
    const data=await api('/api/archive/rules/schedule-preview',{method:'POST',body:{period_type:$(prefix+'RulePeriodType').value,deadline_time:$(prefix+'RuleDeadline').value || '18:00',schedule:readRuleSchedule(prefix)}});
    if(version!==ruleScheduleVersions[prefix])return;
    output.innerHTML=`<b>${esc(data.summary)}</b>${data.upcoming?.length?`<div class="schedule-preview-title">最近检测节点（最多 3 次）· 北京时间</div><ol>${data.upcoming.map(row=>`<li><span>${esc(row.period_start)} 至 ${esc(row.period_end)}</span><strong>${row.skipped?'跳过该期':esc(row.deadline_display)}</strong>${row.adjusted?`<small>由 ${esc(row.original_date)} 顺延</small>`:''}</li>`).join('')}</ol>`:''}${data.calendar_error?`<p class="schedule-warning">${esc(data.calendar_error)}</p>`:''}${data.warnings?.length?`<p class="schedule-warning">${esc(data.warnings.join('；'))}。未覆盖日期不会按普通工作日自动判断。</p>`:''}${data.calendar_source?`<a href="${esc(data.calendar_source)}" target="_blank" rel="noopener">查看已维护的 2026 年法定节假日及调休依据 ↗</a>`:''}`;
  }catch(error){if(version===ruleScheduleVersions[prefix])output.textContent=error.message || '日程计算失败，请检查日期和天数';}
}
