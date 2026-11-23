/* Main business flows. Global functions remain available to existing page actions. */
const $ = id => document.getElementById(id);
let selectedExt = '', sortMode = 'relevance', lastResults = [], currentFileId = null, workbookInfo = null, lastAnalysis = null, chartType = 'bar', activeTab = 'pivot', filterSerial = 0;
let dateFrom = '', dateTo = '', dateField = 'mtime';
let currentPage = 1, pageSize = 10, totalCandidates = 0, totalPages = 0, currentQuery = '';
let matchMode = 'all', searchTarget = 'title';
const AGG_LABEL = { sum: 'SUM 求和', count: 'COUNT 计数', mean: 'AVG 平均值', median: 'MEDIAN 中位数', min: 'MIN 最小值', max: 'MAX 最大值', nunique: 'DISTINCT COUNT 去重计数' };
const OPERATORS = [['contains', '包含'], ['equals', '等于'], ['not_equals', '不等于'], ['gt', '大于'], ['gte', '大于等于'], ['lt', '小于'], ['lte', '小于等于'], ['is_empty', '为空'], ['not_empty', '不为空']];

// ============================================================
// 深模块 1：Toast 全局通知中心 (Deep Module: Toast Notification Center)
// 满足 codebase-design: 小接口 (toast), 深实现 (自动图标/队列/动效)
// ============================================================
const Toast = {
  show(message, type = 'info', duration = 2800) {
    const container = $('toastContainer') || document.body;
    const pill = document.createElement('div');
    const icons = {
      success: 'i-check-circle',
      warn: 'i-alert',
      error: 'i-x',
      info: 'i-info'
    };
    pill.className = `toast-pill toast-${type}`;
    pill.innerHTML = `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#${icons[type] || 'i-info'}"/></svg><span>${esc(message)}</span>`;
    container.appendChild(pill);

    setTimeout(() => {
      pill.classList.add('toast-hiding');
      setTimeout(() => {
        if (pill.parentNode) pill.parentNode.removeChild(pill);
      }, 200);
    }, duration);
  },
  success(msg, dur) { this.show(msg, 'success', dur); },
  warn(msg, dur) { this.show(msg, 'warn', dur); },
  error(msg, dur) { this.show(msg, 'error', dur); },
  info(msg, dur) { this.show(msg, 'info', dur); }
};

function toast(t, type = 'info') {
  if (typeof t === 'string') {
    if (t.includes('成功') || t.includes('已完成') || t.includes('关闭') || t.includes('已复制') || t.includes('已生效')) {
      type = 'success';
    } else if (t.includes('失败') || t.includes('错误') || t.includes('驳回') || t.includes('超期')) {
      type = 'error';
    } else if (t.includes('注意') || t.includes('请') || t.includes('等待')) {
      type = 'warn';
    }
  }
  Toast.show(t, type);
}

// ============================================================
// 深模块 2：全局键盘快捷键总线 (Deep Module: KeyboardShortcuts)
// 极客交互支持：Cmd+K / / (聚焦搜索), 1/2/3 (切换视图), ESC (退出弹窗)
// ============================================================
const KeyboardManager = {
  init() {
    window.addEventListener('keydown', (e) => {
      const activeTag = document.activeElement ? document.activeElement.tagName.toLowerCase() : '';
      const isInput = (activeTag === 'input' || activeTag === 'textarea' || activeTag === 'select');

      // 1. Cmd/Ctrl + K 或 / (快速聚焦搜索)
      if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') || (e.key === '/' && !isInput)) {
        e.preventDefault();
        switchNavMode('search');
        const q = $('q');
        if (q) {
          q.focus();
          q.select();
        }
        return;
      }

      // 2. Escape 分层退出
      if (e.key === 'Escape') {
        if (!$('monitoringTaskModal').classList.contains('hidden')) { closePortalMonitoringTask(); return; }
        const previewModal = $('imagePreviewModal');
        if (previewModal && !previewModal.classList.contains('hidden')) {
          closeImagePreview();
          return;
        }
        const reviewModal = $('adminReviewModal');
        if (reviewModal && !reviewModal.classList.contains('hidden')) {
          closeAdminReviewModal();
          return;
        }
        const submitModal = $('submitProofModal');
        if (submitModal && !submitModal.classList.contains('hidden')) {
          closeSubmitProofModal();
          return;
        }
        const ruleModal = $('createRuleModal');
        if (ruleModal && !ruleModal.classList.contains('hidden')) {
          closeCreateRuleModal();
          return;
        }
        const editRule = $('editRuleModal');
        if (editRule && !editRule.classList.contains('hidden')) {
          closeEditRuleModal();
          return;
        }
        const createUser = $('createUserModal');
        if (createUser && !createUser.classList.contains('hidden')) {
          closeCreateUserModal();
          return;
        }
        const resetPwd = $('resetPasswordModal');
        if (resetPwd && !resetPwd.classList.contains('hidden')) {
          closeResetPasswordModal();
          return;
        }
        const downloadM = $('downloadModal');
        if (downloadM && downloadM.style.display !== 'none' && downloadM.style.display === 'flex') {
          downloadM.style.display = 'none';
          return;
        }
        const filterPop = $('filterPopoverMenu');
        if (filterPop && !filterPop.classList.contains('search-filters-inline') && !filterPop.classList.contains('hidden')) {
          filterPop.classList.add('hidden');
          return;
        }
        const aiDrawer = $('aiDrawerOverlay');
        if (aiDrawer && !aiDrawer.classList.contains('hidden')) {
          toggleAIChat(false);
          return;
        }
      }

      // 3. 数字键 1-5 切换顶层视图 (仅在非输入状态生效)
      if (!isInput && !e.metaKey && !e.ctrlKey && !e.altKey) {
        if (e.key === '1') { switchNavMode('search'); toast('快捷切换：首页检索'); }
        else if (e.key === '2') { switchNavMode('catalog'); toast('快捷切换：组织架构'); }
        else if (e.key === '3') { switchNavMode('archive'); toast('快捷切换：归档合规'); }
        else if (e.key === '4') { switchNavMode('approval'); toast('快捷切换：下载审批'); }
        else if (e.key === '5') { switchNavMode('permission'); toast('快捷切换：人员权限'); }
      }
    });
  }
};

// ============================================================
// 深模块 3：流光骨架屏管理 (Deep Module: SkeletonUI)
// 解决加载生硬问题，提升系统高级感与首屏预期
// ============================================================
const SkeletonUI = {
  renderTaskCards(container, count = 4) {
    if (!container) return;
    let html = '';
    for (let i = 0; i < count; i++) {
      html += `
        <div class="skeleton-task-card">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div class="skeleton-box" style="width:110px;height:20px"></div>
            <div class="skeleton-box" style="width:130px;height:24px;border-radius:999px"></div>
          </div>
          <div class="skeleton-box" style="width:70%;height:22px;margin-top:6px"></div>
          <div class="skeleton-box" style="width:90%;height:14px"></div>
          <div style="grid-template-columns:1fr 1fr;display:grid;gap:10px;margin-top:8px">
            <div class="skeleton-box" style="height:32px"></div>
            <div class="skeleton-box" style="height:32px"></div>
          </div>
          <div class="skeleton-box" style="width:100%;height:6px;border-radius:999px;margin-top:8px"></div>
          <div style="display:flex;justify-content:space-between;margin-top:12px">
            <div class="skeleton-box" style="width:140px;height:32px;border-radius:8px"></div>
            <div class="skeleton-box" style="width:100px;height:32px;border-radius:8px"></div>
          </div>
        </div>`;
    }
    container.innerHTML = html;
  }
};

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtBytes(n) {
  if (n == null || !Number.isFinite(Number(n))) return '—';
  n = Number(n);
  if (n === 0) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}
function fmtNumber(n) {
  if (n === null || n === undefined || n === '' || !Number.isFinite(Number(n))) return '—';
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}
function fmtDate(ts) {
  if (!ts || !Number.isFinite(Number(ts))) return {full:'—',rel:''};
  const d=new Date(Number(ts)*1000);
  if(Number.isNaN(d.getTime()))return {full:'—',rel:''};
  const format=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  const parts=date=>Object.fromEntries(format.formatToParts(date).map(p=>[p.type,p.value]));
  const p=parts(d),n=parts(new Date());
  const day=v=>Date.UTC(Number(v.year),Number(v.month)-1,Number(v.day));
  const diff=Math.round((day(n)-day(p))/86400000);
  return {full:`${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`,rel:({0:'今天',1:'昨天','-1':'明天'})[diff] || ''};
}
function renderFileTime(ts) {
  const info=fmtDate(ts);
  return `<span class="file-timestamp"><time>${esc(info.full)}</time>${info.rel?`<small class="file-time-label">${esc(info.rel)}</small>`:''}</span>`;
}

function extClass(ext) {
  ext = (ext || '').toLowerCase();
  if (ext === '.xlsx' || ext === '.xls') return 'xlsx';
  if (ext === '.csv' || ext === '.tsv') return 'csv';
  if (ext === '.xlsm') return 'xlsm';
  return 'other';
}

