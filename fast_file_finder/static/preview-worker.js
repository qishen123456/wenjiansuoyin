// 预览解析 Worker：在后台线程解析 Excel，避免大文件卡死页面
// load 时一次性完整解析（共享字符串表只付一次成本），之后翻页/切表全部毫秒级
try {
  importScripts("/static/xlsx.full.min.js?v=11");
} catch (err) {
  postMessage({ cmd: "error", message: "解析引擎加载失败：" + (err && err.message ? err.message : String(err)) });
  throw err;
}

const MAX_COLS = 200; // 单次渲染列数上限（防止整行格式污染导致的上千列）

function rgb(c) {
  if (!c || !c.rgb) return "";
  let v = String(c.rgb);
  if (v.length === 8) v = v.slice(2);
  return /^[0-9A-Fa-f]{6}$/.test(v) ? "#" + v : "";
}

function cellStyle(cell) {
  const s = cell && cell.s;
  if (!s) return "";
  const css = [];
  const bg = rgb(s.fgColor);
  if (bg && bg.toUpperCase() !== "#000000") css.push("background:" + bg);
  if (s.font) {
    const fc = rgb(s.font.color);
    if (fc) css.push("color:" + fc);
    if (s.font.bold) css.push("font-weight:700");
    if (s.font.italic) css.push("font-style:italic");
    if (s.font.underline) css.push("text-decoration:underline");
    if (s.font.sz) css.push("font-size:" + Math.max(s.font.sz, 10) + "px");
    if (s.font.name) css.push("font-family:'" + s.font.name + "',sans-serif");
  }
  if (s.alignment) {
    if (s.alignment.horizontal) css.push("text-align:" + (s.alignment.horizontal === "centerContinuous" ? "center" : s.alignment.horizontal));
    if (s.alignment.vertical) css.push("vertical-align:" + s.alignment.vertical);
    if (s.alignment.wrapText) css.push("white-space:pre-wrap");
  }
  return css.join(";");
}

function cellText(cell) {
  if (!cell || cell.v === undefined || cell.v === null) return "";
  if (cell.w !== undefined) return String(cell.w);
  if (cell.v instanceof Date) {
    // cellDates 解析出的日期对象兜底格式化为 yyyy/M/d
    return cell.v.getFullYear() + "/" + (cell.v.getMonth() + 1) + "/" + cell.v.getDate();
  }
  try { return XLSX.utils.format_cell(cell); } catch (e) { return String(cell.v); }
}

let fileBuf = null;
let wb = null;           // 完整工作簿（load 时一次性解析，之后所有翻页/切表秒开）
let sheetNames = [];
const sheetMeta = {};    // name -> { totalRows, totalCols }
const pageCache = {};    // "sheet|page" -> pageData

onmessage = function (e) {
  const d = e.data;

  if (d.cmd === "load") {
    try {
      fileBuf = d.buffer;
      // 一次性完整解析：共享字符串表只解析一次，之后所有操作零解析成本
      const t0 = performance.now();
      wb = XLSX.read(fileBuf, { type: "array", cellStyles: true, cellDates: true });
      sheetNames = wb.SheetNames;
      const sheets = sheetNames.map(name => {
        const ws = wb.Sheets[name];
        let rows = 0, cols = 0;
        if (ws && ws["!ref"]) {
          const r = XLSX.utils.decode_range(ws["!ref"]);
          rows = r.e.r - r.s.r + 1;
          cols = r.e.c - r.s.c + 1;
        }
        sheetMeta[name] = { totalRows: rows, totalCols: cols };
        return { name, rows, cols };
      });
      fileBuf = null; // 释放原始字节，减轻内存
      postMessage({ cmd: "loaded", ms: Math.round(performance.now() - t0), sheets });
    } catch (err) {
      postMessage({ cmd: "error", message: "文件解析失败：" + (err && err.message ? err.message : String(err)) });
    }
    return;
  }

  if (d.cmd === "page") {
    const pt0 = performance.now();
    try {
      const name = sheetNames[d.sheet];
      const cacheKey = d.sheet + "|" + d.page;
      if (pageCache[cacheKey]) { postMessage(pageCache[cacheKey]); return; }

      const ws = wb.Sheets[name];
      if (!ws || !ws["!ref"]) {
        const empty = { cmd: "pageData", empty: true, sheet: d.sheet, page: d.page };
        pageCache[cacheKey] = empty;
        postMessage(empty);
        return;
      }
      const range = XLSX.utils.decode_range(ws["!ref"]);
      const meta = sheetMeta[name];
      const totalRows = meta.totalRows;
      const colEnd = Math.min(range.e.c, range.s.c + MAX_COLS - 1);
      const r0 = range.s.r + d.page * d.pageSize;
      const r1 = Math.min(range.e.r, r0 + d.pageSize - 1);
      if (r0 > range.e.r) {
        const empty = { cmd: "pageData", empty: true, sheet: d.sheet, page: d.page };
        pageCache[cacheKey] = empty;
        postMessage(empty);
        return;
      }

      // 与当前页相交的合并区域（截断到页内）
      const merges = [];
      for (const m of (ws["!merges"] || [])) {
        if (m.e.r < r0 || m.s.r > r1 || m.s.c > colEnd) continue;
        merges.push({
          sr: m.s.r, sc: m.s.c,
          er: Math.min(m.e.r, r1), ec: Math.min(m.e.c, colEnd)
        });
      }

      const cols = ws["!cols"] || [];
      const colWidths = [];
      for (let c = range.s.c; c <= colEnd; c++)
        colWidths.push(cols[c] && cols[c].wpx ? Math.min(cols[c].wpx, 420) : 0);

      const rows = [];
      for (let r = r0; r <= r1; r++) {
        const row = [];
        for (let c = range.s.c; c <= colEnd; c++) {
          const cell = ws[XLSX.utils.encode_cell({ r, c })];
          row.push([cellText(cell), cell ? cellStyle(cell) : ""]);
        }
        rows.push(row);
      }

      const out = {
        cmd: "pageData", empty: false, sheet: d.sheet, page: d.page,
        startCol: range.s.c, r0, r1,
        totalRows, hasMore: false, totalCols: meta.totalCols,
        capped: meta.totalCols > MAX_COLS, merges, colWidths, rows,
        ms: Math.round(performance.now() - pt0)
      };
      pageCache[cacheKey] = out;
      postMessage(out);
    } catch (err) {
      postMessage({ cmd: "error", message: "渲染失败：" + (err && err.message ? err.message : String(err)) });
    }
  }
};
