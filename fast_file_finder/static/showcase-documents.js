/* Static document rendering for the isolated showcase. No API or network calls.
 * Always assign buildHTMLPreview() to an iframe with an EMPTY sandbox attribute.
 * The allowlist, detached template and CSP are independent containment layers.
 */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ShowcaseDocuments = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const MAX_SOURCE = 2 * 1024 * 1024;
  const MAX_ELEMENTS = 20000;
  const MAX_DEPTH = 128;
  const MAX_IMAGE_SOURCE = 700000;
  const CSP = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; font-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
  const ALLOWED_TAGS = new Set(('a abbr address article aside b bdi bdo blockquote br caption center cite code col colgroup dd del details div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main mark nav ol p pre q s samp section small span strong sub summary sup table tbody td tfoot th thead time tr u ul var wbr').split(' '));
  const DROP_CONTENT = new Set(('script style iframe object embed applet svg math form input button select textarea template noscript noembed noframes xmp plaintext title audio video canvas frameset frame base link meta').split(' '));
  const VOID_TAGS = new Set(['br', 'col', 'hr', 'img', 'wbr']);
  const STYLE_PROPERTIES = new Set(('color background background-color font font-family font-size font-style font-weight font-variant line-height letter-spacing word-spacing text-align text-decoration text-indent text-transform white-space word-break overflow-wrap vertical-align border border-width border-style border-color border-top border-right border-bottom border-left border-collapse border-spacing border-radius padding padding-top padding-right padding-bottom padding-left margin margin-top margin-right margin-bottom margin-left width min-width max-width height min-height max-height display list-style-type table-layout').split(' '));

  function escapeHTML(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const safeExponent = Math.max(0, exponent);
    const amount = bytes / (1024 ** safeExponent);
    return `${safeExponent === 0 ? Math.round(amount) : Number(amount.toFixed(1))} ${units[safeExponent]}`;
  }

  function parseCSV(value) {
    const source = String(value ?? '').replace(/^\uFEFF/, '');
    if (source.length > MAX_SOURCE) throw new Error('CSV 预览最多支持 2 MB 文本，请选择较小的文件。');
    const rows = [];
    let row = [], field = '', quoted = false, closedQuote = false, pendingRow = false, truncated = false;
    function finishField() {
      if (row.length < 50) row.push(field);
      else truncated = true;
      field = '';
      closedQuote = false;
    }
    function finishRow() {
      finishField();
      if (rows.length < 200) rows.push(row);
      else truncated = true;
      row = [];
      pendingRow = false;
    }
    for (let index = 0; index < source.length; index += 1) {
      const character = source[index];
      pendingRow = true;
      if (quoted) {
        if (character !== '"') field += character;
        else if (source[index + 1] === '"') { field += '"'; index += 1; }
        else { quoted = false; closedQuote = true; }
        continue;
      }
      if (character === ',') finishField();
      else if (character === '\n' || character === '\r') {
        if (character === '\r' && source[index + 1] === '\n') index += 1;
        finishRow();
      } else if (character === '"' && !field && !closedQuote) quoted = true;
      else if (closedQuote || character === '"') throw new Error('CSV 引号位置不正确，请检查字段的引号与分隔符。');
      else field += character;
    }
    if (quoted) throw new Error('CSV 存在未闭合的引号，请检查文件内容。');
    if (pendingRow) finishRow();
    return { rows, truncated };
  }

  function sanitizeStyle(value) {
    return String(value ?? '').slice(0, 4000).split(';').flatMap(declaration => {
      const separator = declaration.indexOf(':');
      if (separator < 1) return [];
      const property = declaration.slice(0, separator).trim().toLowerCase();
      const raw = declaration.slice(separator + 1).trim();
      // Deliberately exclude escapes, comments, at-rules, custom properties and
      // resource functions. CSP also denies resource retrieval if parsing differs.
      if (!STYLE_PROPERTIES.has(property) || !/^[\w\s#.,%()+\-/'"]{1,300}$/.test(raw)) return [];
      if (/(?:url|expression|image-set|attr)\s*\(/i.test(raw)) return [];
      return [`${property}:${raw}`];
    }).join(';');
  }

  function safeImageSource(value) {
    const source = String(value ?? '');
    if (source.length > MAX_IMAGE_SOURCE) return '';
    if (/^data:image\/(?:png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(source)) return source;
    // Blob sources are only useful when created by this browser; arbitrary URLs,
    // SVG/data HTML and protocol-relative resource addresses are never retained.
    if (/^blob:https?:\/\/[a-z0-9.\-\[\]:]+\/[a-z0-9-]+$/i.test(source)) return source;
    return '';
  }

  function buildHTMLPreview(source, { title = '文件预览' } = {}) {
    if (typeof document === 'undefined') throw new Error('HTML 预览需要浏览器文档环境。');
    const text = String(source ?? '');
    if (text.length > MAX_SOURCE) throw new Error('HTML 预览最多支持 2 MB 文本，请选择较小的文件。');
    // Template content is inert and remains disconnected throughout parsing.
    // It is never mounted: only strings rebuilt from the allowlist leave here.
    const template = document.createElement('template');
    template.innerHTML = text;
    let count = 0;
    function render(node, depth) {
      if (++count > MAX_ELEMENTS || depth > MAX_DEPTH) throw new Error('HTML 结构过于复杂，无法在演示中预览。');
      if (node.nodeType === 3) return escapeHTML(node.textContent);
      if (node.nodeType !== 1) return '';
      const tag = node.localName.toLowerCase();
      if (DROP_CONTENT.has(tag)) return '';
      const children = () => Array.from(node.childNodes, child => render(child, depth + 1)).join('');
      if (!ALLOWED_TAGS.has(tag)) return children();
      const clean = {};
      for (const attribute of node.attributes) {
        const { name, value } = attribute;
        if (['title', 'lang', 'dir'].includes(name)) clean[name] = value.slice(0, 500);
        else if (name === 'style') {
          const style = sanitizeStyle(value);
          if (style) clean.style = style;
        } else if (tag === 'img' && name === 'src') {
          const src = safeImageSource(value);
          if (src) clean.src = src;
        } else if (tag === 'img' && name === 'alt') clean.alt = value.slice(0, 500);
        else if (['td', 'th'].includes(tag) && ['colspan', 'rowspan'].includes(name) && /^\d{1,3}$/.test(value) && Number(value) > 0) clean[name] = value;
        else if (['img', 'table', 'td', 'th', 'col'].includes(tag) && ['width', 'height'].includes(name) && /^\d{1,4}%?$/.test(value)) clean[name] = value;
      }
      if (tag === 'img' && !clean.src) return escapeHTML(clean.alt || '');
      const attributes = Object.entries(clean).map(([name, value]) => ` ${name}="${escapeHTML(value)}"`).join('');
      return `<${tag}${attributes}>${VOID_TAGS.has(tag) ? '' : `${children()}</${tag}>`}`;
    }
    const body = Array.from(template.content.childNodes, node => render(node, 0)).join('');
    return '<!doctype html><html lang="zh-CN"><head>' +
      `<meta http-equiv="Content-Security-Policy" content="${escapeHTML(CSP)}">` +
      `<meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>${escapeHTML(title)}</title>` +
      '<meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<style>body{margin:24px;color:#292d29;font:15px/1.7 system-ui,sans-serif;overflow-wrap:anywhere}h1,h2,h3{color:#285649}table{border-collapse:collapse;max-width:100%}th,td{border:1px solid #dddfd7;padding:8px 12px;text-align:left}th{background:#edf2ed}img{max-width:100%;height:auto}pre{white-space:pre-wrap}a{color:inherit;text-decoration:none}*{box-sizing:border-box}</style>' +
      `</head><body>${body}</body></html>`;
  }

  return Object.freeze({ escapeHTML, formatBytes, parseCSV, sanitizeStyle, safeImageSource, buildHTMLPreview });
});