async function api(url, opts = {}) {
  const token = sessionStorage.getItem('fileFinderAccessToken') || '';
  const headers = { 'X-Product-Session': portalProductSessionId, ...(opts.headers || {}) };
  let body = opts.body;
  if (body !== undefined && !headers['Content-Type']) {
    if (typeof body === 'object' && !(body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    } else if (typeof body === 'string' && (body.trim().startsWith('{') || body.trim().startsWith('['))) {
      headers['Content-Type'] = 'application/json';
    }
  }
  if (token) headers['X-App-Token'] = token;

  const requestUrl = new URL(url, window.location.origin);
  const isAiRequest = requestUrl.pathname === '/api/ai/chat' ||
    (requestUrl.pathname === '/api/search' && requestUrl.searchParams.get('enable_ai') === 'true');
  const timeoutMs = opts.timeoutMs || (isAiRequest ? 120000 : 15000);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, { ...opts, headers, body, signal: controller.signal });
    const data = (res.headers.get('content-type') || '').includes('application/json') ? await res.json() : await res.text();
    const rawDetail = data?.detail || data?.message;
    const errText = typeof rawDetail === 'object' ? (Array.isArray(rawDetail) ? rawDetail.map(d => d.msg || JSON.stringify(d)).join('; ') : JSON.stringify(rawDetail)) : rawDetail;
    if (res.status === 401 && String(errText || '').includes('登录')) showLoginOverlay();
    if (!res.ok) throw new Error(errText || `服务响应异常 (${res.status})`);
    return data;
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error(isAiRequest
        ? '回答等待超时，请稍后重试，或改用文件名关键词检索。'
        : `请求超时 (${timeoutMs / 1000}s)，请检查网络连接或稍后重试`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/* 现代化分页条渲染 */
function renderPagination() {
  const c = $('paginationContainer');
  if (!c) return;
  if (!totalCandidates || totalPages <= 1) {
    c.innerHTML = totalCandidates ? `<div class="pagination-bar"><div class="page-info">共 <strong>${totalCandidates}</strong> 份严格匹配表格</div></div>` : '';
    return;
  }

  const maxButtons = 5;
  let startPage = Math.max(1, currentPage - 2);
  let endPage = Math.min(totalPages, startPage + maxButtons - 1);
  if (endPage - startPage < maxButtons - 1) {
    startPage = Math.max(1, endPage - maxButtons + 1);
  }

  let pageButtons = '';
  for (let p = startPage; p <= endPage; p++) {
    pageButtons += `<button class="page-btn ${p === currentPage ? 'active' : ''}" onclick="gotoPage(${p})">${p}</button>`;
  }

  c.innerHTML = `
    <div class="pagination-bar">
      <div class="page-info">
        第 <strong>${currentPage}</strong> / <strong>${totalPages}</strong> 页 · 共 <strong>${totalCandidates}</strong> 份严格包含表格 (每页 10 份)
      </div>
      <div class="page-actions">
        <button class="page-btn" ${currentPage <= 1 ? 'disabled' : ''} onclick="gotoPage(1)" title="第一页">«</button>
        <button class="page-btn" ${currentPage <= 1 ? 'disabled' : ''} onclick="gotoPage(${currentPage - 1})">上一页</button>
        ${pageButtons}
        <button class="page-btn" ${currentPage >= totalPages ? 'disabled' : ''} onclick="gotoPage(${currentPage + 1})">下一页</button>
        <button class="page-btn" ${currentPage >= totalPages ? 'disabled' : ''} onclick="gotoPage(${totalPages})" title="末页">»</button>
      </div>
    </div>`;
}

function gotoPage(p) {
  if (p < 1 || p > totalPages || p === currentPage) return;
  search(p).then(() => {
    const el = $('resultsCard');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

/* 渲染搜索结果与【突出体现搜索时间】 */
let indexedTotalCache = 0;
function renderResults(data) {
  portalSearchEventId = data.search_event_id || null;
  lastResults = data.results || [];
  currentPage = data.page || 1;
  totalCandidates = data.total_candidates || lastResults.length;
  totalPages = data.total_pages || (totalCandidates > 0 ? Math.ceil(totalCandidates / pageSize) : 0);

  hideHomeCockpit();
  selectedResultIndex = -1;
  // 搜索结果摘要
  const banner = $('searchMetricsBanner');
  banner.classList.remove('hidden');
  const elapsed = Math.max(0.1, Number(data.elapsed_ms || 0) / 1000).toFixed(1);
  $('metricsElapsed').textContent = elapsed;
  const isPacedDemo = Number(data.demo_delay_ms || 0) > 0;
  const timingLabel = isPacedDemo ? '演示查询' : '本次查询';
  $('metricsTimingLabel').textContent = timingLabel;
  $('metricsTiming').title = isPacedDemo
    ? `${data.ai_used ? '包含 AI 找表演示等待，通常约 5～10 秒' : '包含演示查询等待，通常约 2～4 秒'}；实际环境以现场实测为准。`
    : '本次服务端查询耗时，随数据、负载和环境变化，供排查参考';
  $('metricsSummary').textContent = !lastResults.length && !data.ai_used ? '未找到匹配文件，可调整关键词或筛选条件。' : (data.message || '已按所选条件检索');
  $('metricsDetails').textContent = `在 ${indexedTotalCache.toLocaleString()} 个可访问索引文件中找到 ${totalCandidates} 个结果 · 当前第 ${currentPage} / ${totalPages || 1} 页`;

  const modeLabel = matchMode === 'any' ? '宽松匹配' : '严格匹配';
  $('resultTitle').innerHTML = data.query ? `搜索结果 <span style="font-size:12px;color:var(--muted);font-weight:400">（“${esc(data.query)}” · ${modeLabel}）</span>` : '筛选结果';
  $('resultMeta').textContent = `共 ${totalCandidates} 份表格 · 本页呈现 ${lastResults.length} 份 · ${isPacedDemo ? '演示查询（含节奏等待）' : '本次服务端查询'} ${elapsed} 秒`;

  // 已激活筛选条件 chips（点击单个移除）
  const chips = [];
  if (selectedExt) chips.push({ key: 'ext', label: '格式 ' + selectedExt.toUpperCase() });
  if (dateFrom || dateTo) {
    const quickKey = matchQuickRange(dateFrom, dateTo);
    const quickLabel = DATE_QUICK_LABELS[quickKey];
    chips.push({ key: 'date', label: quickLabel || `${dateFrom || '不限'} ~ ${dateTo || '至今'}` });
  }
  if (matchMode === 'any') chips.push({ key: 'mode', label: '宽松匹配' });
  const chipsEl = $('activeFilterChips');
  if (chipsEl) {
    chipsEl.innerHTML = chips.map(c => `
      <button type="button" class="active-filter-chip" onclick="removeActiveFilter('${c.key}')" title="点击移除该筛选条件">
        ${c.label}
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>`).join('');
  }

  if (!lastResults.length) {
    if (data.ai_guess && data.ai_used) {
      const g = data.ai_guess;
      $('results').innerHTML = `
        <div class="ai-guess-card">
          <div class="ai-guess-head">
            <svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="color:var(--brand)"><use href="#i-sparkles"/></svg>
            <div>
              <div style="font-weight:600;color:var(--brand);font-size:14px">AI 智能推测补齐与目录定位诊断</div>
              <div style="font-size:11.5px;color:var(--muted);margin-top:2px">参考来源：${esc(g.model_used || '来源未标注')} · 根据已有目录与命名规律给出建议，请核对文件</div>
            </div>
          </div>
          <div class="ai-guess-body" style="background:#ffffff;padding:12px 14px;border-radius:8px;border:1px solid var(--line)">
            ${esc(g.reasoning).replace(/\n/g, '<br>')}
          </div>
          ${g.suggested_terms?.length ? `
            <div class="ai-guess-tags" style="margin-top:12px">
              <span style="font-size:12px;color:var(--ink-secondary);font-weight:600">建议点击重搜词：</span>
              ${g.suggested_terms.map(t => `<button class="chip-btn" style="background:#285649;color:#fff;border:none;font-weight:600" onclick="$('q').value='${esc(t)}';search()">${esc(t)}</button>`).join('')}
            </div>
          ` : ''}
          ${g.matched_files?.length ? `
            <div style="margin-top:14px;font-size:12px;color:var(--ink-secondary);font-weight:600;display:flex;align-items:center;gap:4px"><svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-folder"/></svg> 为您推测的相近或模糊关联文件：</div>
            <div style="display:flex;flex-direction:column;gap:6px;margin-top:6px">
              ${g.matched_files.map(f => `
                <div style="display:flex;justify-content:space-between;align-items:center;background:#fff;padding:8px 12px;border:1px solid var(--line);border-radius:6px">
                  <div>
                    <span style="font-size:12.5px;color:var(--ink);font-weight:600">${esc(f.name)}</span>
                    <div style="font-size:10.5px;color:var(--muted)">${esc(f.parent)}</div>
                  </div>
                  <div style="display:flex;gap:6px">
                    <button class="copy-path-btn" onclick="startAnalysis(${f.id})">直接分析</button>
                    <button class="copy-path-btn" onclick="startDownload(${f.id}, this.parentElement.previousElementSibling.querySelector('span').textContent)">下载原文件</button>
                  </div>
                </div>
              `).join('')}
            </div>
          ` : ''}
          <div style="margin-top:14px;display:flex;justify-content:space-between;align-items:center">
            <span style="font-size:11.5px;color:var(--muted)">若想进一步探究文件归属，可随时与 AI 对话助手交流</span>
            <button class="chip-btn" style="background:#1e443a;color:#fff;border:none;font-weight:600" onclick="toggleAIChat(true)">打开 AI 对话框深度交流</button>
          </div>
        </div>`;
    } else {
      const hasFilters = selectedExt || dateFrom || dateTo;
      $('results').innerHTML = `
        <div style="padding:60px 20px;text-align:center;color:var(--muted)">
          <svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:32px;height:32px;color:var(--faint);margin-bottom:10px"><use href="#i-search"/></svg>
          <div style="font-size:14px;font-weight:600;color:var(--ink);margin-bottom:6px">未找到匹配的报表</div>
          <div style="font-size:12px;color:var(--muted);margin-top:6px">试试更短的关键词，调整格式或日期筛选，也可以请 AI 助手协助。</div>
          <div style="margin-top:16px;display:flex;justify-content:center;gap:8px">
            ${hasFilters ? '<button class="chip-btn" onclick="resetAllFilters()">清除筛选重搜</button>' : ''}
            <button class="chip-btn" style="background:#285649;color:#fff;border:none" onclick="toggleAIChat(true)">唤起 AI 助手</button>
          </div>
        </div>`;
    }
    renderPagination();
    return;
  }

  $('results').innerHTML = lastResults.map((r, i) => {
    const timeInfo = fmtDate(r.mtime);
    const cls = extClass(r.ext);
    const hasData = r.size > 0;
    const scoreVal = r.score ? '按索引相关性排序' : '';
    return `
      <article class="result-card" data-idx="${i}" onclick="selectResult(${i}, true)">
        <div class="file-badge-icon ${cls}">${esc((r.ext || 'FILE').replace('.', '').toUpperCase())}</div>
        <div class="card-main">
          <div class="card-title-row">
            <button type="button" class="card-file-name" title="${esc(r.name)}" onclick="event.stopPropagation();selectResult(${i}, true)">${highlightPortalQuery(r.name)}</button>
          </div>
          <div class="card-path-row">
            <span title="${esc(r.unc_path || r.path)}">${esc(r.unc_path || r.path)}</span>
          </div>
          <div class="card-meta-row">
            <span class="meta-time">${renderFileTime(r.mtime)}</span>
            <span>${fmtBytes(r.size)}</span>
            ${hasData ? '<span class="badge-real-data">非空文件</span>' : ''}
            ${scoreVal ? `<span class="score-badge">${scoreVal}</span>` : ''}
            <span class="match-tag">${esc(r.match_reason || '全词包含')}</span>
          </div>
        </div>
        <div class="card-actions">
          <button class="btn-action-secondary" onclick="event.stopPropagation();previewFile(${r.id})" title="浏览器内在线预览"><svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-eye"/></svg></button>
          <button class="btn-action-secondary" onclick="event.stopPropagation();locateInCatalog('${esc(r.parent || r.path)}')" title="在组织架构树中定位"><svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-network"/></svg></button>
          <button class="btn-action-secondary" onclick="event.stopPropagation();handleSearchDownload(${i})" title="申请或直接下载"><svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-download"/></svg></button>
          <button class="btn-action-secondary" onclick="event.stopPropagation();copyUncPath(${i})" title="复制网络共享路径 (UNC)"><svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-copy"/></svg></button>
          <button class="btn-action-secondary" onclick="event.stopPropagation();copyPath(${i})" title="复制本机路径"><svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-link"/></svg></button>
        </div>
      </article>`;
  }).join('');

    renderPagination();
  hideHomeCockpit();
  if (lastResults.length > 0) {
    selectResult(0);
  } else {
    selectedResultIndex = -1;
  }
}

async function search(page = 1) {
  if (currentNavMode !== 'search') switchNavMode('search');
  else window.FileWorkspace?.showSource('search');
  const q = $('q').value.trim();
  hideSearchHistory();
  currentPage = (typeof page === 'number' && !isNaN(page) && page >= 1) ? Math.floor(page) : 1;
  currentQuery = q;
  const btn = $('searchBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-anim"></span> 检索中…';
  }
  // 检索前注入骨架行
  hideHomeCockpit();
  selectedResultIndex = -1;
  const resultsEl = $('results');
  if (resultsEl) {
    resultsEl.innerHTML = Array.from({ length: 5 }).map(() => `
      <div style="display:flex;align-items:center;gap:14px;padding:14px 18px;border-bottom:1px solid var(--line)">
        <div class="skeleton-box" style="width:36px;height:24px"></div>
        <div style="flex:1;display:flex;flex-direction:column;gap:6px">
          <div class="skeleton-box" style="width:55%;height:14px"></div>
          <div class="skeleton-box" style="width:35%;height:10px"></div>
        </div>
      </div>`).join('');
  }
  try {
    const p = new URLSearchParams({
      q,
      page: String(currentPage),
      page_size: String(pageSize),
      ext: selectedExt,
      sort: sortMode,
      match_mode: matchMode,
      target: searchTarget,
      enable_ai: String(Boolean(q) && portalAiEnabled)
    });
    appendPortalExtraFilters(p);
    if (dateFrom) p.set('date_from', dateFrom);
    if (dateTo) p.set('date_to', dateTo);
    if (dateField) p.set('date_field', dateField);
    const data = await api('/api/search?' + p);
    recordSearchHistory(q);
    renderResults(data);
  } catch (e) {
    const msg = e.message || '搜索服务异常';
    toast(msg);
    if (resultsEl) {
      resultsEl.innerHTML = `
        <div style="padding:56px 20px;text-align:center;color:var(--muted)">
          <svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:32px;height:32px;color:var(--danger);margin-bottom:10px"><use href="#i-alert"/></svg>
          <div style="font-size:14px;font-weight:600;color:var(--ink);margin-bottom:6px">检索失败</div>
          <div style="font-size:12px;color:var(--muted);margin-top:4px">${esc(msg)}</div>
          <div style="margin-top:16px"><button class="chip-btn" onclick="search(currentPage)">重试</button></div>
        </div>`;
    }
  } finally {
    updateFilterToggleState();
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '搜索';
    }
  }
}

/* ===== 搜索历史（localStorage，上限 8 条） ===== */
const SEARCH_HISTORY_KEY = 'ff_search_history';
function getSearchHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(SEARCH_HISTORY_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter(x => typeof x === 'string' && x.trim()).slice(0, 8) : [];
  } catch { return []; }
}
function recordSearchHistory(q) {
  if (!q) return;
  let list = getSearchHistory().filter(x => x !== q);
  list.unshift(q);
  list = list.slice(0, 8);
  try { localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(list)); } catch { /* 隐私模式忽略 */ }
}
function removeSearchHistoryItem(idx) {
  const list = getSearchHistory();
  list.splice(idx, 1);
  try { localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(list)); } catch { }
  renderSearchHistory();
}
function renderSearchHistory() {
  const dd = $('searchHistoryDD');
  if (!dd) return;
  const list = getSearchHistory();
  if (!list.length) {
    dd.innerHTML = '<div class="search-history-empty">暂无搜索历史，检索过的关键词会自动记录在这里</div>';
    return;
  }
  dd.innerHTML = list.map((h, i) => `
    <div class="search-history-item" data-i="${i}">
      <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--muted);flex:none"><use href="#i-clock"/></svg>
      <span class="sh-text" title="${esc(h)}">${esc(h)}</span>
      <button type="button" class="sh-remove" data-remove="${i}" title="删除此条记录"><svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-x"/></svg></button>
    </div>`).join('');
  dd.querySelectorAll('.search-history-item').forEach(item => {
    item.addEventListener('click', (e) => {
      if (e.target.closest('[data-remove]')) { e.stopPropagation(); removeSearchHistoryItem(Number(item.dataset.i)); return; }
      const list = getSearchHistory();
      const text = list[Number(item.dataset.i)];
      if (!text) return;
      $('q').value = text;
      const clr = $('clearSearch');
      if (clr) clr.style.visibility = 'visible';
      hideSearchHistory();
      search(1);
    });
  });
}
function showSearchHistory() { renderSearchHistory(); $('searchHistoryDD')?.classList.remove('hidden'); }
function hideSearchHistory() { $('searchHistoryDD')?.classList.add('hidden'); }
function initSearchHistoryUI() {
  const q = $('q');
  if (!q || !$('searchHistoryDD')) return;
  q.addEventListener('focus', () => { if (!q.value.trim()) showSearchHistory(); });
  q.addEventListener('input', () => { if (q.value.trim()) hideSearchHistory(); else renderSearchHistory(); });
  document.addEventListener('click', (e) => {
    const dd = $('searchHistoryDD');
    if (!dd || dd.classList.contains('hidden')) return;
    if (!dd.contains(e.target) && !e.target.closest('.command-search-frame')) hideSearchHistory();
  });
}

/* 移除单条已激活筛选（结果头部 chips 使用） */
function removeActiveFilter(key) {
  if (key === 'ext') setExtFilter('');
  else if (key === 'date') applyDateQuick('all');
  else if (key === 'mode') selectMatchMode('all');
}

/* 筛选非默认时：按钮品牌蓝描边 + 计数徽章 */
function updateFilterToggleState() {
  const btn = $('filterPopoverToggle');
  if (!btn) return;
  let count = 0;
  if (selectedExt) count++;
  if (dateFrom || dateTo) count++;
  btn.classList.toggle('has-active-filters', count > 0);
  let badge = btn.querySelector('.filter-count-badge');
  if (count > 0) {
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'filter-count-badge';
      btn.appendChild(badge);
    }
    badge.textContent = count;
  } else if (badge) {
    badge.remove();
  }
}

/* 重置全部筛选 */
function resetAllFilters() {
  resetPortalExtraFilters();
  selectedExt = '';
  matchMode = 'all';
  searchTarget = 'title';
  $('btnFilterAnd')?.classList.add('active');
  $('btnFilterOr')?.classList.remove('active');
  document.querySelectorAll('.popover-ext-btn').forEach(b => b.classList.toggle('active', !b.dataset.ext));
  dateFrom = ''; dateTo = '';
  if ($('dateFromInput')) $('dateFromInput').value = '';
  if ($('dateToInput')) $('dateToInput').value = '';
  refreshDateQuickTags('all');
  _updateDateHint('', '', 'all');
  syncQuickFilterStrip();
  updateFilterToggleState();
  toggleFilterPopover(false);
  toast('已重置全部筛选条件');
  search(1);
}

async function copyPath(i) {
  const r = lastResults[i];
  if (!r) return;
  try {
    await navigator.clipboard.writeText(r.path);
    recordPortalEvent('path_copy', r.id);
    toast('服务器绝对路径已复制到剪贴板');
  } catch {
    prompt('复制服务器文件路径：', r.path);
  }
}

// 是否为服务器本机访问。跨机器永远调不起对方电脑上的 Excel/WPS，这是浏览器的安全边界。
function isLocalHost() {
  return ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
}

async function copyCatalogPath(i) {
  const f = (catalogState.pageFiles || [])[i];
  if (!f) return;
  copyPathDirect(f.path);
}

// ---------- 登录认证与下载审批 ----------
let currentUser = null;
let previewEnabled = false;

function isAdminUser() {
  return currentUser && (currentUser.role === 'super_admin' || currentUser.role === 'dept_admin');
}

function showLoginOverlay() {
  window.FileWorkspace?.clear();
  const el = $('loginOverlay');
  if (el) el.style.display = 'flex';
}
function hideLoginOverlay() {
  const el = $('loginOverlay');
  if (el) el.style.display = 'none';
}

async function checkAuth() {
  try {
    const res = await api('/api/auth/me');
    currentUser = res.user;
    previewEnabled = !!res.preview_enabled;
    hideLoginOverlay();
    applyUserUI();
  } catch (e) {
    currentUser = null;
    showLoginOverlay();
    return false;
  }
  try { await window.FileWorkspace?.restoreRoute(); }
  catch (error) { toast(error.message || '页面加载失败，请重试'); }
  return true;
}

function applyUserUI() {
  const pill = $('userPill');
  if (!pill) return;
  if (currentUser) {
    pill.style.display = 'flex';
    $('userPillAvatar').textContent = (currentUser.name || '?').charAt(0);
    $('userPillName').textContent = currentUser.name;
    $('userPillRole').textContent = { super_admin: '超级管理员', dept_admin: '部门管理员', user: '成员' }[currentUser.role] || currentUser.role;
  } else {
    pill.style.display = 'none';
  }
  applyPortalUserRole();
  // 非管理员隐藏"人员与账号权限"入口（该页接口本身也已被后端限制为管理员）
  const permTab = $('navPermTab');
  if (permTab) permTab.style.display = currentUser?.role === 'super_admin' ? '' : 'none';
  // 标记当前访问方式（本机 / 公网），供按钮提示与后续样式扩展使用；按钮本身不隐藏
  document.body.classList.toggle('remote-mode', !isLocalHost());
}

async function doLogin() {
  const phone = $('loginPhone').value.trim();
  const password = $('loginPassword').value.trim();
  $('loginError').textContent = '';
  if (!phone || !password) { $('loginError').textContent = '请输入手机号和密码'; return; }
  try {
    const res = await api('/api/auth/login', { method: 'POST', body: { phone, password } });
    currentUser = res.user;
    hideLoginOverlay();
    applyUserUI();
    showHomeCockpit();
    await window.FileWorkspace?.restoreRoute();
    initHomeDataCockpit();
    loadArchiveOverview();

    loadStatus(); loadRecent();
  } catch (e) {
    $('loginError').textContent = e.message || '登录失败';
  }
}

async function doLogout() {
  window.FileWorkspace?.clear();
  // 退出时清除本机记住的账号，便于切换账号
  try { localStorage.removeItem('ff_saved_login'); } catch {}
  try { await api('/api/auth/logout', { method: 'POST' }); } catch {}
  location.reload();
}

// ---------- 下载：按当前文件夹权限执行 ----------
async function startDownload(fileId, fileName) {
  if (!currentUser) { showLoginOverlay(); return; }
  try {
    const access = await api(`/api/file/${fileId}/permissions`);
    if (access.download === 'deny') { toast('你没有此文件的下载权限'); return; }
    if (access.download === 'direct') { await saveApprovedDownload(fileId); return; }
    const res = await api('/api/downloads/requests', { method: 'POST', body: { file_id: fileId, reason: '', search_event_id: portalSearchEventId } });
    if (res.status === 'approved' && res.ticket) {
      await saveApprovedDownload(fileId, res.ticket);
      toast('已有有效授权，开始下载');
    } else {
      toast('该文件的下载申请正在审批中，可在下载审批中心查看进度');
    }
  } catch (e) {
    if ((e.message || '').includes('用途')) {
      $('downloadFileId').value = fileId;
      $('downloadFileName').textContent = fileName;
      $('downloadReason').value = '';
      $('downloadModal').style.display = 'flex';
    } else {
      toast(e.message || '操作失败');
    }
  }
}

