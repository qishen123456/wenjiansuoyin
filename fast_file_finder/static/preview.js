/* Standalone reader and same-origin file workspace embed. No parent globals required. */
(() => {
  'use strict';

  const params = new URLSearchParams(location.search);
  const fileId = Number(params.get('id'));
  const embedded = params.get('embedded') === '1';
  const validId = Number.isSafeInteger(fileId) && fileId > 0;
  const get = id => document.getElementById(id);
  const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
  const state = {
    sheet: '', page: 1, data: null, name: '', busy: false, sequence: 0,
    previewController: null, blobURL: '', zoom: false, rotation: 0,
    downloadBusy: false, permission: null, permissionSequence: 0,
    authRequiredSent: false, disposed: false,
  };
  const lifetime = new AbortController();
  const downloadURLs = new Set();
  const downloadTimers = new Set();
  let resizeFrame = 0;
  let lastHeight = 0;
  let lastGridWidth = 0;

  document.body.classList.toggle('is-embedded', embedded);
  if (validId) {
    get('standalone').href = `/static/preview.html?id=${fileId}`;
    get('standalone').hidden = !embedded;
  }

  function on(element, event, callback) {
    element.addEventListener(event, callback, { signal: lifetime.signal });
  }

  function authHeaders() {
    // sessionStorage may be unavailable in a browser privacy mode.
    try {
      const token = sessionStorage.getItem('fileFinderAccessToken');
      return token ? { 'X-App-Token': token } : {};
    } catch {
      return {};
    }
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin', ...options,
      signal: options.signal || lifetime.signal,
      headers: { ...authHeaders(), ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      notifyAuthenticationRequired(response.status);
      const error = new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请稍后重试');
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function sendToWorkspace(message) {
    if (embedded && validId && window.parent !== window && !state.disposed) {
      window.parent.postMessage({ ...message, fileId }, location.origin);
    }
  }

  function notifyAuthenticationRequired(status) {
    if (status !== 401 || state.authRequiredSent) return;
    state.authRequiredSent = true;
    sendToWorkspace({ type: 'file-workspace:auth-required' });
  }

  function reportHeight() {
    if (!embedded || state.disposed || resizeFrame) return;
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      // Measure the content, never viewport/scrollHeight: iframe height must not feed back.
      const height = Math.max(480, Math.min(1400, Math.ceil(get('reader').getBoundingClientRect().height)));
      if (height !== lastHeight) {
        lastHeight = height;
        sendToWorkspace({ type: 'file-workspace:resize', height });
      }
    });
  }

  function setName(name) {
    const filename = String(name || '文件预览');
    const extension = filename.match(/\.([^.]+)$/)?.[1] || '';
    get('title').textContent = filename.replace(/\.[^.]+$/, '') || filename;
    get('originalName').textContent = filename;
    document.title = `${filename} · 文件预览`;
    if (filename !== state.name) {
      state.name = filename;
      sendToWorkspace({ type: 'file-workspace:metadata', name: filename });
    }
    get('fileType').textContent = extension ? `${extension.toUpperCase()} 文件` : '文档预览';
  }

  function setActionStatus(message, error = false) {
    get('actionStatus').textContent = message;
    get('actionStatus').hidden = !message;
    get('actionStatus').classList.toggle('is-error', error);
    reportHeight();
  }

  function releasePreview() {
    if (state.blobURL) URL.revokeObjectURL(state.blobURL);
    state.blobURL = '';
  }

  function setLoading() {
    state.busy = true;
    get('grid').setAttribute('aria-busy', 'true');
    get('grid').classList.remove('is-media', 'is-html');
    get('grid').innerHTML = '<div class="reader-state"><span class="loading-mark" aria-hidden="true"></span><p>正在读取当前页…</p></div>';
    get('info').textContent = '正在读取当前页';
    get('prev').disabled = get('next').disabled = true;
    get('mediaTools').querySelectorAll('button').forEach(button => { button.disabled = true; });
    get('sheets').querySelectorAll('button').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.sheet === state.sheet));
    });
  }

  function showPreviewError(error) {
    get('grid').classList.remove('is-media', 'is-html');
    if (!state.name) get('title').textContent = '文件暂时无法预览';
    const authError = error.status === 401;
    const title = authError ? '请登录后查看文件' : error.status === 404 || error.status === 403 ? '暂时无法访问此文件' : '暂时无法预览';
    get('grid').innerHTML = `<div class="reader-state is-error" role="alert"><h2>${title}</h2><p>${escapeHTML(error.message)}</p>${authError ? '<a href="/" target="_top">返回文件中心登录</a>' : '<p>可点击“刷新预览”重试，或按下载权限获取原件。</p>'}</div>`;
    get('info').textContent = '预览未完成';
    get('page').textContent = '—';
    get('prev').disabled = get('next').disabled = true;
  }

  function renderTable(data) {
    releasePreview();
    get('mediaTools').hidden = true;
    get('sheets').hidden = false;
    get('sheets').replaceChildren(...data.sheets.map(name => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = name;
      button.title = name;
      button.dataset.sheet = name;
      button.setAttribute('aria-pressed', String(name === state.sheet));
      return button;
    }));
    get('grid').classList.remove('is-media');
    if (data.rows.length) {
      get('grid').innerHTML = `<table aria-label="${escapeHTML(state.sheet)}工作表，第 ${state.page} 页"><thead><tr><th class="row" scope="col">行</th>${Array.from({ length: data.column_count }, (_, index) => `<th scope="col">列 ${index + 1}</th>`).join('')}</tr></thead><tbody>${data.rows.map((row, index) => `<tr><th scope="row" class="row">${data.start_row + index}</th>${Array.from({ length: data.column_count }, (_, column) => `<td>${escapeHTML(row[column])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
    } else {
      get('grid').innerHTML = '<div class="reader-state"><h2>此工作表暂无内容</h2><p>可以切换其他工作表，或按权限下载原件查看。</p></div>';
    }
    get('notice').textContent = '仅预览前 10,000 行、100 列及前 50 个工作表，单元格最多 2,000 字符；公式显示已保存的计算值，不执行公式。' + (data.truncated ? ' 此文件已达到预览范围上限，完整内容请按权限下载原件。' : '');
    get('notice').parentElement.classList.toggle('is-warning', Boolean(data.truncated));
    const totalPages = Math.max(1, Math.ceil(data.row_count / (data.page_size || 100)));
    get('page').textContent = `第 ${state.page} / ${totalPages} 页`;
    get('info').textContent = `${data.rows_truncated ? '已读取前' : '共'} ${Number(data.row_count).toLocaleString('zh-CN')} 行 · ${data.column_count} 列`;
    get('fileType').textContent += ` · ${data.sheets.length} 个工作表`;
    get('prev').disabled = state.page <= 1;
    get('next').disabled = !data.has_more;
  }

  function layoutMedia() {
    const image = get('previewMedia');
    const stage = get('mediaStage');
    if (!image?.naturalWidth || !stage) return;
    const sideways = Math.abs(state.rotation % 180) === 90;
    const naturalWidth = sideways ? image.naturalHeight : image.naturalWidth;
    const naturalHeight = sideways ? image.naturalWidth : image.naturalHeight;
    const padding = getComputedStyle(get('grid'));
    const available = Math.max(120, get('grid').clientWidth - parseFloat(padding.paddingLeft) - parseFloat(padding.paddingRight));
    const scale = state.zoom ? 1 : Math.min(1, available / naturalWidth, 470 / naturalHeight);
    stage.style.width = `${naturalWidth * scale}px`;
    stage.style.height = `${naturalHeight * scale}px`;
    image.style.width = `${image.naturalWidth * scale}px`;
    image.style.height = `${image.naturalHeight * scale}px`;
    image.style.transform = `translate(-50%, -50%) rotate(${state.rotation}deg)`;
    get('zoomMedia').textContent = state.zoom ? '适应窗口' : '放大';
    get('zoomMedia').setAttribute('aria-pressed', String(state.zoom));
    reportHeight();
  }

  function renderHtml(data) {
    if (typeof data.html !== 'string' || !data.html.trim()) throw new Error('HTML 预览内容为空或格式无效，请刷新预览重试');
    releasePreview();
    get('sheets').hidden = true;
    get('mediaTools').hidden = true;
    get('grid').classList.add('is-html');
    const frame = document.createElement('iframe');
    frame.className = 'html-preview-frame';
    frame.title = `${state.name} · HTML 文档内容`;
    // The server sanitizes the document and supplies CSP. Keep this additional
    // boundary opaque: no script, same-origin, navigation, form or popup grants.
    frame.setAttribute('sandbox', '');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.srcdoc = data.html;
    get('grid').replaceChildren(frame);
    get('notice').textContent = 'HTML 静态预览，保留正文、表格与部分内联样式；脚本、表单和外部资源不加载，排版可能与原网页不同。';
    get('notice').parentElement.classList.toggle('is-warning', Boolean(data.truncated));
    get('info').textContent = 'HTML 文档 · 在内容区内滚动阅读';
    get('page').textContent = '第 1 / 1 页';
    get('prev').disabled = get('next').disabled = true;
  }

  function renderMedia(data, blobURL) {
    releasePreview();
    state.blobURL = blobURL;
    get('sheets').hidden = true;
    get('mediaTools').hidden = false;
    get('mediaTools').querySelectorAll('button').forEach(button => { button.disabled = false; });
    get('grid').classList.add('is-media');
    get('grid').innerHTML = '<div class="media-stage" id="mediaStage"><img id="previewMedia"></div>';
    const image = get('previewMedia');
    image.alt = `${state.name}，第 ${state.page} 页预览`;
    image.onload = layoutMedia;
    image.onerror = () => { showPreviewError(new Error('预览图未能显示，请刷新预览重试')); };
    image.src = blobURL;
    const notices = {
      slides: '静态幻灯片预览，不播放动画与视频；字体与排版可能与原件不同。',
      pdf: 'PDF 分页预览，页面已转换为图像；可放大、旋转查看，原件请按下载权限获取。',
      image: '图片已缩放为预览图，原件请按下载权限获取。',
    };
    get('notice').textContent = notices[data.kind] + (data.truncated ? ' 当前仅展示部分页面，完整内容请按权限下载原件。' : '');
    get('notice').parentElement.classList.toggle('is-warning', Boolean(data.truncated));
    get('page').textContent = `第 ${state.page} / ${data.page_count} 页`;
    get('info').textContent = data.kind === 'image' ? '图片预览' : `${data.kind === 'pdf' ? 'PDF 文档' : '演示文稿'} · 共 ${data.page_count} 页`;
    get('prev').disabled = state.page <= 1;
    get('next').disabled = state.page >= data.page_count;
  }

  async function load({ sheet = state.sheet, page = state.page } = {}) {
    if (!validId || state.disposed) return;
    state.previewController?.abort();
    const controller = new AbortController();
    state.previewController = controller;
    const sequence = ++state.sequence;
    state.sheet = sheet;
    state.page = page;
    setLoading();
    try {
      const data = await api(`/api/file/${fileId}/preview-data?${new URLSearchParams({ sheet, page, page_size: 100 })}`, { signal: controller.signal });
      if (sequence !== state.sequence || state.disposed) return;
      setName(data.name);
      // Spreadsheet responses predate kind. All other types need an explicit
      // renderer; a new kind must never accidentally trigger a media request.
      const kind = data.kind === undefined ? 'table' : data.kind;
      if (!['table', 'image', 'slides', 'pdf', 'html'].includes(kind)) {
        throw new Error(`暂不支持此文件的预览类型（${String(kind).slice(0, 40)}），请按权限下载原件查看`);
      }
      const isMedia = ['image', 'slides', 'pdf'].includes(kind);
      let blobURL = '';
      if (isMedia) {
        const asset = new URL(data.asset_url, location.origin);
        if (asset.origin !== location.origin || asset.pathname !== `/api/file/${fileId}/preview-media`) throw new Error('预览资源地址无效，请重新打开文件');
        const response = await fetch(asset.href, { credentials: 'same-origin', headers: authHeaders(), signal: controller.signal });
        if (!response.ok) {
          notifyAuthenticationRequired(response.status);
          const errorData = await response.json().catch(() => ({}));
          const error = new Error(typeof errorData.detail === 'string' ? errorData.detail : '预览加载失败');
          error.status = response.status;
          throw error;
        }
        const blob = await response.blob();
        // An aborted or superseded media request must never own an object URL.
        if (sequence !== state.sequence || state.disposed) return;
        blobURL = URL.createObjectURL(blob);
      }
      if (sequence !== state.sequence || state.disposed) return;
      state.data = data;
      state.sheet = data.sheet || '';
      state.page = data.page || page;
      if (isMedia) renderMedia(data, blobURL);
      else if (kind === 'html') renderHtml(data);
      else renderTable(data);
      get('grid').scrollTop = 0;
      get('grid').scrollLeft = 0;
    } catch (error) {
      if (sequence !== state.sequence || error.name === 'AbortError' || state.disposed) return;
      showPreviewError(error);
    } finally {
      if (sequence === state.sequence && !state.disposed) {
        state.busy = false;
        get('grid').setAttribute('aria-busy', 'false');
        reportHeight();
      }
    }
  }

  function renderPermission() {
    const labels = { direct: '可下载原件', approval: '下载需审批', deny: '无下载权限' };
    get('permission').textContent = labels[state.permission] || '下载权限尚未确认';
    get('download').textContent = state.downloadBusy ? '正在处理…' : state.permission === 'approval' ? '申请下载' : '下载原件';
    get('download').disabled = state.downloadBusy || !state.permission || state.permission === 'deny';
  }

  async function loadPermissions() {
    const sequence = ++state.permissionSequence;
    try {
      const permissions = await api(`/api/file/${fileId}/permissions`);
      const mode = ['direct', 'approval', 'deny'].includes(permissions.download) ? permissions.download : null;
      if (sequence === state.permissionSequence && !state.disposed) {
        state.permission = mode;
        renderPermission();
      }
      // A download action uses its own fresh check, never another request's UI state.
      return mode;
    } catch (error) {
      if (error.name === 'AbortError' || sequence !== state.permissionSequence || state.disposed) return null;
      state.permission = null;
      renderPermission();
      get('permission').textContent = error.status === 401 ? '登录后可查看下载权限' : '暂时无法确认下载权限';
      return null;
    }
  }

  async function saveDownload(ticket = '') {
    const url = `/api/file/${fileId}/download?${new URLSearchParams({ ticket })}`;
    const headers = authHeaders();
    // Native attachment download avoids keeping a large original file in memory.
    // Token-protected deployments require fetch so the header is sent.
    const tokenProtected = Boolean(headers['X-App-Token']);
    const response = await fetch(url, { method: tokenProtected ? 'GET' : 'HEAD', headers, credentials: 'same-origin', signal: lifetime.signal });
    if (!response.ok) {
      notifyAuthenticationRequired(response.status);
      const data = tokenProtected ? await response.json().catch(() => ({})) : {};
      throw new Error(typeof data.detail === 'string' ? data.detail : '下载未完成，请刷新权限或重新申请');
    }
    const link = document.createElement('a');
    link.download = state.name || '文件';
    if (tokenProtected) {
      const objectURL = URL.createObjectURL(await response.blob());
      downloadURLs.add(objectURL);
      link.href = objectURL;
      const timer = setTimeout(() => { URL.revokeObjectURL(objectURL); downloadURLs.delete(objectURL); downloadTimers.delete(timer); }, 60000);
      downloadTimers.add(timer);
    } else link.href = url;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setActionStatus('已开始下载原件。');
  }

  async function processRequest(reason = '') {
    const result = await api('/api/downloads/requests', { method: 'POST', body: JSON.stringify({ file_id: fileId, reason }) });
    get('downloadRequest').hidden = true;
    if (result.status === 'approved' && result.ticket) await saveDownload(result.ticket);
    else setActionStatus('下载申请已提交，等待管理员审批。可在文件中心的“下载审批”查看进度。');
  }

  async function startDownload() {
    if (state.downloadBusy) return;
    state.downloadBusy = true;
    renderPermission();
    setActionStatus('');
    try {
      const mode = await loadPermissions();
      if (mode === 'direct') await saveDownload();
      else if (mode === 'approval') {
        try { await processRequest(); } catch (error) {
          if (error.status !== 400) throw error;
          get('downloadRequest').hidden = false;
          get('requestError').hidden = true;
          get('downloadReason').focus();
        }
      } else if (mode === 'deny') setActionStatus('你没有此文件的下载权限。', true);
      else setActionStatus('暂时无法确认下载权限，请刷新预览后重试。', true);
    } catch (error) {
      if (error.name !== 'AbortError') setActionStatus(error.message, true);
    } finally {
      state.downloadBusy = false;
      renderPermission();
      reportHeight();
    }
  }

  on(get('retry'), 'click', () => { load(); loadPermissions(); });
  on(get('prev'), 'click', () => { if (!state.busy && state.page > 1) load({ page: state.page - 1 }); });
  on(get('next'), 'click', () => { if (!state.busy && !get('next').disabled) load({ page: state.page + 1 }); });
  on(get('sheets'), 'click', event => {
    const button = event.target.closest('button[data-sheet]');
    if (button && button.dataset.sheet !== state.sheet) load({ sheet: button.dataset.sheet, page: 1 });
  });
  on(get('zoomMedia'), 'click', () => { state.zoom = !state.zoom; layoutMedia(); });
  on(get('rotateLeft'), 'click', () => { state.rotation = (state.rotation - 90) % 360; layoutMedia(); });
  on(get('rotateRight'), 'click', () => { state.rotation = (state.rotation + 90) % 360; layoutMedia(); });
  on(get('download'), 'click', startDownload);
  on(get('cancelRequest'), 'click', () => { get('downloadRequest').hidden = true; get('download').focus(); reportHeight(); });
  on(get('requestForm'), 'submit', async event => {
    event.preventDefault();
    const reason = get('downloadReason').value.trim();
    if (!reason || state.downloadBusy) { get('downloadReason').focus(); return; }
    state.downloadBusy = true;
    get('submitRequest').disabled = true;
    get('requestError').hidden = true;
    renderPermission();
    try { await processRequest(reason); } catch (error) {
      if (error.name !== 'AbortError') { get('requestError').textContent = error.message; get('requestError').hidden = false; }
    } finally {
      state.downloadBusy = false;
      get('submitRequest').disabled = false;
      renderPermission();
      reportHeight();
    }
  });

  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
    const width = get('grid').clientWidth;
    if (width !== lastGridWidth) { lastGridWidth = width; layoutMedia(); }
    reportHeight();
  }) : null;
  observer?.observe(get('reader'));
  observer?.observe(get('grid'));
  on(window, 'resize', () => { layoutMedia(); reportHeight(); });
  on(window, 'pagehide', event => {
    if (event.persisted) return;
    state.disposed = true;
    state.previewController?.abort();
    observer?.disconnect();
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    releasePreview();
    downloadTimers.forEach(clearTimeout);
    downloadURLs.forEach(url => URL.revokeObjectURL(url));
    lifetime.abort();
  });

  if (validId) {
    load();
    loadPermissions();
    api('/api/auth/me').then(data => {
      if (!data.user) return;
      // Decorative CSS content keeps the watermark out of the document's text flow.
      get('watermark').replaceChildren(...Array.from({ length: 12 }, () => {
        const mark = document.createElement('span');
        mark.dataset.label = `${data.user.name} ${data.user.phone} · 仅供预览`;
        return mark;
      }));
    }).catch(() => {});
  } else {
    get('title').textContent = '无法打开文件';
    get('permission').textContent = '缺少有效的文件编号';
    get('grid').setAttribute('aria-busy', 'false');
    get('retry').disabled = true;
    showPreviewError(new Error('文件链接缺少有效参数，请返回文件中心重新打开。'));
  }
  reportHeight();
})();
