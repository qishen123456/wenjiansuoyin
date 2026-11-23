const test = require('node:test');
const assert = require('node:assert/strict');
const { escapeHTML, formatBytes, parseCSV, sanitizeStyle, safeImageSource, buildHTMLPreview } = require('../static/showcase-documents.js');

test('file names and text cannot inject markup or escape attribute boundaries', () => {
  assert.equal(escapeHTML('<img src=x onerror="alert(1)">\'&'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&#39;&amp;');
  assert.equal(escapeHTML(null), '');
  assert.equal(escapeHTML('季度经营简报'), '季度经营简报');
});

test('format bytes handles invalid, fractional and large sizes without invalid units', () => {
  assert.equal(formatBytes(-1), '0 B');
  assert.equal(formatBytes(Infinity), '0 B');
  assert.equal(formatBytes(0.5), '1 B');
  assert.equal(formatBytes(1024), '1 KB');
  assert.equal(formatBytes(1536), '1.5 KB');
  assert.equal(formatBytes(1024 ** 4), '1 TB');
});

test('inline presentation survives while CSS resource requests and escaped functions do not', () => {
  assert.equal(sanitizeStyle('color: #285649; padding: 8px 12px; background: url(https://host/x); position: fixed'), 'color:#285649;padding:8px 12px');
  assert.equal(sanitizeStyle('background: u\\72l(https://host/x); width: expression(alert(1)); background: image-set(foo); color: red/*x*/; @import: x; --value: red; width: attr(data-value)'), '');
  assert.equal(sanitizeStyle('font-family: "PingFang SC", sans-serif; border: 1px solid #ddd'), 'font-family:"PingFang SC", sans-serif;border:1px solid #ddd');
});

test('only bounded embedded raster data and browser blob image sources are retained', () => {
  assert.equal(safeImageSource('data:image/png;base64,iVBORw0KGgo='), 'data:image/png;base64,iVBORw0KGgo=');
  assert.equal(safeImageSource('blob:http://127.0.0.1:8080/766f3aba-1234'), 'blob:http://127.0.0.1:8080/766f3aba-1234');
  for (const source of ['https://host/image.png', '//host/a.png', '/api/private', 'javascript:alert(1)', 'data:image/svg+xml,<svg/>', 'data:text/html;base64,aGVsbG8=', 'data:image/png;base64,' + 'a'.repeat(700000)]) {
    assert.equal(safeImageSource(source), '', source.slice(0, 80));
  }
});

test('HTML sanitizer explicitly requires an actual browser, not a fallback string parser', () => {
  assert.throws(() => buildHTMLPreview('<p>test</p>'), /浏览器/);
});

test('CSV parses BOM, CRLF, quoted commas, doubled quotes and multiline cells', () => {
  const result = parseCSV('\uFEFF名称,备注,金额\r\n"供应商,甲","包含""引号""",1200\r\n供应商乙,"两行\r\n内容",800\r\n');
  assert.deepEqual(result, {
    rows: [['名称', '备注', '金额'], ['供应商,甲', '包含"引号"', '1200'], ['供应商乙', '两行\r\n内容', '800']],
    truncated: false,
  });
});

test('CSV retains empty cells without manufacturing an extra row after the final newline', () => {
  assert.deepEqual(parseCSV(''), { rows: [], truncated: false });
  assert.deepEqual(parseCSV('a,,\n\n""'), { rows: [['a', '', ''], [''], ['']], truncated: false });
  assert.deepEqual(parseCSV(','), { rows: [['', '']], truncated: false });
  assert.deepEqual(parseCSV('\r\n'), { rows: [['']], truncated: false });
});

test('CSV reports malformed and unterminated quotes instead of silently shifting columns', () => {
  assert.throws(() => parseCSV('name,"unfinished\nnext,row'), /未闭合/);
  assert.throws(() => parseCSV('name,"closed"trailing'), /引号位置/);
  assert.throws(() => parseCSV('name,in"side'), /引号位置/);
  assert.throws(() => parseCSV('x'.repeat(2 * 1024 * 1024 + 1)), /2 MB/);
});

test('CSV output is bounded and marks actual truncation only', () => {
  const exactRows = Array.from({ length: 200 }, (_, index) => `${index},value`).join('\n');
  assert.equal(parseCSV(exactRows + '\n').truncated, false);
  const extraRows = parseCSV(exactRows + '\n200,last');
  assert.equal(extraRows.rows.length, 200);
  assert.equal(extraRows.truncated, true);
  const columns = Array.from({ length: 51 }, (_, index) => String(index));
  assert.equal(parseCSV(columns.slice(0, 50).join(',')).truncated, false);
  const extraColumns = parseCSV(columns.join(','));
  assert.deepEqual(extraColumns.rows[0], columns.slice(0, 50));
  assert.equal(extraColumns.truncated, true);
  // Validation still covers text after the displayed row limit.
  assert.throws(() => parseCSV(exactRows + '\n"unclosed'), /未闭合/);
});