function handleSearchDownload(i) {
  const r = lastResults[i];
  if (r) startDownload(r.id, r.name);
}

function handleCatalogDownload(idx) {
  const f = (catalogState.pageFiles || [])[idx];
  if (f) startDownload(f.id, f.name);
}

async function submitDownloadRequest() {
  const fileId = Number($('downloadFileId').value);
  const reason = $('downloadReason').value.trim();
  if (!reason) { toast('请填写下载用途'); return; }
  try {
    const res = await api('/api/downloads/requests', { method: 'POST', body: { file_id: fileId, reason, search_event_id: portalSearchEventId } });
    $('downloadModal').style.display = 'none';
    if (res.status === 'approved' && res.ticket) {
      await saveApprovedDownload(fileId, res.ticket);
      toast('已获授权，开始下载');
    } else {
      toast('下载申请已提交，等待管理员审批');
    }
    if (currentNavMode === 'approval') loadApprovalCenter();
  } catch (e) { toast(e.message); }
}

// ---------- 在线预览（服务端限行解析，浏览器接收分页数据） ----------
function previewFile(fileId) {
  recordPortalEvent('preview_open', fileId);
  const file = lastResults.find(item => Number(item.id) === Number(fileId))
    || (catalogState.pageFiles || []).find(item => Number(item.id) === Number(fileId));
  window.FileWorkspace.open(fileId, file?.name);
}

// ---------- 下载审批中心 ----------
function initApprovalCenter() { return loadApprovalCenter(); }

async function loadApprovalCenter() {
  const wrap = $('approvalTableWrap');
  if (!wrap) return;
  wrap.innerHTML = '<div style="padding:50px;text-align:center;color:#6f736b">加载中…</div>';
  const admin = isAdminUser();
  $('approvalTitle').textContent = admin ? '下载审批（管理员视角 · 全部申请）' : '我的下载申请';
  try {
    const res = await api(`/api/downloads/requests?scope=${admin ? 'all' : 'mine'}`);
    const dot = $('navApprovalDot');
    if (dot) dot.style.display = (admin && res.pending_count > 0) ? '' : 'none';
    renderApprovalTable(res.requests || []);
  } catch (e) {
    wrap.innerHTML = `<div style="padding:50px;text-align:center;color:#dc2626">${esc(e.message || '加载失败')}</div>`;
  }
}

let approvalRows = [];
let approvalFilter = 'all';
const approvalStateLabels = {pending:'待审核',reviewing:'审核中',ready:'已通过 · 待下载',downloaded:'已完成下载',rejected:'已驳回',expired:'已过期 · 未下载'};
function approvalState(r) {
  if(r.status==='pending') return r.workflow_events?.some(e=>e.kind==='reviewing')?'reviewing':'pending';
  if(r.status==='rejected')return 'rejected';
  if(r.downloaded_at)return 'downloaded';
  return r.expired?'expired':'ready';
}
function setApprovalFilter(value){approvalFilter=value;renderApprovalTable(approvalRows);}
function renderApprovalTable(list) {
  approvalRows=list;
  const wrap=$('approvalTableWrap'),admin=isAdminUser();
  if(!list.length){wrap.innerHTML='<div class="portal-empty">暂无下载申请记录，可从文件列表发起申请。</div>';return;}
  const counts={};list.forEach(r=>{const state=approvalState(r);counts[state]=(counts[state]||0)+1;});
  const filters=[['all','全部'],...Object.entries(approvalStateLabels)];
  const visible=approvalFilter==='all'?list:list.filter(r=>approvalState(r)===approvalFilter);
  wrap.innerHTML=`<div class="approval-state-filters">${filters.map(([key,label])=>`<button class="${approvalFilter===key?'active':''}" onclick="setApprovalFilter('${key}')">${esc(label)}<span>${key==='all'?list.length:(counts[key]||0)}</span></button>`).join('')}</div>
    <table class="ap-table workflow-approval-table"><thead><tr>${admin?'<th>申请人</th>':''}<th>申请文件 / 用途</th><th>流程状态</th><th>审批与下载进度</th><th>操作</th></tr></thead><tbody>${visible.map(r=>{
      const state=approvalState(r),pending=r.status==='pending';
      const review=r.reviewed_at?`${r.reviewed_by} · ${r.reviewed_at_display}`:(state==='reviewing'?'业务负责人正在核实用途':'等待管理员处理');
      const progress=r.downloaded_at?`下载完成：${r.downloaded_at_display}`:r.status==='approved'?`授权截止：${r.expires_at_display}`:r.status==='rejected'?'申请未通过，需重新提交':r.review_note || '用途与访问范围核验';
      let actions=`<button class="btn-table-action" onclick="openApprovalDetail(${r.id})">查看流程</button><button class="btn-table-action" onclick="previewFile(${r.file_id})">预览</button>`;
      if(admin&&pending)actions+=`<button class="btn-table-action primary" onclick="reviewRequest(${r.id},'approve')">通过</button><button class="btn-table-action" onclick="reviewRequest(${r.id},'reject')">驳回</button>`;
      else if(!admin&&state==='ready'&&!r.is_demo)actions+=`<a class="btn-table-action primary" href="/api/file/${r.file_id}/download?ticket=${encodeURIComponent(r.ticket)}" download>下载</a>`;
      else if(state==='ready'||state==='downloaded')actions+=`<button class="btn-table-action" onclick="startDownload(${r.file_id},${esc(JSON.stringify(r.file_name))})">${r.is_demo&&!admin?'申请下载':'下载文件'}</button>`;
      return `<tr>${admin?`<td><b>${esc(r.user_name)}</b><small>${esc(r.department)}</small></td>`:''}<td><div class="approval-file-name">${esc(r.file_name)}</div><small class="approval-path" title="${esc(r.file_path)}">${esc((r.directory_display || r.file_path).split('/').filter(Boolean).map(portalOrganizationLabel).join(' / '))}</small><p class="approval-purpose">${esc(r.reason)}</p></td><td><span class="ap-status ${state}">${approvalStateLabels[state]}</span>${r.is_demo?'<small class="approval-demo-tag">演示记录</small>':''}<small>申请于 ${r.created_at_display}</small></td><td><div>${esc(review)}</div><small>${esc(progress)}</small>${r.review_note&&r.status!=='pending'?`<p class="approval-review-note">${esc(r.review_note)}</p>`:''}</td><td><div class="table-action-btns">${actions}</div></td></tr>`;
    }).join('')||`<tr><td colspan="${admin?5:4}" class="portal-empty">此状态暂无申请</td></tr>`}</tbody></table>`;
}
function closeApprovalDetail(){ $('approvalDetailModal')?.remove(); }
function openApprovalDetail(id){
  const r=approvalRows.find(row=>row.id===id);if(!r)return;
  closeApprovalDetail();
  let events=r.workflow_events?.length?r.workflow_events:[{kind:'submitted',at:r.created_at,actor:r.user_name,note:r.reason}];
  if(!r.workflow_events?.length&&r.reviewed_at)events.push({kind:r.status,at:r.reviewed_at,actor:r.reviewed_by,note:r.review_note});
  if(!r.workflow_events?.length&&r.downloaded_at)events.push({kind:'downloaded',at:r.downloaded_at,actor:r.user_name,note:'已下载原文件'});
  const eventLabels={submitted:'提交申请',reviewing:'进入审核',approved:'审批通过',rejected:'审批驳回',previewed:'在线预览',downloaded:'下载完成',expired:'授权过期'};
  const pending= r.status==='pending';
  document.body.insertAdjacentHTML('beforeend',`<div class="modal-overlay" id="approvalDetailModal" role="dialog" aria-modal="true" aria-labelledby="approvalDetailTitle"><div class="modal-window approval-detail-dialog"><div class="modal-header"><h3 id="approvalDetailTitle">下载申请 #${String(r.id).padStart(4,'0')}</h3><button class="modal-close-btn" aria-label="关闭流程详情" onclick="closeApprovalDetail()">×</button></div><div class="modal-body"><div class="approval-detail-file">${esc(r.file_name)}</div><span class="ap-status ${approvalState(r)}">${approvalStateLabels[approvalState(r)]}</span>${r.is_demo?'<span class="approval-demo-tag">演示流程 · 不产生真实下载授权</span>':''}<dl class="approval-detail-facts"><div><dt>申请人</dt><dd>${esc(r.user_name)} · ${esc(r.department)}</dd></div><div><dt>申请用途</dt><dd>${esc(r.reason)}</dd></div><div><dt>文件位置</dt><dd>${esc(r.file_path)}</dd></div></dl><h4 class="approval-timeline-heading">流程记录</h4><ol class="approval-timeline">${events.map(e=>`<li><div><b>${eventLabels[e.kind]||'处理记录'}</b><time>${esc(fmtDate(e.at).full)}</time></div><p>${esc(e.actor)}${e.note?' · '+esc(e.note):''}</p></li>`).join('')}</ol>${pending?'<div class="approval-next-step">下一步：核验用途和文件范围后，由管理员审批。</div>':''}</div><div class="modal-footer"><button class="tree-tool-btn" onclick="closeApprovalDetail();previewFile(${r.file_id})">查看文件</button>${isAdminUser()&&pending?`<button class="tree-tool-btn" onclick="closeApprovalDetail();reviewRequest(${r.id},'reject')">驳回</button><button class="btn-archive-primary" onclick="closeApprovalDetail();reviewRequest(${r.id},'approve')">审批通过</button>`:'<button class="tree-tool-btn" onclick="closeApprovalDetail()">关闭</button>'}</div></div></div>`);
}

async function reviewRequest(id, action) {
  try {
    const res = await api(`/api/downloads/requests/${id}/review`, { method: 'POST', body: { action } });
    toast(res.message || '已处理');
    loadApprovalCenter();
  } catch (e) { toast(e.message); }
}

async function copyUncPath(i) {
  const r = lastResults[i];
  if (!r) return;
  const target = r.unc_path || r.path;
  try {
    await navigator.clipboard.writeText(target);
    recordPortalEvent('path_copy', r.id);
    toast('Windows 共享路径已复制！可在资源管理器地址栏或运行(Win+R)中直接粘贴直达。');
  } catch {
    prompt('复制 Windows 共享网络路径：', target);
  }
}

async function copyTableData() {
  if (!lastAnalysis?.pivot?.rows?.length) {
    toast('当前没有可复制的交叉汇总数据');
    return;
  }
  const headers = lastAnalysis.pivot.headers || [];
  const rows = lastAnalysis.pivot.rows || [];
  const tsv = [headers.join('\t'), ...rows.map(r => r.join('\t'))].join('\n');
  try {
    await navigator.clipboard.writeText(tsv);
    toast('交叉汇总表已复制为表格数据，可直接在 Excel 中按 Ctrl+V 粘贴！');
  } catch {
    toast('复制失败，请使用右上角【导出 Excel】下载文件');
  }
}

function openPath(i) {
  const r = lastResults[i];
  if (!r) return;
  toast('已尝试定位路径；浏览器安全沙箱限制直接唤起，建议复制路径在文件资源管理器中粘贴打开。');
}

/* 状态加载 */
async function loadStatus() {
  try {
    const d = await api('/api/index/status');
    updatePortalStatus(d);
    if ($('indexedCount')) $('indexedCount').textContent = Number(d.indexed_files || 0).toLocaleString();
    if ($('indexedSize')) $('indexedSize').textContent = fmtBytes(d.indexed_size_bytes || 0);
    if ($('lastScan')) $('lastScan').textContent = portalDate(d.last_scan_at);
    if ($('rootDisplay')) $('rootDisplay').textContent = d.root_display || '未配置';
    if ($('aiState')) $('aiState').textContent = d.ai_configured ? '已配置 (就绪)' : '纯本地索引';
    // 动态更新顶部状态 pill 与检索指标中的总数
    const totalFiles = Number(d.indexed_files || 0);
    indexedTotalCache = totalFiles;
    if ($('serverStatusText')) $('serverStatusText').textContent = `已索引 ${totalFiles.toLocaleString()} 份`;
    if ($('metricsDetails') && !$('searchWorkspaceLayout').classList.contains('hidden')) {
      $('metricsDetails').textContent = `在 ${totalFiles.toLocaleString()} 个表格中匹配`;
    }
    // 如果后台扫描正在运行，自动启动进度轮询
    if (d.running && !_scanPollTimer) {
      _setIndexBtnSpinning(true);
      _pollScanState();
    }
  } catch (e) { portalStatusUnavailable(e); }
}

let _scanPollTimer = null;
function _setIndexBtnSpinning(spinning) {
  const btn = $('indexBtn');
  if (!btn) return;
  const svg = btn.querySelector('svg');
  if (svg) svg.style.animation = spinning ? 'spin 0.8s linear infinite' : 'none';
  btn.style.pointerEvents = spinning ? 'none' : 'auto';
  btn.style.opacity = spinning ? '0.6' : '1';
}

/* 手动触发增量扫描 */
async function startScan(mode='full', path='') {
  if (mode==='rebuild' && !confirm('重建期间可继续搜索，写入操作需等待。开始重建索引？')) return;
  if (_scanPollTimer) { toast('索引扫描正在进行中，请稍候…'); return; }
  try {
    const res = await api('/api/index/start?' + new URLSearchParams({mode,path}), { method: 'POST' });
    if (res.started) {
      toast(({full:'全量扫描已启动',directory:'目录同步已启动',rebuild:'索引重建已启动'})[mode]);
    } else {
      toast('索引扫描已在运行中，正在同步新文件…');
    }
    _setIndexBtnSpinning(true);
    _pollScanState();
  } catch (e) {
    toast('触发扫描失败：' + (e.message || '未知错误'));
    _setIndexBtnSpinning(false);
  }
}

function _pollScanState() {
  if (_scanPollTimer) clearTimeout(_scanPollTimer);
  _scanPollTimer = setTimeout(async () => {
    try {
      const d = await api('/api/index/status');
      updatePortalStatus(d);
      const statusEl = $('scanStatusText');
      if (d.running) {
        const seen = Number(d.seen || 0).toLocaleString();
        const indexed = Number(d.indexed || 0);
        // 在面板状态栏实时显示扫描进度
        if (statusEl) {
          statusEl.textContent = `扫描中：已检查 ${seen} 个，新增 ${indexed} 个`;
          statusEl.style.color = '#285649';
        }
        $('indexedCount').textContent = Number(d.indexed_files || 0).toLocaleString();
        _scanPollTimer = null;
        _pollScanState();
      } else {
        _setIndexBtnSpinning(false);
        _scanPollTimer = null;
        if (statusEl) {
          statusEl.textContent = d.message || '扫描已完成';
          statusEl.style.color = '#6f736b';
        }
        const indexed = Number(d.indexed || 0), updated = Number(d.updated || 0), deleted = Number(d.deleted || 0);
        if (['partial', 'error'].includes(d.phase)) toast(d.message || '扫描未完整完成，请检查目录访问状态', 'warn');
        else toast(`扫描完成：新增 ${indexed} 个，更新 ${updated} 个，删除 ${deleted} 个`);
        initHomeDataCockpit();
        loadStatus();
        loadRecent();
      }
    } catch (e) {
      _setIndexBtnSpinning(false);
      _scanPollTimer = null;
      const statusEl = $('scanStatusText');
      if (statusEl) { statusEl.textContent = '扫描状态获取失败，请刷新重试'; statusEl.style.color = '#b54708'; }
      portalStatusUnavailable(e);
    }
  }, 2000);
}

async function loadRecent() {
  const list = $('recentList');
  try {
    const data = await api('/api/recent?limit=6');
    const files = data.results || [];
    list.innerHTML = files.map((file, index) => {
      const date = fmtDate(file.mtime);
      return `<button class="recent-file-row" data-recent-index="${index}"><span>${esc(file.name)}</span><small>${esc(date.full)} · ${fmtBytes(file.size)}</small></button>`;
    }).join('') || '<div class="portal-empty">暂无最近更新文件</div>';
    list.querySelectorAll('[data-recent-index]').forEach(button => {
      button.onclick = () => {
        switchNavMode('search');
        clearPortalSearchFilters();
        $('q').value = files[Number(button.dataset.recentIndex)].name;
        search(1);
      };
    });
  } catch (error) {
    list.innerHTML = '<div class="portal-empty">暂时无法读取最近更新文件</div>';
  }
}

/* ============================================================
 * 现代 BI 仪表板编辑器 (Dashboard-First, Canvas-Driven) 核心交互引擎
 * ============================================================ */

/* ============================================================
 * 企业网盘组织架构全景看板 (Catalog Dashboard) 交互逻辑
 * ============================================================ */
let currentNavMode = 'search'; // 'search' | 'catalog'
let catalogState = {
  overview: null,
  tree: null,
  filterOnlyToday: false,
  filterKeyword: '',
  selectedNode: null, // { path, name, file_count, today_added }
  fileFilterMode: 'all', // 'all' | 'today'
  fileKeyword: '',
  page: 1,
  pageSize: 25,
  totalFiles: 0,
  sortBy: 'mtime', // 'mtime' | 'ctime' | 'name' | 'size'
  sortOrder: 'desc', // 'desc' | 'asc'
  expandedPaths: new Set()
};

function canOpenWorkspacePage(mode) {
  const pages = ['search', 'catalog', 'archive', 'approval', 'permission', 'sync'];
  return pages.includes(mode) && (mode !== 'permission' || currentUser?.role === 'super_admin');
}

// Page visibility is separate from initial data loading: activating an existing
// workspace tab keeps its filters, pagination and DOM without another request.
function activateWorkspacePage(mode) {
  currentNavMode = mode || '';
  const entries = { search: 'navSearchTab', catalog: 'navCatalogTab', archive: 'navArchiveTab', approval: 'navApprovalTab', permission: 'navPermTab', sync: 'navSyncTab' };
  Object.entries(entries).forEach(([page, id]) => {
    const active = page === mode;
    $(id)?.classList.toggle('active', active);
    $(id)?.setAttribute('aria-current', active ? 'page' : 'false');
    $(page + 'View')?.classList.toggle('hidden', !active);
  });
  togglePortalSidebar(false);
}

function initializeWorkspacePage(mode) {
  if (mode === 'catalog') return initCatalogDashboard();
  if (mode === 'archive') { renderPortalArchiveNotice(); return initArchiveDashboard(); }
  if (mode === 'approval') return initApprovalCenter();
  if (mode === 'permission') return initPermissionDashboard();
  if (mode === 'sync') return refreshPortalSync();
}

function switchNavMode(mode) {
  if (!canOpenWorkspacePage(mode)) return Promise.resolve(false);
  if (window.FileWorkspace) return window.FileWorkspace.showSource(mode);
  activateWorkspacePage(mode);
  return Promise.resolve(initializeWorkspacePage(mode));
}

async function initCatalogDashboard() {
  await Promise.all([
    loadCatalogOverview(),
    loadCatalogTree()
  ]);
}

function formatStorageSize(mb) {
  // 超过 1024MB 自动换算 GB，避免超长数字溢出 KPI 卡片
  const num = Number(mb) || 0;
  if (num >= 1024) {
    $('kpiTotalSizeUnit').textContent = 'GB';
    return (num / 1024).toLocaleString('zh-CN', { maximumFractionDigits: 1 });
  }
  $('kpiTotalSizeUnit').textContent = 'MB';
  return num.toLocaleString('zh-CN', { maximumFractionDigits: 1 });
}

async function loadCatalogOverview() {
  try {
    const res = await api('/api/catalog/overview');
    if (!res.ok) return;
    catalogState.overview = res;

    $('kpiTotalFiles').textContent = (res.total_files || 0).toLocaleString('zh-CN');
    $('kpiTodayAdded').textContent = `+${(res.today_added || 0).toLocaleString('zh-CN')}`;
    $('kpiDeptCount').textContent = (res.departments || []).length;
    $('kpiTotalSize').textContent = formatStorageSize(res.total_size_mb || 0);

    if (res.ext_distribution?.length) {
      const topExts = res.ext_distribution.slice(0, 3).map(e => `${e.ext.toUpperCase()} ${Math.round(e.count / res.total_files * 100)}%`).join(' · ');
      $('kpiExtDist').textContent = topExts;
    }
  } catch (err) {
    console.error('Failed to load catalog overview:', err);
  }
}

async function loadCatalogTree() {
  const container = $('catalogTreeContainer');
  container.innerHTML = `
    <div style="padding:40px;text-align:center;color:#6f736b;font-size:13px">
      <span class="spinner-anim" style="border-color:#285649;border-top-color:transparent;width:18px;height:18px"></span>
      <div style="margin-top:8px">正在构建组织架构树…</div>
    </div>`;

  try {
    const res = await api(`/api/catalog/tree?only_today_branch=${catalogState.filterOnlyToday}`);
    if (!res.ok) return;
    catalogState.tree = res.tree;

    // Keep departments collapsed on first visit, preserving deliberate expansion thereafter.
    if (res.tree) catalogState.expandedPaths.add(res.tree.path);

    renderCatalogTree();

    const previous = catalogState.selectedNode;
    const selected = previous ? findNodeByPath(res.tree, previous.path) : null;
    const initial = selected || res.tree;
    if (initial) selectTreeNode(initial.path, initial.name, initial.file_count, initial.today_added);

  } catch (err) {
    container.innerHTML = `<div style="padding:30px;color:#ef4444;text-align:center">加载组织架构树失败：${esc(err.message)}</div>`;
  }
}

function buildTreeNodeHtml(node, depth = 0, ancestorMatched = false) {
  const kw = catalogState.filterKeyword.trim().toLowerCase();
  const hasChildren = node.children && node.children.length > 0;
  const isExpanded = catalogState.expandedPaths.has(node.path);
  const isSelected = catalogState.selectedNode?.path === node.path;

  const matchesKw = ancestorMatched || !kw || node.name.toLowerCase().includes(kw);
  let visibleChildren = node.children || [];
  if (kw && !matchesKw) {
    visibleChildren = visibleChildren.filter(c => checkSubtreeMatch(c, kw));
  }

  if (kw && !matchesKw && visibleChildren.length === 0) {
    return '';
  }

  visibleChildren = visibleChildren.slice().sort((a,b) => a.name.localeCompare(b.name,'zh-CN',{numeric:true}));
  const icon = depth === 0
    ? '<svg class="icon org-root-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 21V4a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v17M9 21v-4h6v4M9 7h1m4 0h1M9 11h1m4 0h1" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>'
    : '<svg class="icon org-folder-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 6a2 2 0 0 1 2-2h5l2.2 2H20a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z" fill="currentColor"/></svg>';
  const todayBadge = node.today_added > 0 ? `<span class="tree-today-dot" title="今日更新 ${node.today_added} 份文件" aria-label="今日更新 ${node.today_added} 份"></span>` : '';
  const displayName = portalOrganizationLabel(node.name);

  const indentPx = depth * 18;

  // 注意：路径包含 Windows 反斜杠，不能内联进 onclick 字符串（会被 JS 转义损坏），
  // 统一通过 data-path 属性 + 事件代理处理点击。
  let html = `
    <div class="tree-node-item" data-path="${esc(node.path)}" data-depth="${depth}">
      <div class="tree-node-row ${isSelected ? 'selected' : ''}" tabindex="${isSelected || (depth === 0 && !catalogState.selectedNode) ? 0 : -1}" role="treeitem" aria-level="${depth+1}" aria-selected="${isSelected}" ${hasChildren ? `aria-expanded="${isExpanded || !!kw}"` : ''}
           style="padding-left:${indentPx + 6}px">
        <span class="tree-toggle-arrow ${hasChildren ? (isExpanded ? 'expanded' : '') : 'leaf'}"><svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-chevron-right"/></svg></span>
        <span class="tree-node-icon">${icon}</span>
        <span class="tree-node-title" title="${esc(node.name)}">${esc(displayName)}</span>
        <span class="badge-file-count" title="总文件数">${(node.file_count || 0).toLocaleString('zh-CN')}</span>
        ${todayBadge}
      </div>`;

  if (hasChildren && (isExpanded || kw)) {
    html += `<div class="tree-node-group" role="group">`;
    for (const child of visibleChildren) {
      html += buildTreeNodeHtml(child, depth + 1, !!kw && matchesKw);
    }
    html += `</div>`;
  }

  html += `</div>`;
  return html;
}

function checkSubtreeMatch(node, kw) {
  if (node.name.toLowerCase().includes(kw)) return true;
  if (node.children) {
    return node.children.some(c => checkSubtreeMatch(c, kw));
  }
  return false;
}

let _treeDelegationBound = false;
function bindTreeDelegation() {
  if (_treeDelegationBound) return;
  _treeDelegationBound = true;
  $('catalogTreeContainer').addEventListener('click', (e) => {
    const row = e.target.closest('.tree-node-row');
    if (!row) return;
    const item = row.closest('.tree-node-item');
    const path = item?.dataset.path;
    if (!path) return;
    if (e.target.closest('.tree-toggle-arrow')) {
      toggleTreeNodeExpand(path, e);
    } else {
      onTreeNodeClick(path, e);
    }
  });
  $('catalogTreeContainer').addEventListener('keydown', (e) => {
    if (['ArrowDown','ArrowUp','ArrowRight','ArrowLeft'].includes(e.key)) {
      const row = e.target.closest('.tree-node-row');
      if (!row) return;
      e.preventDefault();
      const item = row.closest('.tree-node-item');
      const node = findNodeByPath(catalogState.tree,item.dataset.path);
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        const rows = [...$('catalogTreeContainer').querySelectorAll('.tree-node-row')];
        rows[rows.indexOf(row) + (e.key === 'ArrowDown' ? 1 : -1)]?.focus();
      } else if (e.key === 'ArrowRight') {
        if (node?.children?.length && !catalogState.expandedPaths.has(node.path)) toggleTreeNodeExpand(node.path,e);
        else item.querySelector('.tree-node-group .tree-node-row')?.focus();
      } else if (node?.children?.length && catalogState.expandedPaths.has(node.path)) toggleTreeNodeExpand(node.path,e);
      else item.parentElement?.closest('.tree-node-item')?.querySelector(':scope > .tree-node-row')?.focus();
      return;
    }
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const row = e.target.closest('.tree-node-row');
    if (!row) return;
    e.preventDefault();
    const item = row.closest('.tree-node-item');
    const path = item?.dataset.path;
    if (!path) return;
    onTreeNodeClick(path, e);
  });
}

function renderCatalogTree() {
  const container = $('catalogTreeContainer');
  if (!catalogState.tree) return;
  bindTreeDelegation();

  const scrollTop = container.scrollTop, scrollLeft = container.scrollLeft;
  const focusedPath = document.activeElement?.closest('.tree-node-item')?.dataset.path;
  container.innerHTML = buildTreeNodeHtml(catalogState.tree, 0);
  container.scrollTop = scrollTop; container.scrollLeft = scrollLeft;
  if (focusedPath) findTreeItemEl(focusedPath)?.querySelector(':scope > .tree-node-row')?.focus({preventScroll:true});

  const totalRoots = catalogState.tree.children?.length || 0;
  $('treeVisibleSummary').textContent = catalogState.filterOnlyToday
    ? `已过滤今日活跃业务域`
    : `全量 ${totalRoots} 个业务域`;
}

function findTreeItemEl(path) {
  if (window.CSS && CSS.escape) {
    return document.querySelector(`.tree-node-item[data-path="${CSS.escape(path)}"]`);
  }
  for (const el of document.querySelectorAll('.tree-node-item')) {
    if (el.dataset.path === path) return el;
  }
  return null;
}

function toggleTreeNodeExpand(path, evt) {
  if (evt) evt.stopPropagation();
  const willExpand = !catalogState.expandedPaths.has(path);
  if (willExpand) {
    catalogState.expandedPaths.add(path);
  } else {
    catalogState.expandedPaths.delete(path);
  }

  // 过滤模式下结果集较小，直接整体重绘；常规模式做增量更新，避免全树重排卡顿
  if (catalogState.filterKeyword.trim()) {
    renderCatalogTree();
    return;
  }

  const item = findTreeItemEl(path);
  const node = findNodeByPath(catalogState.tree, path);
  if (!item || !node) {
    renderCatalogTree();
    return;
  }

  const arrow = item.querySelector(':scope > .tree-node-row > .tree-toggle-arrow');
  const existingGroup = item.querySelector(':scope > .tree-node-group');

  if (willExpand) {
    if (!existingGroup && node.children?.length) {
      const depth = parseInt(item.dataset.depth || '0', 10);
      const group = document.createElement('div');
      group.className = 'tree-node-group';
      group.innerHTML = node.children.map(c => buildTreeNodeHtml(c, depth + 1)).join('');
      item.appendChild(group);
    }
    if (node.children?.length) {
      arrow?.classList.add('expanded');
      arrow?.classList.remove('leaf');
    }
  } else {
    existingGroup?.remove();
    arrow?.classList.remove('expanded');
    if (!node.children?.length) arrow?.classList.add('leaf');
  }
}

function onTreeNodeClick(path, evt) {
  const node = findNodeByPath(catalogState.tree, path);
  if (!node) return;
  if (node.children?.length) catalogState.expandedPaths.add(node.path);
  selectTreeNode(node.path, node.name, node.file_count, node.today_added);
  renderCatalogTree();
}

function selectTreeNode(path, name, fileCount, todayAdded) {
  catalogState.selectedNode = { path, name, file_count: fileCount, today_added: todayAdded };
  catalogState.page = 1;

  document.querySelectorAll('.tree-node-row').forEach(row => {
    const parentItem = row.closest('.tree-node-item');
    if (parentItem && parentItem.dataset.path === path) {
      row.classList.add('selected');
      row.setAttribute('aria-selected','true'); row.tabIndex = 0;
    } else {
      row.classList.remove('selected');
      row.setAttribute('aria-selected','false'); row.tabIndex = -1;
    }
  });

  renderPortalChildFolders(findNodeByPath(catalogState.tree, path));
  $('detailDeptName').textContent = portalOrganizationLabel(name);
  $('detailDeptName').title = name;
  $('detailTotalFiles').textContent = (fileCount || 0).toLocaleString('zh-CN');
  $('detailTodayAdded').textContent = `+${(todayAdded || 0).toLocaleString('zh-CN')}`;

  const rootPrefix = catalogState.tree?.path || '/data/share';
  const rel = path.startsWith(rootPrefix) ? path.slice(rootPrefix.length) : path;
  const parts = rel.split(/[/\\]+/).filter(Boolean);
  let breadcrumbHtml = `<span class="breadcrumb-part" data-path="${esc(rootPrefix)}">企业网盘总目录</span>`;
  let accumPath = rootPrefix;
  parts.forEach(p => {
    accumPath += '/' + p;
    breadcrumbHtml += `<span class="breadcrumb-sep">/</span><span class="breadcrumb-part" data-path="${esc(accumPath)}">${esc(portalOrganizationLabel(p))}</span>`;
  });
  bindCrumbDelegation();
  $('catalogBreadcrumb').innerHTML = breadcrumbHtml;

  loadCatalogFiles();
}

let _crumbDelegationBound = false;
function bindCrumbDelegation() {
  if (_crumbDelegationBound) return;
  _crumbDelegationBound = true;
  $('catalogBreadcrumb').addEventListener('click', (e) => {
    const part = e.target.closest('.breadcrumb-part');
    const path = part?.dataset.path;
    if (!path) return;
    const node = findNodeByPath(catalogState.tree, path);
    if (node) {
      selectTreeNode(node.path, node.name, node.file_count, node.today_added);
    }
  });
}

function toggleTodayFilter() {
  catalogState.filterOnlyToday = !catalogState.filterOnlyToday;
  const btn = $('filterTodayBtn');
  btn.classList.toggle('active', catalogState.filterOnlyToday);
  $('filterTodayText').textContent = '仅看今日有更新部门';
  toast(catalogState.filterOnlyToday ? '已开启【仅看今日更新】过滤：隐藏无增量部门' : '已恢复显示全网盘组织架构树');
  loadCatalogTree();
}

function expandAllTree() {
  function collect(node) {
    catalogState.expandedPaths.add(node.path);
    if (node.children) node.children.forEach(collect);
  }
  if (catalogState.tree) collect(catalogState.tree);
  renderCatalogTree();
  toast('已展开全部组织架构树');
}

function collapseAllTree() {
  catalogState.expandedPaths.clear();
  if (catalogState.tree) catalogState.expandedPaths.add(catalogState.tree.path);
  renderCatalogTree();
  toast('已折叠至一级架构');
}

let _filterDebounceTimer = null;
let _catalogFilesRequestId = 0;
function filterCatalogTree(val) {
  catalogState.filterKeyword = val;
  clearTimeout(_filterDebounceTimer);
  _filterDebounceTimer = setTimeout(renderCatalogTree, 200);
}

function findNodeByPath(root, path) {
  if (!root) return null;
  if (root.path === path) return root;
  if (root.children) {
    for (const c of root.children) {
      const found = findNodeByPath(c, path);
      if (found) return found;
    }
  }
  return null;
}

async function loadCatalogFiles() {
  if (!catalogState.selectedNode) return;
  const requestId = ++_catalogFilesRequestId;
  const tbody = $('catalogTableBody');
  tbody.innerHTML = `
    <tr>
      <td colspan="6" style="text-align:center;padding:50px;color:#6f736b">
        <span class="spinner-anim" style="border-color:#285649;border-top-color:transparent;width:18px;height:18px"></span>
        <div style="margin-top:8px">正在查询该部门下文件…</div>
      </td>
    </tr>`;

  const onlyToday = (catalogState.fileFilterMode === 'today');
  const path = catalogState.selectedNode.path;
  const kw = catalogState.fileKeyword.trim();
  const page = catalogState.page;
  const pageSize = catalogState.pageSize;

  try {
    const params = new URLSearchParams({
      path: path,
      only_today: onlyToday,
      keyword: kw,
      page: page,
      page_size: pageSize,
      sort_by: catalogState.sortBy || 'time',
      sort_order: catalogState.sortOrder || 'desc'
    });
    const res = await api(`/api/catalog/files?${params.toString()}`);
    if (!res.ok || requestId !== _catalogFilesRequestId) return;

    catalogState.totalFiles = res.total;

    if (onlyToday) {
      $('tabCountToday').textContent = res.total;
    } else {
      $('tabCountAll').textContent = res.total;
      $('tabCountToday').textContent = catalogState.selectedNode.today_added || 0;
    }

    renderCatalogTable(res.items, res.total, page, pageSize);
  } catch (err) {
    if (requestId !== _catalogFilesRequestId) return;
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:#ef4444;padding:30px">查询文件失败：${esc(err.message)}</td></tr>`;
  }
}

function renderCatalogTable(items, total, page, pageSize) {
  const tbody = $('catalogTableBody');
  catalogState.pageFiles = items || [];
  if (!items || items.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="6" style="text-align:center;padding:60px 20px;color:#6f736b">
          <div style="margin-bottom:8px"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-folder-open"/></svg></div>
          <div style="font-size:13px;font-weight:600;color:#6f736b">该组织架构节点下暂无匹配文件</div>
          <div style="font-size:11px;margin-top:4px">请尝试切换为“全部文件”或清除搜索词</div>
        </td>
      </tr>`;
    $('pageInfoText').textContent = '第 1 页 · 共 0 条';
    const emptyBtns = $('catalogPageBtns');
    if (emptyBtns) {
      emptyBtns.innerHTML = `<button class="page-btn" disabled>上一页</button><button class="page-btn" disabled>下一页</button>`;
    }
    return;
  }

  tbody.innerHTML = items.map((f, idx) => {
    const ext = (f.ext || '').toLowerCase();
    let badgeClass = 'badge-ext-other';
    if (ext === '.xlsx' || ext === '.xls') badgeClass = 'badge-ext-xlsx';
    else if (ext === '.csv') badgeClass = 'badge-ext-csv';

    const todayBadge = f.is_today
      ? `<span class="tag-today-highlight" title="今天更新入库"><span class="dot"></span>今日更新</span>`
      : '';

    return `
      <tr>
        <td>
          <div class="file-cell-main">
            <span class="badge-ext-icon ${badgeClass}">${esc((f.ext || 'FILE').toUpperCase().replace('.', ''))}</span>
            <div class="file-name-wrap">
              <div class="file-name-line">
                <span class="file-name-text" title="${esc(f.name)}">${esc(f.name)}</span>
                ${todayBadge}
              </div>
              <div class="file-subpath-text" title="${esc(f.path)}">${esc(portalRelativeDirectory(f.parent || f.path))}</div>
            </div>
          </div>
        </td>
        <td><span style="font-family:var(--font-mono);font-size:11px;color:var(--muted);font-weight:600">${esc((f.ext || '--').replace('.', '').toUpperCase())}</span></td>
        <td><span style="font-family:var(--font-mono);font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap">${esc(f.size_display || '--')}</span></td>
        <td class="catalog-time-cell" data-time-field="ctime">${renderFileTime(f.ctime)}</td>
        <td class="catalog-time-cell" data-time-field="mtime">${renderFileTime(f.mtime)}</td>
        <td style="text-align:right">
          <div class="table-action-btns" style="justify-content:flex-end">
            <button class="btn-table-action" onclick="previewFile(${f.id})" title="在浏览器中查看表格内容">预览</button>
            <button class="btn-table-action" onclick="updateFileContent(${f.id})">更新／恢复</button>
            <button class="btn-table-action" onclick="handleCatalogDownload(${idx})" title="按当前文件夹下载权限执行">下载</button>
            <button class="btn-table-action" onclick="copyCatalogPath(${idx})" title="复制共享路径">复制</button>
          </div>
        </td>
      </tr>`;
  }).join('');

  const totalPages = Math.ceil(total / pageSize) || 1;
  $('pageInfoText').textContent = `第 ${page} / ${totalPages} 页 · 共 ${total.toLocaleString('zh-CN')} 个文件`;
  // 数字页码渲染（复用 page-btn 样式与逻辑）
  const btnsWrap = $('catalogPageBtns');
  if (btnsWrap) {
    const maxButtons = 5;
    let startPage = Math.max(1, page - 2);
    let endPage = Math.min(totalPages, startPage + maxButtons - 1);
    if (endPage - startPage < maxButtons - 1) startPage = Math.max(1, endPage - maxButtons + 1);
    let html = `<button class="page-btn" ${page <= 1 ? 'disabled' : ''} onclick="gotoCatalogPage(${page - 1})">上一页</button>`;
    for (let p = startPage; p <= endPage; p++) {
      html += `<button class="page-btn ${p === page ? 'active' : ''}" onclick="gotoCatalogPage(${p})">${p}</button>`;
    }
    html += `<button class="page-btn" ${page >= totalPages ? 'disabled' : ''} onclick="gotoCatalogPage(${page + 1})">下一页</button>`;
    btnsWrap.innerHTML = html;
  }
}

function gotoCatalogPage(p) {
  if (p < 1 || p === catalogState.page) return;
  catalogState.page = p;
  loadCatalogFiles();
}

function setFileFilterMode(mode) {
  catalogState.fileFilterMode = mode;
  $('tabAllFiles').classList.toggle('active', mode === 'all');
  $('tabTodayFiles').classList.toggle('active', mode === 'today');
  catalogState.page = 1;
  loadCatalogFiles();
}

let fileSearchTimer = null;
function onFileSearchInput() {
  clearTimeout(fileSearchTimer);
  fileSearchTimer = setTimeout(() => {
    catalogState.fileKeyword = $('fileKeywordInput').value.trim();
    catalogState.page = 1;
    loadCatalogFiles();
  }, 300);
}

function changeCatalogPage(delta) {
  catalogState.page += delta;
  loadCatalogFiles();
}

/* 组织架构列表排序处理 */
function onCatalogSortChange(val) {
  const parts = val.split('_');
  catalogState.sortBy = parts[0] || 'time';
  catalogState.sortOrder = parts[1] || 'desc';
  catalogState.page = 1;
  updateCatalogSortHeadersUI();
  loadCatalogFiles();
}

function toggleCatalogSort(field) {
  if (catalogState.sortBy === field) {
    catalogState.sortOrder = (catalogState.sortOrder === 'desc') ? 'asc' : 'desc';
  } else {
    catalogState.sortBy = field;
    catalogState.sortOrder = (field === 'name') ? 'asc' : 'desc';
  }
  catalogState.page = 1;
  updateCatalogSortHeadersUI();
  loadCatalogFiles();
}

function updateCatalogSortHeadersUI() {
  const fields = ['name', 'size', 'ctime', 'mtime'];
  fields.forEach(f => {
    const th = $('thCatalog' + f.charAt(0).toUpperCase() + f.slice(1));
    const icon = $('iconSort' + f.charAt(0).toUpperCase() + f.slice(1));
    if (!th || !icon) return;
    th.setAttribute('aria-sort',catalogState.sortBy === f ? (catalogState.sortOrder === 'desc' ? 'descending' : 'ascending') : 'none');
    if (catalogState.sortBy === f) {
      th.classList.add('active');
      icon.innerHTML = (catalogState.sortOrder === 'desc') ? '<svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-arrow-down"/></svg>' : '<svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-arrow-up"/></svg>';
    } else {
      th.classList.remove('active');
      icon.innerHTML = '<svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-chevron-down"/></svg>';
    }
  });
}

/* 搜索结果列表排序处理 */
function onSearchSortChange(val) {
  sortMode = val;
  const sel = $('resultsSortSelect');
  if (sel && sel.value !== val) sel.value = val;
  if ($('q') && $('q').value.trim()) {
    search(1);
  }
}

/* ============================================================
 * 归档时效监控与待办维护中心 (Archive Compliance & Tasks) 逻辑
 * ============================================================ */
let archiveState = {
  currentSubTab: 'tasks', // 'tasks' | 'rules'
  taskFilter: 'pending', // 'all' | 'pending' | 'submitted' | 'resolved'
  userRole: 'worker', // 'worker' (责任人) | 'admin' (管理员)
  overview: null,
  tasks: [],
  rules: [],
  currentSubmitTaskId: null,
  currentReviewTaskId: null,
  uploadedProofUrl: ''
};

async function initArchiveDashboard() {
  await Promise.all([
    loadArchiveOverview(),
    loadArchiveTasks(),
    loadArchiveRules()
  ]);
}

async function loadArchiveOverview() {
  try {
    const res = await api('/api/archive/overview');
    if (!res.ok) return;
    archiveState.overview = res;
    updatePortalArchive(res);
    $('archiveTotalRules').textContent = res.total_rules || 0;
    $('archivePendingTasks').textContent = res.pending_tasks || 0;
    $('archiveSubmittedTasks').textContent = res.submitted_tasks || 0;
    $('archiveResolvedTasks').textContent = res.resolved_tasks || 0;
    $('archiveComplianceRate').textContent = res.compliance_rate == null ? '—' : `${res.compliance_rate}%`;

    // 更新定时巡检守护心跳状态
    if (res.scheduler) {
      const sch = res.scheduler;
      const pulseText = $('topbarSchedulerText');
      if (pulseText) {
        pulseText.textContent = `巡检守护中 · 60s/次 (已巡检${sch.run_count}轮)`;
      }
      const scanTip = $('archiveScanTimeTip');
      if (scanTip) {
        scanTip.innerHTML = `最近自检：<span style="color:#171916;font-weight:700">${esc(sch.last_run_display || '刚刚')}</span> · 自动关闭 <span style="color:#15803d;font-weight:700">${sch.auto_resolved_total || 0}</span> 项`;
      }
    }

    // 更新导航红标与微型红点 (Linear 极简微红点)
    const totalAlerts = (res.pending_tasks || 0) + (res.submitted_tasks || 0);
    const badge = $('navPendingBadge');
    if (badge) {
      if (totalAlerts > 0) {
        badge.textContent = `待办 ${totalAlerts}`;
        badge.style.display = 'inline-block';
      } else {
        badge.style.display = 'none';
      }
    }
    const dot = $('navPendingDot');
    if (dot) {
      dot.style.display = totalAlerts > 0 ? 'inline-block' : 'none';
    }
    updateTaskFilterCounters();
  } catch (e) {
    console.error('loadArchiveOverview error:', e);
  }
}

async function loadArchiveTasks() {
  const grid = $('archiveTaskGrid');
  SkeletonUI.renderTaskCards(grid, 4);

  try {
    const res = await api(`/api/archive/tasks?status=${archiveState.taskFilter}&page=${portalArchivePage}&page_size=25`);
    if (!res.ok) return;
    portalArchiveTotal = res.total || 0;
    archiveState.tasks = res.items || [];
    renderArchiveTasks(archiveState.tasks);
  } catch (err) {
    grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:#ef4444;padding:30px">加载待办失败：${esc(err.message)}</div>`;
  }
}

function updateTaskFilterCounters() {
  if (!archiveState.overview) return;
  const o = archiveState.overview;
  $('cntFilterAll').textContent = ((o.pending_tasks || 0) + (o.submitted_tasks || 0) + (o.resolved_tasks || 0));
  $('cntFilterPending').textContent = o.pending_tasks || 0;
  $('cntFilterSubmitted').textContent = o.submitted_tasks || 0;
  $('cntFilterResolved').textContent = o.resolved_tasks || 0;
  $('tabBadgeTasksCount').textContent = ((o.pending_tasks || 0) + (o.submitted_tasks || 0));
}

function getAvatarColor(name) {
  const colors = [
    { bg: '#edf2ed', color: '#1e443a' },
    { bg: '#fdf2f8', color: '#be185d' },
    { bg: '#f0fdf4', color: '#15803d' },
    { bg: '#fefce8', color: '#a16207' },
    { bg: '#f6f7f5', color: '#33624e' },
    { bg: '#ecfeff', color: '#3c755e' }
  ];
  let hash = 0;
  for (let i = 0; i < (name || '').length; i++) hash = (hash * 31 + name.charCodeAt(i)) % colors.length;
  return colors[hash] || colors[0];
}

// Task queue rendering is defined in portal.js.


function switchArchiveSubTab(subTab) {
  archiveState.currentSubTab = subTab;
  $('subtabTasks').classList.toggle('active', subTab === 'tasks');
  $('subtabRules').classList.toggle('active', subTab === 'rules');
  $('archiveTasksView').classList.toggle('hidden', subTab !== 'tasks');
  $('archiveRulesView').classList.toggle('hidden', subTab !== 'rules');
  $('taskStatusFilterBar').style.display = (subTab === 'tasks') ? 'flex' : 'none';
}

function setArchiveTaskFilter(status) {
  archiveState.taskFilter = status;
  portalArchivePage = 1;
  document.querySelectorAll('#taskStatusFilterBar .filter-tab-pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.status === status);
  });
  loadArchiveTasks();
}

async function triggerArchiveCheck() {
  const btn = $('runCheckBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-anim"></span> 正在扫描全部…';
  try {
    const res = await api('/api/archive/check', { method: 'POST' });
    toast(res.message || '时效扫描完成');
    await Promise.all([loadArchiveOverview(), loadArchiveTasks(), loadArchiveRules()]);
  } catch (e) {
    toast('时效扫描异常：' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span>立即检测</span>';
  }
}

async function loadArchiveRules() {
  try {
    const res = await api('/api/archive/rules');
    if (!res.ok) return;
    archiveState.rules = res.rules || [];
    $('tabBadgeRulesCount').textContent = archiveState.rules.length;

    const tbody = $('archiveRulesTableBody');
    if (!archiveState.rules.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:30px;color:#6f736b">暂无监控规则，请点击上方“+ 新建规则”添加人工维护</td></tr>';
      return;
    }

    tbody.innerHTML = archiveState.rules.map(r => `
      <tr>
        <td>
          <div style="font-weight:700;color:#171916">${esc(r.name)}</div>
          <div style="font-size:10.5px;color:#6f736b;margin-top:2px">ID: ${r.id}</div>
        </td>
        <td><span style="font-family:monospace;font-size:11.5px;color:#3e413a">${esc(r.target_path)}</span><div class="rule-assessment-note">${esc(portalAssessmentLabel(r.assessment))} · ${fmtNumber(r.assessment?.matched_files || 0)} 份 / 非空 ${fmtNumber(r.assessment?.nonempty_files || 0)} 份</div></td>
        <td><span class="stat-pill" style="font-size:11px">${esc(r.assessment?.schedule?.summary || (r.period_type === 'daily' ? `每天 ${r.deadline_time || '18:00'} 前` : (r.period_type === 'monthly' ? '每 30 天更新' : `每 ${r.period_days} 天更新`)))}</span></td>
        <td><span style="font-size:11.5px;color:#3e413a">${esc(r.responsible_person || '未指定')}</span></td>
        <td>
          <span class="rule-status-toggle ${r.is_active ? 'active' : 'inactive'}" onclick="toggleArchiveRule(${r.id})" title="点击可快速启用/暂停此规则">
            ${r.is_active ? '监控中' : '已暂停'}
          </span>
        </td>
        <td style="color:#6f736b;font-size:11.5px">${esc(r.last_checked_display)}</td>
        <td style="text-align:right">
          <div class="table-action-btns" style="justify-content:flex-end">
            <button class="btn-table-action" onclick="openEditRuleModal(${r.id})" title="修改监控目录或时效周期">编辑</button>
            <button class="btn-table-action" onclick="checkSingleRule(${r.id})" title="立即针对此目录执行自检">自检</button>
            <button class="btn-table-action" data-rule-locate="${r.id}">定位</button>
            <button class="btn-table-action" style="color:#ef4444" onclick="deleteArchiveRule(${r.id})">删除</button>
          </div>
        </td>
      </tr>
    `).join('');
    tbody.querySelectorAll('[data-rule-locate]').forEach(button => {
      button.onclick = () => {
        const rule = archiveState.rules.find(item => item.id === Number(button.dataset.ruleLocate));
        if (rule) locateInCatalog(rule.target_path);
      };
    });
  } catch (e) {
    console.error('loadArchiveRules error:', e);
  }
}

async function toggleArchiveRule(ruleId) {
  try {
    const res = await api(`/api/archive/rules/${ruleId}/toggle`, { method: 'PUT' });
    toast(res.message || '规则状态已变更');
    await loadArchiveRules();
  } catch (e) {
    toast('操作失败：' + e.message);
  }
}

async function checkSingleRule(ruleId) {
  toast('正在执行当前规则时效自检…');
  try {
    const res = await api('/api/archive/check', { method: 'POST' });
    toast('自检完成！' + (res.message || ''));
    await Promise.all([loadArchiveOverview(), loadArchiveTasks(), loadArchiveRules()]);
  } catch (e) {
    toast('自检失败：' + e.message);
  }
}

function openEditRuleModal(ruleId) {
  const r = archiveState.rules.find(x => x.id === ruleId);
  if (!r) return;
  $('editRuleId').value = r.id;
  $('editRuleName').value = r.name;
  $('editRulePath').value = r.target_path;
  portalText('portalRulePreflightEdit', '保存前将检查目录是否匹配当前索引。');
  $('editRulePeriodType').value = r.period_type || 'weekly';
  $('editRulePeriodDays').value = r.period_days || 7;
  $('editRulePerson').value = r.responsible_person || '';
  $('editRuleKeyword').value = r.file_keyword || '';
  $('editRuleDeadline').value = r.deadline_time || '18:00';
  fillRuleSchedule('edit', r.schedule || {mode:'rolling'});
  $('editRuleModal').classList.remove('hidden');
  _focusFirstInput('editRuleModal');
}

function closeEditRuleModal() {
  $('editRuleModal').classList.add('hidden');
}

function onEditPeriodTypeChange(val) {
  syncRuleScheduleFields('edit', true);
  const input = $('editRulePeriodDays');
  if (val === 'daily') input.value = 1;
  else if (val === 'weekly') input.value = 7;
  else if (val === 'monthly') input.value = 30;
}

async function confirmEditRule() {
  const id = $('editRuleId').value;
  const name = $('editRuleName').value.trim();
  const target_path = $('editRulePath').value.trim();
  const period_type = $('editRulePeriodType').value;
  const period_days = parseInt($('editRulePeriodDays').value, 10) || 7;
  const responsible_person = $('editRulePerson').value.trim();
  const file_keyword = $('editRuleKeyword').value.trim(), deadline_time = $('editRuleDeadline').value || '18:00';

  if (!name || !target_path) {
    toast('请完整填写规则名称和监控路径');
    return;
  }

  try {
    const checked = await preflightPortalRule('edit');
    if (!checked || !responsible_person) { if (!responsible_person) toast('请填写明确的责任人或责任组'); return; }
    const res = await api(`/api/archive/rules/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ name, target_path, period_type, period_days, responsible_person, file_keyword, deadline_time, schedule:readRuleSchedule('edit') })
    });
    toast(res.message || '规则已更新');
    closeEditRuleModal();
    await Promise.all([loadArchiveOverview(), loadArchiveTasks(), loadArchiveRules()]);
  } catch (e) {
    toast('更新规则失败：' + e.message);
  }
}

async function deleteArchiveRule(ruleId) {
  if (!confirm('确定要删除此条监控规则及其历史待办吗？')) return;
  try {
    const res = await api(`/api/archive/rules/${ruleId}`, { method: 'DELETE' });
    toast(res.message || '规则已删除');
    await Promise.all([loadArchiveOverview(), loadArchiveTasks(), loadArchiveRules()]);
  } catch (e) {
    toast('删除失败：' + e.message);
  }
}


function _focusFirstInput(modalId) {
  const m = $(modalId);
  if (!m) return;
  const first = m.querySelector('input:not([type=hidden]), textarea, select');
  if (first) setTimeout(() => first.focus(), 50);
}
/* 员工提交证明弹窗交互 */
function openSubmitProofModal(taskId) {
  archiveState.currentSubmitTaskId = taskId;
  archiveState.uploadedProofUrl = '';
  const task = findPortalArchiveTask(taskId);
  if (task) {
    $('submitModalTaskTitle').textContent = task.rule_name;
    $('submitModalTaskPath').textContent = task.target_path;
    $('submitterNameInput').value = currentUser?.name || '未登录';
    $('submitNoteInput').value = task.submit_note || '';
    if (task.proof_image) {
      archiveState.uploadedProofUrl = task.proof_image;
      $('uploadPreviewImg').src = task.proof_image;
      $('uploadPreviewWrap').classList.remove('hidden');
      $('proofDropzone').classList.add('hidden');
    } else {
      clearUploadProof();
    }
  }
  $('submitProofModal').classList.remove('hidden');
  _focusFirstInput('submitProofModal');
}

function closeSubmitProofModal() {
  $('submitProofModal').classList.add('hidden');
  archiveState.currentSubmitTaskId = null;
}

function handleProofFileSelected(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(evt) {
    archiveState.uploadedProofUrl = evt.target.result;
    $('uploadPreviewImg').src = archiveState.uploadedProofUrl;
    $('uploadPreviewWrap').classList.remove('hidden');
    $('proofDropzone').classList.add('hidden');
  };
  reader.readAsDataURL(file);
}

function clearUploadProof(e) {
  if (e) e.stopPropagation();
  archiveState.uploadedProofUrl = '';
  $('proofFileInput').value = '';
  $('uploadPreviewImg').src = '';
  $('uploadPreviewWrap').classList.add('hidden');
  $('proofDropzone').classList.remove('hidden');
}

// 监听剪贴板粘贴截图 (Ctrl+V / Cmd+V)
window.addEventListener('paste', e => {
  if ($('submitProofModal').classList.contains('hidden')) return;
  const items = e.clipboardData?.items;
  if (!items) return;
  for (let i = 0; i < items.length; i++) {
    if (items[i].type.indexOf('image') !== -1) {
      const blob = items[i].getAsFile();
      const reader = new FileReader();
      reader.onload = function(evt) {
        archiveState.uploadedProofUrl = evt.target.result;
        $('uploadPreviewImg').src = archiveState.uploadedProofUrl;
        $('uploadPreviewWrap').classList.remove('hidden');
        $('proofDropzone').classList.add('hidden');
        toast('已成功从剪贴板粘贴截图证明！');
      };
      reader.readAsDataURL(blob);
      break;
    }
  }
});

async function confirmSubmitTask() {
  const taskId = archiveState.currentSubmitTaskId;
  if (!taskId) return;
  const note = $('submitNoteInput').value.trim();
  const submitter = $('submitterNameInput').value.trim() || '责任人';
  const proof = archiveState.uploadedProofUrl.trim();

  if (!proof) {
    toast('请先上传或粘贴截图证明，作为任务完成凭据');
    return;
  }

  const btn = $('btnConfirmSubmit');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-anim"></span> 提交中…';
  try {
    const res = await api(`/api/archive/tasks/${taskId}/submit`, {
      method: 'POST',
      body: JSON.stringify({ note, proof_image: proof, submitted_by: submitter })
    });
    toast(res.message || '已提交待管理员审核');
    closeSubmitProofModal();
    await Promise.all([loadArchiveOverview(), loadArchiveTasks()]);
  } catch (e) {
    toast('提交失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '确认提交待管理员审核';
  }
}

/* 管理员审核弹窗交互 */
function openAdminReviewModal(taskId) {
  archiveState.currentReviewTaskId = taskId;
  const task = findPortalArchiveTask(taskId);
  if (!task) return;
  $('reviewModalTaskTitle').textContent = `${task.rule_name} (超期 ${task.overdue_days} 天)`;
  $('reviewModalSubmitter').textContent = task.submitted_by || '责任人';
  $('reviewModalSubmitTime').textContent = task.submitted_at_display;
  $('reviewModalSubmitNote').textContent = task.submit_note || '（无补充文字说明）';
  $('reviewModalProofImg').src = task.proof_image || '';
  $('reviewCommentInput').value = '';
  $('adminReviewModal').classList.remove('hidden');
  _focusFirstInput('adminReviewModal');
}

function closeAdminReviewModal() {
  $('adminReviewModal').classList.add('hidden');
  archiveState.currentReviewTaskId = null;
}

async function confirmReviewTask(action) {
  const taskId = archiveState.currentReviewTaskId;
  if (!taskId) return;
  const comment = $('reviewCommentInput').value.trim();
  try {
    const res = await api(`/api/archive/tasks/${taskId}/review`, {
      method: 'POST',
      body: JSON.stringify({ action, comment, reviewed_by: '管理员' })
    });
    toast(res.message);
    closeAdminReviewModal();
    await Promise.all([loadArchiveOverview(), loadArchiveTasks()]);
  } catch (e) {
    toast('审核失败：' + e.message);
  }
}

/* 新建规则弹窗交互 */
function openCreateRuleModal() {
  $('newRuleKeyword').value = '';
  $('newRuleDeadline').value = '18:00';
  $('newRuleDeadline').disabled = true;
  $('newRuleName').value = '';
  $('newRulePath').value = catalogState.selectedNode?.path || portalLastStatus?.root_display || '';
  portalText('portalRulePreflightCreate', '保存前将检查目录是否匹配当前索引。');
  $('newRulePeriodType').value = 'weekly';
  $('newRulePeriodDays').value = 7;
  $('newRulePerson').value = '';
  fillRuleSchedule('new', {mode:'calendar',due_period:'next',due_day:2,holiday_policy:'next_workday'});
  $('createRuleModal').classList.remove('hidden');
  _focusFirstInput('createRuleModal');
}

function openCreateRuleModalForSelectedNode() {
  $('newRuleKeyword').value='';
  $('newRuleDeadline').value='18:00';
  $('newRuleDeadline').disabled=true;
  const node = catalogState.selectedNode;
  const path = node?.path || '/data/share';
  const name = node?.name ? `${node.name} - 归档时效监控` : '新部门目录时效监控';
  $('newRuleName').value = name;
  $('newRulePath').value = path;
  $('newRulePeriodType').value = 'weekly';
  $('newRulePeriodDays').value = 7;
  $('newRulePerson').value = '';
  fillRuleSchedule('new', {mode:'calendar',due_period:'next',due_day:2,holiday_policy:'next_workday'});
  $('createRuleModal').classList.remove('hidden');
}

function closeCreateRuleModal() {
  $('createRuleModal').classList.add('hidden');
}

function onPeriodTypeChange(val) {
  syncRuleScheduleFields('new', true);
  const input = $('newRulePeriodDays');
  if (val === 'daily') input.value = 1;
  else if (val === 'weekly') input.value = 7;
  else if (val === 'monthly') input.value = 30;
}

async function confirmCreateRule() {
  const name = $('newRuleName').value.trim();
  const target_path = $('newRulePath').value.trim();
  const period_type = $('newRulePeriodType').value;
  const period_days = parseInt($('newRulePeriodDays').value, 10) || 7;
  const responsible_person = $('newRulePerson').value.trim();
  const file_keyword = $('newRuleKeyword').value.trim(), deadline_time = $('newRuleDeadline').value || '18:00';

  if (!name || !target_path) {
    toast('请完整填写规则名称和监控目录路径');
    return;
  }

  try {
    const checked = await preflightPortalRule('create');
    if (!checked || !responsible_person) { if (!responsible_person) toast('请填写明确的责任人或责任组'); return; }
    const res = await api('/api/archive/rules', {
      method: 'POST',
      body: JSON.stringify({ name, target_path, period_type, period_days, responsible_person, file_keyword, deadline_time, schedule:readRuleSchedule('new') })
    });
    toast(res.message || '规则已创建');
    closeCreateRuleModal();
    await Promise.all([loadArchiveOverview(), loadArchiveTasks(), loadArchiveRules()]);
  } catch (e) {
    toast('创建规则失败：' + e.message);
  }
}

/* 截图大图预览 */
function openImagePreview(src) {
  if (!src) return;
  $('imagePreviewBig').src = src;
  $('imagePreviewModal').classList.remove('hidden');
}
function closeImagePreview() {
  $('imagePreviewModal').classList.add('hidden');
}

/* -----------------------------------------------------------
 * 人员权限维护与飞书打通控制器 (Permission & Feishu Integration)
 * ----------------------------------------------------------- */
let permState = {
  users: [],
  feishuConfig: {},
  summary: {}
};

async function initPermissionDashboard() {
  await loadPermissionOverview();
}

async function loadPermissionOverview() {
  try {
    const res = await api('/api/system/permissions');
    if (!res.ok) return;
    permState.users = res.users || [];
    permState.feishuConfig = res.feishu_config || {};
    permState.summary = res.summary || {};

    // 1. KPI 统计卡片渲染
    if ($('permTotalUsers')) $('permTotalUsers').textContent = permState.summary.total_users || 0;
    if ($('permSuperAdmins')) $('permSuperAdmins').textContent = permState.summary.super_admins || 0;
    if ($('permDeptAdmins')) $('permDeptAdmins').textContent = permState.summary.dept_admins || 0;

    const cfg = permState.feishuConfig;
    const isBound = Boolean(cfg.app_id);
    if ($('permFeishuStatus')) $('permFeishuStatus').innerHTML = isBound ? '已打通' : '待配置';
    const lastSyncDisplay = cfg.last_synced_at ? `${new Date(cfg.last_synced_at * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} 完成同步` : '尚未执行同步';
    if ($('permFeishuSyncTip')) $('permFeishuSyncTip').textContent = `通讯录：${lastSyncDisplay}`;

    // 2. 飞书配置表单回显
    if ($('cfgFeishuAppId')) $('cfgFeishuAppId').value = cfg.app_id || '';
    if ($('cfgFeishuAppSecret')) $('cfgFeishuAppSecret').value = cfg.app_secret || '';
    if ($('cfgFeishuWebhook')) $('cfgFeishuWebhook').value = cfg.webhook_url || '';
    if ($('cfgFeishuNotifyCheck')) $('cfgFeishuNotifyCheck').checked = cfg.notify_enabled !== false;

    // 3. 人员表格渲染
    renderPermissionUsersTable();
    await loadDirectoryGrantPanel();
    await loadFeishuPanel();
  } catch (e) {
    console.error('loadPermissionOverview error:', e);
  }
}

function renderPermissionUsersTable() {
  const tbody = $('permissionUsersTableBody');
  if (!tbody) return;

  if (!permState.users.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:40px;color:#6f736b">暂无受控企业成员，请点击上方“+ 新增成员”</td></tr>';
    return;
  }

  tbody.innerHTML = permState.users.map(u => {
    const isActive = u.status === 'active';
    const statusPill = `
      <span class="rule-status-toggle ${isActive ? 'active' : 'inactive'}" onclick="toggleUserStatus(${u.id})" title="点击切换启用/停用">
        ${isActive ? '正常' : '已停用'}
      </span>
    `;

    return `
      <tr>
        <td>
          <div style="display:flex;align-items:center;gap:9px">
            <div style="width:28px;height:28px;border-radius:50%;background:#e3e5e1;color:#3e413a;font-weight:700;font-size:11px;display:flex;align-items:center;justify-content:center">${esc(u.avatar || u.name.slice(0, 2))}</div>
            <div>
              <div style="font-weight:700;color:#171916">${esc(u.name)}</div>
              <div style="font-size:10.5px;color:#6f736b">UID: ${u.id}</div>
            </div>
          </div>
        </td>
        <td>
          <span style="font-size:12.5px;color:#171916;font-weight:600;font-family:monospace">${esc(u.phone || '--')}</span>
        </td>
        <td>
          <div style="display:flex;align-items:center;gap:6px">
            <span style="font-size:12px;color:#6f736b;font-family:monospace">••••••••</span>
            <button class="btn-table-action" style="padding:2px 8px;font-size:11px;background:#FAFAFA;border:1px solid #e3e5e1;border-radius:6px;cursor:pointer" onclick="openResetPasswordModal(${u.id}, '${esc(u.name)}', '${esc(u.phone || '')}')" title="重置登录密码">重置</button>
          </div>
        </td>
        <td><span style="font-size:12px;color:#3e413a">${esc(u.department || '未分配部门')}</span></td>
        <td>
          <div style="display:flex;align-items:center;gap:6px">
            <select class="sort-select-pill" style="padding:2px 8px;font-size:11.5px;font-weight:600" onchange="changeUserRole(${u.id}, this.value)">
              <option value="user" ${u.role === 'user' ? 'selected' : ''}>普通用户</option>
              <option value="dept_admin" ${u.role === 'dept_admin' ? 'selected' : ''}>部门管理员</option>
              <option value="super_admin" ${u.role === 'super_admin' ? 'selected' : ''}>系统超管</option>
            </select>
          </div>
        </td>
        <td>${statusPill}</td>
        <td style="text-align:right">
          <div class="table-action-btns" style="justify-content:flex-end">
            <button class="btn-table-action" style="color:#ef4444" onclick="deleteUser(${u.id})" title="移除此人员系统权限">移除</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

function openResetPasswordModal(userId, name, phone) {
  $('resetPasswordUserId').value = userId;
  $('resetPasswordUserName').textContent = name;
  $('resetPasswordUserPhone').textContent = phone;
  $('newPasswordInput').value = '';
  $('resetPasswordModal').classList.remove('hidden');
  _focusFirstInput('resetPasswordModal');
}

function closeResetPasswordModal() {
  $('resetPasswordModal').classList.add('hidden');
}

async function confirmResetPassword() {
  const userId = $('resetPasswordUserId').value;
  const newPassword = $('newPasswordInput').value.trim();
  if (!newPassword) {
    toast('请输入新登录密码');
    return;
  }
  if (newPassword.length < 4) {
    toast('密码长度不能少于4位');
    return;
  }

  try {
    const res = await api(`/api/system/permissions/users/${userId}/password`, {
      method: 'PUT',
      body: JSON.stringify({ password: newPassword })
    });
    toast('' + (res.message || '登录密码已成功更新！'));
    closeResetPasswordModal();
  } catch (e) {
    toast('重置密码失败：' + e.message);
  }
}

async function changeUserRole(userId, newRole) {
  try {
    const res = await api(`/api/system/permissions/users/${userId}/role`, {
      method: 'PUT',
      body: JSON.stringify({ role: newRole })
    });
    toast('' + (res.message || '角色已更新'));
    await loadPermissionOverview();
  } catch (e) {
    toast('更新角色失败：' + e.message);
    await loadPermissionOverview();
  }
}

async function toggleUserStatus(userId) {
  try {
    const res = await api(`/api/system/permissions/users/${userId}/status`, { method: 'PUT' });
    toast(res.message || '账号状态已切换');
    await loadPermissionOverview();
  } catch (e) {
    toast('切换失败：' + e.message);
  }
}

async function deleteUser(userId) {
  if (!confirm('确定要移除该成员的系统授权吗？')) return;
  try {
    const res = await api(`/api/system/permissions/users/${userId}`, { method: 'DELETE' });
    toast(res.message || '授权已移除');
    await loadPermissionOverview();
  } catch (e) {
    toast('移除失败：' + e.message);
  }
}

function openCreateUserModal() {
  $('newUserName').value = '';
  $('newUserPhone').value = '';
  $('newUserPassword').value = '123456';
  $('newUserDept').value = '电商运营部';
  $('newUserRole').value = 'user';
  $('createUserModal').classList.remove('hidden');
  _focusFirstInput('createUserModal');
}

function closeCreateUserModal() {
  $('createUserModal').classList.add('hidden');
}

async function confirmCreateUser() {
  const name = $('newUserName').value.trim();
  const phone = $('newUserPhone').value.trim();
  const password = $('newUserPassword').value.trim() || '123456';
  const department = $('newUserDept').value.trim();
  const role = $('newUserRole').value;

  if (!name) {
    toast('请填写成员姓名');
    return;
  }
  if (!phone) {
    toast('请填写登录电话号码 (手机号)');
    return;
  }

  try {
    const res = await api('/api/system/permissions/users', {
      method: 'POST',
      body: JSON.stringify({ name, phone, password, department, role })
    });
    toast('' + (res.message || '新成员账号已成功创建并授权！'));
    closeCreateUserModal();
    await loadPermissionOverview();
  } catch (e) {
    toast('添加成员失败：' + e.message);
  }
}


/* 筛选 Popover 与模式选择控制器 */
function toggleFilterPopover(force) {
  const pop = $('filterPopoverMenu');
  if (!pop) return;
  if (pop.classList.contains('search-filters-inline')) { pop.querySelector('button')?.focus(); return; }
  const isHidden = pop.classList.contains('hidden');
  const show = force !== undefined ? force : isHidden;
  if (show) {
    pop.classList.remove('hidden');
  } else {
    pop.classList.add('hidden');
  }
}

function selectMatchMode(mode) {
  matchMode = mode;
  $('btnFilterAnd')?.classList.toggle('active', mode === 'all');
  $('btnFilterOr')?.classList.toggle('active', mode === 'any');
  syncQuickFilterStrip();
  toast(`已切换检索模式：【${mode === 'all' ? '严格匹配 (AND)' : '宽松推荐 (OR)'}】`);
  if ($('q').value.trim()) search();
}

/* 格式筛选统一入口：快捷条与 Popover 共用 */
function setExtFilter(ext) {
  selectedExt = ext || '';
  document.querySelectorAll('.popover-ext-btn').forEach(b => b.classList.toggle('active', (b.dataset.ext || '') === selectedExt));
  syncQuickFilterStrip();
  updateFilterToggleState();
  toast(`已筛选格式：${selectedExt ? selectedExt.toUpperCase() : '全部'}`);
  if ($('q').value.trim()) search();
}

/* 快捷筛选条 ↔ Popover 双向同步：以全局状态为准刷新各组高亮 */
function syncQuickFilterStrip() {
  document.querySelectorAll('.popover-ext-btn').forEach(button => button.setAttribute('aria-pressed',String((button.dataset.ext || '') === selectedExt)));
  document.querySelectorAll('[data-qf-ext]').forEach(button => button.setAttribute('aria-pressed',String((button.dataset.qfExt || '') === selectedExt)));

  syncPortalAdvancedFilters();
  const strip = $('quickFilterStrip');
  if (!strip) return;
  strip.querySelectorAll('[data-qf-ext]').forEach(b => b.classList.toggle('active', (b.dataset.qfExt || '') === (selectedExt || '')));
  const quickKey = matchQuickRange(dateFrom, dateTo);
  strip.querySelectorAll('[data-qf-range]').forEach(b => b.classList.toggle('active', b.dataset.qfRange === quickKey));
  strip.querySelectorAll('[data-qf-mode]').forEach(b => b.classList.toggle('active', b.dataset.qfMode === matchMode));
}

// 日期筛选：快捷标签 + 自定义输入，双向联动
const DATE_QUICK_LABELS = {today:'今天',yesterday:'昨天','3d':'近3天','7d':'近7天','30d':'近30天',this_month:'本月',last_month:'上个月',this_quarter:'本季度',last_quarter:'上季度',this_year:'本年','3m':'近三个月','6m':'近半年',custom:'自定义'};
const DATE_FIELD_LABEL = { mtime: '修改日期', ctime: '创建日期' };

/** 计算给定快捷范围对应的 from/to 日期字符串（YYYY-MM-DD） */
function computeDateRange(rangeKey) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  switch (rangeKey) {
    case 'today':
      return { from: fmt(today), to: fmt(today) };
    case 'yesterday': {
      const y = new Date(today.getTime() - 86400000);
      return { from: fmt(y), to: fmt(y) };
    }
    case '3d': {
      const f = new Date(today.getTime() - 86400000 * 2);
      return { from: fmt(f), to: fmt(today) };
    }
    case '7d': {
      const f = new Date(today.getTime() - 86400000 * 6);
      return { from: fmt(f), to: fmt(today) };
    }
    case '30d': {
      const f = new Date(today.getTime() - 86400000 * 29);
      return { from: fmt(f), to: fmt(today) };
    }
    case 'this_month':
      return { from: fmt(new Date(today.getFullYear(), today.getMonth(), 1)), to: fmt(today) };
    case 'last_month':
      return { from: fmt(new Date(today.getFullYear(), today.getMonth() - 1, 1)), to: fmt(new Date(today.getFullYear(), today.getMonth(), 0)) };
    case 'this_quarter':
      return { from: fmt(new Date(today.getFullYear(), Math.floor(today.getMonth() / 3) * 3, 1)), to: fmt(today) };
    case 'last_quarter': {
      const month = Math.floor(today.getMonth() / 3) * 3;
      return { from: fmt(new Date(today.getFullYear(), month - 3, 1)), to: fmt(new Date(today.getFullYear(), month, 0)) };
    }
    case 'this_year':
      return { from: fmt(new Date(today.getFullYear(), 0, 1)), to: fmt(today) };
    case '3m':
    case '6m': {
      // Calendar months, clamped for short months (e.g. May 31 → Feb 28).
      const start = new Date(today.getFullYear(), today.getMonth() - (rangeKey === '3m' ? 3 : 6), 1);
      const lastDay = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
      start.setDate(Math.min(today.getDate(), lastDay));
      return { from: fmt(start), to: fmt(today) };
    }
    default:
      return { from: '', to: '' };
  }
}

/** 对比 from/to 是否等于某个快捷范围的结果，匹配则返回 rangeKey，否则返回 'custom' */
function matchQuickRange(from, to) {
  if (!from && !to) return 'all';
  if (from && to) {
    for (const key of Object.keys(DATE_QUICK_LABELS).filter(k => k !== 'custom')) {
      const r = computeDateRange(key);
      if (r.from === from && r.to === to) return key;
    }
  }
  return 'custom';
}

/** 刷新快捷标签高亮：activeRange='all'/'today'/'yesterday'/'3d'/'7d'/'30d' 任一标签高亮，'custom' 或无值则全部置灰 */
function refreshDateQuickTags(activeRange) {
  const wrap = $('dateQuickTags');
  if (!wrap) return;
  wrap.querySelectorAll('button').forEach(btn => {
    const active = btn.dataset.range === activeRange;
    btn.classList.toggle('active',active);
    btn.setAttribute('aria-pressed',String(active));
    if (active) {
      btn.style.background = '#edf2ed';
      btn.style.borderColor = '#285649';
      btn.style.color = '#285649';
      btn.style.fontWeight = '600';
    } else {
      btn.style.background = '#fff';
      btn.style.borderColor = '#e3e5e1';
      btn.style.color = '#6f736b';
      btn.style.fontWeight = '400';
    }
  });
}

function refreshDateFieldToggle() {
  const wrap = $('dateFieldToggle');
  if (!wrap) return;
  wrap.querySelectorAll('button').forEach(btn => {
    const active = btn.dataset.field === dateField;
    btn.style.background = active ? '#fff' : 'transparent';
    btn.style.color = active ? '#285649' : '#6f736b';
    btn.style.fontWeight = active ? '600' : '400';
    btn.style.boxShadow = active ? '0 1px 2px rgba(0,0,0,0.08)' : 'none';
  });
}

/** 更新提示文字 + 漏斗按钮高亮 */
function _updateDateHint(from, to, quickKey) {
  const hint = $('dateFilterHint');
  const btn = $('filterPopoverToggle');
  if (from || to) {
    const quickLabel = DATE_QUICK_LABELS[quickKey] || '自定义';
    const rangeText = (from || '不限') + ' 至 ' + (to || '至今');
    hint.textContent = rangeText + ' · ' + quickLabel + '（按' + DATE_FIELD_LABEL[dateField] + '）';
    hint.style.color = '#285649';
    if (btn) btn.style.background = '#e0e8de';
  } else {
    hint.textContent = '不限日期，全部文件参与检索';
    hint.style.color = '#6f736b';
    if (btn) btn.style.background = '#FAFAFA';
  }
}

/** 点击快捷标签：计算范围 → 更新 date input → 更新全局变量 → 刷新高亮 → 自动搜索 */
function applyDateQuick(rangeKey) {
  const { from, to } = computeDateRange(rangeKey);
  $('dateFromInput').value = from;
  $('dateToInput').value = to;
  dateFrom = from;
  dateTo = to;
  refreshDateQuickTags(rangeKey);
  _updateDateHint(from, to, rangeKey);
  syncQuickFilterStrip();
  if ($('q').value.trim()) search();
}

/** 手动修改 date input：双向联动 — 计算范围匹配哪个快捷标签，刷新高亮和提示 */
function onDateFilterChange() {
  const from = $('dateFromInput')?.value || '';
  const to = $('dateToInput')?.value || '';
  if (from && to && from > to) {
    toast('起始日期不能晚于截止日期');
    $('dateToInput').value = '';
    dateTo = '';
    return;
  }
  dateFrom = from;
  dateTo = to;
  const quickKey = matchQuickRange(from, to);
  refreshDateQuickTags(quickKey);
  _updateDateHint(from, to, quickKey);
  syncQuickFilterStrip();
  if ($('q').value.trim()) search();
}

function setDateField(field) {
  if (field === dateField) return;
  dateField = field;
  refreshDateFieldToggle();
  const quickKey = matchQuickRange(dateFrom, dateTo);
  _updateDateHint(dateFrom, dateTo, quickKey);
  syncQuickFilterStrip();
  if ($('q').value.trim()) search();
}

function clearDateFilter() {
  applyDateQuick('all');
}

// 全局监听点击 Popover 外部关闭
document.addEventListener('click', (e) => {
  const pop = $('filterPopoverMenu');
  const toggleBtn = $('filterPopoverToggle');
  if (!pop || pop.classList.contains('search-filters-inline') || pop.classList.contains('hidden')) return;
  if (!pop.contains(e.target) && (!toggleBtn || !toggleBtn.contains(e.target))) {
    pop.classList.add('hidden');
  }
});

/* 页面加载初始化 */
window.addEventListener('DOMContentLoaded', async () => {
  // Remove legacy persisted passwords; the existing HttpOnly session cookie handles sign-in.
  try { localStorage.removeItem('ff_saved_login'); } catch {}
  const authenticated = await checkAuth();
  initHomeDataCockpit();
  KeyboardManager.init();
  if ($('searchBtn')) $('searchBtn').onclick = () => search(1);
  $('q').onkeydown = e => { if (e.key === 'Enter') search(1); };
  const syncClearBtn = () => {
    const btn = $('clearSearch');
    if (btn) btn.style.visibility = $('q').value ? 'visible' : 'hidden';
  };
  $('q').addEventListener('input', syncClearBtn);
  syncClearBtn();
  $('clearSearch').onclick = () => { $('q').value = ''; clearPortalSearchFilters(); syncClearBtn(); showHomeCockpit(); $('q').focus(); };
  if ($('refreshPage')) $('refreshPage').onclick = () => { loadStatus(); loadRecent(); loadArchiveOverview(); toast('状态已更新'); };
  if ($('indexBtn')) $('indexBtn').onclick = () => startScan();

  // Popover 格式过滤与快捷筛选条统一走 setExtFilter
  document.querySelectorAll('.popover-ext-btn').forEach(btn => {
    btn.onclick = () => setExtFilter(btn.dataset.ext || '');
  });

  // 快捷筛选条接线
  const strip = $('quickFilterStrip');
  if (strip) {
    strip.querySelectorAll('[data-qf-ext]').forEach(b => { b.onclick = () => setExtFilter(b.dataset.qfExt || ''); });
    strip.querySelectorAll('[data-qf-range]').forEach(b => { b.onclick = () => applyDateQuick(b.dataset.qfRange); });
    strip.querySelectorAll('[data-qf-mode]').forEach(b => { b.onclick = () => selectMatchMode(b.dataset.qfMode); });
  }
  syncQuickFilterStrip();

  // 搜索历史
  initSearchHistoryUI();

  // 极简低调热搜文字链
  document.querySelectorAll('.prompt-pill-link').forEach(link => {
    link.onclick = () => {
      $('q').value = link.dataset.q;
      syncClearBtn();
      search();
    };
  });

  // 格式过滤 Pill 兼容
  document.querySelectorAll('.filter-pills-cluster .pill-tab, .filter-chips .chip-btn').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('.filter-pills-cluster .pill-tab, .filter-chips .chip-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedExt = btn.dataset.ext;
      search();
    };
  });

  // 匹配规则分段器兼容 (AND / OR)
  document.querySelectorAll('#matchModeGroup .seg-pill-btn, #matchModeGroup .seg-btn').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('#matchModeGroup .seg-pill-btn, #matchModeGroup .seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      matchMode = btn.dataset.mode;
      toast(`已切换检索模式：【${matchMode === 'all' ? '严格包含所有词 (AND)' : '宽松推荐 (OR)'}】`);
      if ($('q').value.trim()) search();
    };
  });

  // 深度测试推荐词兼容
  document.querySelectorAll('.prompt-pill').forEach(pill => {
    pill.onclick = () => {
      $('q').value = pill.dataset.q;
      search();
    };
  });

  loadStatus();
  loadRecent();
  loadArchiveOverview();

  const urlParams = new URLSearchParams(window.location.search);
  // Production routes were consumed by restoreRoute after authentication.
  // Retain the existing explicit demo-scene hooks without overriding a view URL.
  if (!authenticated || urlParams.has('view')) return;
  const testScene = urlParams.get('test_scene');
  if (testScene === 'catalog_today') {
    await switchNavMode('catalog');
    if (!catalogState.filterOnlyToday) toggleTodayFilter();
  } else if (testScene) {
    handleTestScene(testScene);
  }
});

/* -----------------------------------------------------------
 * 端到端场景自动调度机 (用于验收测试与快速演示)
 * ----------------------------------------------------------- */
async function locateInCatalog(dirPath, options = {}) {
  if (!dirPath) return;
  try {
    const resolved = await api('/api/catalog/resolve?' + new URLSearchParams({ path: dirPath }));
    catalogState.filterOnlyToday = false;
    catalogState.filterKeyword = '';
    catalogState.fileFilterMode = options.todayOnly ? 'today' : 'all';
    catalogState.fileKeyword = options.fileKeyword || '';
    $('treeSearchInput').value = '';
    $('fileKeywordInput').value = options.fileKeyword || '';
    $('filterTodayBtn').classList.remove('active');
    $('tabAllFiles').classList.toggle('active',!options.todayOnly);
    $('tabTodayFiles').classList.toggle('active',!!options.todayOnly);
    await switchNavMode('catalog');
    const target = findNodeByPath(catalogState.tree, resolved.path);
    if (!target) throw new Error('当前组织树没有该目录，请刷新索引后重试');
    let current = catalogState.tree.path;
    catalogState.expandedPaths.add(current);
    const relative = resolved.path.slice(current.length).replace(/^\/+/, '');
    relative.split('/').filter(Boolean).forEach(part => {
      current = current.replace(/\/$/, '') + '/' + part;
      catalogState.expandedPaths.add(current);
    });
    selectTreeNode(target.path, target.name, target.file_count, target.today_added);
    renderCatalogTree();
    findTreeItemEl(target.path)?.scrollIntoView({ block: 'nearest' });
    toast(`已定位到【${target.name}】${options.todayOnly ? '的今日更新文件' : ''}`);
    return true;
  } catch (error) { toast(error.message || '目录定位失败'); return false; }
}

async function handleTestScene(scene) {
  if (scene === 'catalog') {
    switchNavMode('catalog');
    return;
  }
  if (scene === 'catalog_today') {
    switchNavMode('catalog');
    setTimeout(() => {
      if (!catalogState.filterOnlyToday) toggleTodayFilter();
    }, 350);
    return;
  }

  if (scene === 'ai_drawer' || scene === 'ai_drawer_wide') toggleAIChat(true);
}

/* -----------------------------------------------------------
 * 浏览器插件级 AI 侧边栏助手：拖拽拉宽、记忆宽度与多轮对话
 * ----------------------------------------------------------- */
let chatHistory = [];
const aiWelcomeMarkup = $('aiChatBody').innerHTML;

async function copyPathDirect(path) {
  try {
    await navigator.clipboard.writeText(path);
    toast('文件路径已复制到剪贴板！可在资源管理器中粘贴直达');
  } catch {
    prompt('复制文件完整路径：', path);
  }
}

function clearAIChat() {
  if (_aiChatInFlight) return;
  chatHistory = [];
  _aiChatLastText = '';
  $('aiChatBody').innerHTML = aiWelcomeMarkup;
}

function sendQuickPrompt(text) {
  toggleAIChat(true);
  $('aiInput').value = text;
  sendAIChat();
}

let _aiChatInFlight = false;
let _aiChatLastText = '';

async function sendAIChat() {
  if (_aiChatInFlight) { toast('AI 正在推理中，请稍候'); return; }
  const input = $('aiInput');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  _aiChatInFlight = true;
  _aiChatLastText = text;
  const sendBtn = $('sendChatBtn');
  if (sendBtn) sendBtn.disabled = true;
  $('aiClearChat').disabled = true;


  const body = $('aiChatBody');
  body.querySelector('.ai-welcome')?.remove();
  // 追加用户气泡
  body.insertAdjacentHTML('beforeend', `
    <div class="chat-msg user">
      <div class="msg-avatar"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-users"/></svg></div>
      <div class="msg-bubble">${esc(text)}</div>
    </div>
  `);

  // 追加等待占位
  const loadingId = 'aiLoading_' + Date.now();
  body.insertAdjacentHTML('beforeend', `
    <div class="chat-msg bot" id="${loadingId}">
      <div class="msg-avatar"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-sparkles"/></svg></div>
      <div class="msg-bubble" style="display:flex;align-items:center;gap:6px">
        <span class="spinner-anim" style="border-color:#285649;border-top-color:transparent"></span>
        <span style="color:#6f736b;font-size:12px">${currentUser?.demo_mode ? '正在准备回答，演示模式包含节奏等待…' : '正在查找你可访问的文件…'}</span>
      </div>
    </div>
  `);
  body.scrollTop = body.scrollHeight;

  try {
    const res = await api('/api/ai/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, query: $('q')?.value || '', history: chatHistory })
    });

    const loadingEl = $(loadingId);
    if (loadingEl) loadingEl.remove();

    const showAnswerSource = Boolean(res.model_used && res.model_used !== '本地对话引导');
    const modelBadge = $('aiModelBadge');
    if (modelBadge) {
      modelBadge.hidden = !showAnswerSource;
      modelBadge.textContent = showAnswerSource
        ? `由【${res.model_used}】提供支持${res.demo_delay_ms > 0 ? ' · 含演示等待' : ''}`
        : '';
    }

    let extraHtml = '';
    if (res.suggested_terms?.length) {
      extraHtml += `
        <div style="margin-top:12px;font-size:12px;color:#3e413a;font-weight:700">推荐点击重搜词：</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">
          ${res.suggested_terms.map(t => `<button class="chip-btn" style="background:#edf2ed;color:#1e443a;font-size:11.5px;padding:4px 11px;cursor:pointer;border:1px solid #c5d2c2;border-radius:999px;font-weight:600;display:inline-flex;align-items:center;gap:4px" data-ai-search="${esc(t)}">搜“${esc(t)}”</button>`).join('')}
        </div>`;
    }
    const chatFiles = res.matched_files?.length ? res.matched_files : (res.related_files || []);
    const historicalOnly = !res.matched_files?.length && !!res.related_files?.length;
    if (chatFiles.length) {
      extraHtml += `
        <div style="margin-top:14px;font-size:12px;color:#3e413a;font-weight:700;display:flex;align-items:center;gap:6px">
          <span>${historicalOnly ? '历史文件（不符合本次日期范围）' : '符合条件的文件'} (${chatFiles.length})：</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">
          ${chatFiles.map(f => {
            const ext = (f.name.split('.').pop() || 'XLSX').toUpperCase();
            const badgeBg = ext.includes('XLS') ? '#edf2ed' : '#f4f4f3';
            const badgeColor = ext.includes('XLS') ? '#285649' : '#3e413a';
            return `
            <div class="ai-matched-card">
              <div class="ai-card-main">
                <span class="ai-file-badge" style="background:${badgeBg};color:${badgeColor}">${esc(ext)}</span>
                <div class="ai-file-info">
                  <div class="ai-file-title" title="${esc(f.name)}">${esc(f.name)}</div>
                  <div class="ai-file-meta" title="${esc(f.path || '')}">
                    <span>${esc(f.path ? (f.path.length > 55 ? '…' + f.path.slice(-52) : f.path) : '网盘根目录')}</span>
                    ${f.modified_at ? `<span>修改：${esc(f.modified_at)}${f.size === 0 ? ' · 空文件' : ''}</span>` : ''}
                  </div>
                </div>
              </div>
              <div class="ai-card-actions">
                <button class="ai-btn-analyze" data-ai-locate="${esc(f.parent || f.path)}" title="在组织架构树中直达此业务目录">
                  组织树定位
                </button>
                <button class="ai-btn-download" onclick="startDownload(${f.id}, this.closest('.ai-matched-card').querySelector('.ai-file-title').textContent)" title="下载原表格文件（成员需申请审批）">
                  下载原件
                </button>
                <button class="ai-btn-copy" data-ai-copy="${esc(f.path || f.name)}" title="复制文件路径">
                  复制路径
                </button>
              </div>
            </div>`;
          }).join('')}
        </div>`;
    }

    // 后端回复里可能带 emoji 前缀，渲染前统一剥离，保持界面图标体系一致
    const stripEmoji = s => String(s || '')
      .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{1F1E6}-\u{1F1FF}]/gu, '')
      .replace(/^[ \t]+/gm, '');
    let formattedReply = esc(stripEmoji(res.reply))
      .replace(/\*\*(.*?)\*\*/g, '<b>$1</b>')
      .replace(/`(.*?)`/g, '<code style="background:#f4f4f3;padding:1px 5px;border-radius:4px;color:#171916;font-weight:600">$1</code>')
      .replace(/\n/g, '<br>');

    body.insertAdjacentHTML('beforeend', `
      <div class="chat-msg bot">
        <div class="msg-avatar"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-sparkles"/></svg></div>
        <div class="msg-bubble">
          ${formattedReply}
          ${showAnswerSource ? `<div class="ai-answer-source">${esc(res.model_used)}</div>` : ''}
          ${extraHtml}
        </div>
      </div>
    `);
    chatHistory.push({ role: 'user', content: text });
    chatHistory.push({ role: 'assistant', content: res.reply });
  } catch (err) {
    const loadingEl = $(loadingId);
    if (loadingEl) loadingEl.remove();
    body.insertAdjacentHTML('beforeend', `
      <div class="chat-msg bot">
        <div class="msg-avatar"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-alert"/></svg></div>
        <div class="msg-bubble" style="color:var(--danger);background:var(--danger-subtle);border-color:var(--danger-line)">
          查询推理失败：${esc(err.message || '网络异常')}
          <div style="margin-top:8px"><button class="chip-btn" onclick="retryAIChat()">重试</button></div>
        </div>
      </div>
    `);
  } finally {
    _aiChatInFlight = false;
    if (sendBtn) sendBtn.disabled = false;
    $('aiClearChat').disabled = false;
  }
  body.scrollTop = body.scrollHeight;
}

function retryAIChat() {
  if (!_aiChatLastText) return;
  // 移除最后一个错误气泡后重发
  const body = $('aiChatBody');
  const msgs = body.querySelectorAll('.chat-msg.bot');
  const last = msgs[msgs.length - 1];
  if (last && last.textContent.includes('查询推理失败')) last.remove();
  $('aiInput').value = _aiChatLastText;
  sendAIChat();
}


// ==================== 首页大屏与检索双流心流控制 ====================
let selectedResultIndex = 0;

// 格式化提炼事业部名称，彻底消灭省略号截断
function cleanDeptLabel(n) {
  if (!n) return '';
  const parts = n.split('-');
  if (parts.length >= 3) {
    const reg = parts[1].replace('业务部', '').replace('拓展部', '');
    const biz = parts[2].replace('业务部', '').replace('拓展部', '').replace('管理部', '').replace('中心', '');
    return `${reg} · ${biz}`;
  }
  return n.replace(/^\d+-/, '');
}

// Asset overview rendering is defined in portal.js.

function showHomeCockpit() {
  const cockpit = $('homeDataCockpit');
  const ws = $('searchWorkspaceLayout');
  $('searchView').classList.remove('has-results');
  if (cockpit) cockpit.classList.remove('hidden');
  if (ws) ws.classList.add('hidden');
}

function hideHomeCockpit() {
  const cockpit = $('homeDataCockpit');
  const ws = $('searchWorkspaceLayout');
  $('searchView').classList.add('has-results');
  if (cockpit) cockpit.classList.add('hidden');
  if (ws) ws.classList.remove('hidden');
}

function selectDepartmentSearch(deptName) {
  clearPortalSearchFilters();
  $('q').value = deptName;
  search(1);
}

// 查看明细到「全部文件 / 今日更新」明细：进入组织架构视图并选中根节点（全部）
function drillToAllFiles(onlyToday = false) {
  switchNavMode('catalog');
  const applyRoot = () => {
    const root = catalogState.tree;
    if (!root) return;
    // 先设定文件过滤模式，避免 selectTreeNode 内按旧模式加载、再二次加载造成竞态
    catalogState.fileFilterMode = onlyToday ? 'today' : 'all';
    catalogState.page = 1;
    $('tabAllFiles')?.classList.toggle('active', !onlyToday);
    $('tabTodayFiles')?.classList.toggle('active', onlyToday);
    selectTreeNode(root.path, root.name, root.file_count, root.today_added);
  };
  if (catalogState.tree) {
    applyRoot();
  } else {
    // 树异步加载中，轮询等待加载完成后再选中根节点
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      if (catalogState.tree) { clearInterval(timer); applyRoot(); }
      else if (tries > 25) clearInterval(timer);
    }, 200);
  }
}

let aiPreviousFocus = null;
function toggleAIChat(forceOpen) {
  const overlay = $('aiDrawerOverlay');
  const launcher = $('aiRobotLauncher');
  const willOpen = forceOpen !== undefined ? forceOpen : overlay.classList.contains('hidden');
  if (willOpen) aiPreviousFocus = document.activeElement;
  overlay.classList.toggle('hidden', !willOpen);
  launcher.setAttribute('aria-expanded', String(willOpen));
  launcher.setAttribute('aria-label', willOpen ? '收起文件小助手' : '打开文件小助手');
  launcher.classList.toggle('is-open', willOpen);
  window.positionRobotPanel?.();
  if (willOpen) $('aiInput')?.focus();
  else (aiPreviousFocus?.isConnected ? aiPreviousFocus : launcher)?.focus();
}

// Selecting a result never fetches a second, partial preview. The reader owns preview data.
function selectResult(idx, open = false) {
  const result = lastResults[idx];
  if (!result) return;
  selectedResultIndex = idx;
  document.querySelectorAll('.result-card').forEach((el, i) => {
    el.classList.toggle('selected', i === idx);
  });
  if (open) {
    recordPortalEvent('result_click', result.id);
    previewFile(result.id);
  }
}

// Arrow keys select results; Enter or Space opens the selected file.
document.addEventListener('keydown', (e) => {
  if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A'].includes(e.target.tagName) || e.target.isContentEditable) return;
  if (e.key === 'Escape') { toggleAIChat(false); }
  if (!window.FileWorkspace?.isReading() && currentNavMode === 'search' && !$('searchWorkspaceLayout').classList.contains('hidden') && lastResults && lastResults.length) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (selectedResultIndex < lastResults.length - 1) {
        selectResult(selectedResultIndex + 1);
        const el = document.querySelector(`.result-card[data-idx="${selectedResultIndex}"]`);
        if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (selectedResultIndex > 0) {
        selectResult(selectedResultIndex - 1);
        const el = document.querySelector(`.result-card[data-idx="${selectedResultIndex}"]`);
        if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    } else if (e.key === 'Enter') {
      if (lastResults[selectedResultIndex]) previewFile(lastResults[selectedResultIndex].id);
    } else if (e.key === ' ') {
      e.preventDefault();
      if (lastResults[selectedResultIndex]) previewFile(lastResults[selectedResultIndex].id);
    }
  }
});
