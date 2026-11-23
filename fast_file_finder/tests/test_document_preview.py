"""Document previews preserve ACL/download boundaries and inert HTML content."""
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import base64
import sys

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
from test_product_foundation import foundation, login
from test_directory_access import secure
from test_operation_permissions import set_permissions
from test_media_preview import index_path


def make_pdf(path, pages=3):
    images = [Image.new('RGB', (120, 80), color) for color in ['red', 'green', 'blue']]
    images[0].save(path, save_all=True, append_images=[images[i % 3] for i in range(1, pages)])


def test_real_pdf_preview_pages_and_read_only_acl(secure, monkeypatch):
    client, root, _ = secure
    monkeypatch.setattr(application, 'ALLOWED_EXTENSIONS', {'.pdf'})
    path = root / '业务域' / '报告.pdf'
    path.parent.mkdir()
    make_pdf(path)
    fid = index_path(path)
    assert client.get(f'/api/file/{fid}/preview-data').status_code == 401
    login(client)
    set_permissions(client, root, permissions={'read': 'allow'})
    login(client, admin=False)
    response = client.get(f'/api/file/{fid}/preview-data?page=2')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['kind'] == 'pdf' and data['page_count'] == 3 and data['page'] == 2
    assert not data['truncated']
    asset = client.get(data['asset_url'])
    assert asset.status_code == 200 and asset.headers['content-type'] == 'image/png'
    assert asset.headers['cache-control'] == 'no-store'
    assert Image.open(BytesIO(asset.content)).size == (240, 160)
    assert client.get(f'/api/file/{fid}/download').status_code == 403
    assert client.get(f'/api/file/{fid}/preview-data?page=4').status_code == 422
    login(client)
    set_permissions(client, root, permissions={'read': 'deny'})
    login(client, admin=False)
    assert client.get(data['asset_url']).status_code == 404
    assert client.get(f'/api/file/{fid}/preview-data').status_code == 404


@pytest.mark.parametrize(('extension', 'encoding'), [('.html', 'utf-8-sig'), ('.htm', 'gb18030')])
def test_html_preserves_chinese_tables_and_rechecks_acl(secure, monkeypatch, extension, encoding):
    client, root, _ = secure
    monkeypatch.setattr(application, 'ALLOWED_EXTENSIONS', {extension})
    path = root / '业务域' / ('报告' + extension)
    path.parent.mkdir()
    path.write_bytes('<h1>月度报告</h1><table style="border-collapse:collapse"><tr><th>金额</th><td style="color:#123456">100</td></tr></table>'.encode(encoding))
    fid = index_path(path)
    assert client.get(f'/api/file/{fid}/preview-data').status_code == 401
    login(client)
    set_permissions(client, root, permissions={'read': 'allow'})
    login(client, admin=False)
    response = client.get(f'/api/file/{fid}/preview-data')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['kind'] == 'html' and data['page_count'] == 1 and data['page'] == 1
    assert '<h1>月度报告</h1>' in data['html'] and '<th>金额</th>' in data['html']
    assert 'color:#123456' in data['html'] and 'border-collapse:collapse' in data['html']
    assert 'asset_url' not in data and response.headers['cache-control'] == 'no-store'
    assert client.get(f'/api/file/{fid}/download').status_code == 403
    assert client.get(f'/api/file/{fid}/preview-data?page=2').status_code == 422
    login(client)
    set_permissions(client, root, permissions={'read': 'deny'})
    login(client, admin=False)
    assert client.get(f'/api/file/{fid}/preview-data').status_code == 404


class Elements(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.items = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))


def test_html_removes_active_content_and_network_sources(foundation, monkeypatch):
    client, root, add = foundation
    monkeypatch.setattr(application, 'ALLOWED_EXTENSIONS', {'.html'})
    source = '''<html><head><base href="https://invalid.example/"><meta http-equiv="refresh" content="0;url=/api/health"><style>@import url(/api/health)</style></head>
    <body onload="fetch('/api/health')"><h1 onclick="alert(1)">可读正文</h1>
    <script>parent.fetch('/api/auth/me'); document.cookie</script>
    <a href="javascript:alert(1)" target="_top">链接正文</a>
    <img src="/api/health" onerror="alert(1)"><img src="https://invalid.example/x">
    <iframe srcdoc="<script>alert(1)</script>"></iframe><object data="/api/health"></object>
    <form action="/api/auth/logout"><input value="test"></form>
    <svg><a href="javascript:alert(1)">SVG</a></svg><math><mtext>math</mtext></math>
    <div style="background:url(/api/health)">外部样式</div>
    <div style="background:u\\72l(/api/health)">转义样式</div>
    <p title="&quot; onmouseover=&quot;alert(1)">安全属性转义</p></body></html>'''
    fid = add('业务域/不可信.html', content=source)
    response = client.get(f'/api/file/{fid}/preview-data')
    assert response.status_code == 200, response.text
    document = response.json()['html']
    elements = Elements(document).items
    assert not {'script', 'iframe', 'object', 'embed', 'form', 'input', 'svg', 'math', 'base'} & {tag for tag, _ in elements}
    for tag, attrs in elements:
        assert not any(key.startswith('on') for key in attrs)
        assert not {'href', 'srcdoc', 'action', 'formaction', 'srcset'} & attrs.keys()
        assert not attrs.get('src', '').startswith(('http', '/', 'javascript:'))
    policies = [attrs['content'] for tag, attrs in elements if tag == 'meta' and attrs.get('http-equiv') == 'Content-Security-Policy']
    assert len(policies) == 1
    for directive in ["default-src 'none'", "script-src 'none'", "object-src 'none'", "base-uri 'none'", "form-action 'none'"]:
        assert directive in policies[0]
    assert '/api/' not in document and 'parent.fetch' not in document
    assert '可读正文' in document and '链接正文' in document


def test_document_errors_and_size_limits(foundation, monkeypatch):
    client, root, add = foundation
    monkeypatch.setattr(application, 'ALLOWED_EXTENSIONS', {'.pdf', '.html'})
    for name, content in [('bad.pdf', 'not a PDF'), ('empty.pdf', ''), ('empty.html', ''), ('blank.html', '  \n\t')]:
        fid = add(name, content=content)
        response = client.get(f'/api/file/{fid}/preview-data')
        assert response.status_code == 422
        if name == 'bad.pdf':
            assert '损坏或已加密' in response.json()['detail']
    fid = add('large.html', content='x' * (2 * 1024 * 1024 + 1))
    assert client.get(f'/api/file/{fid}/preview-data').status_code == 413
    path = root / 'large.pdf'
    with path.open('wb') as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    fid = index_path(path)
    assert client.get(f'/api/file/{fid}/preview-data').status_code == 413


def test_html_bounds_encoding_complexity_and_embedded_images(tmp_path, monkeypatch):
    import html_preview
    path = tmp_path / 'sample.html'
    for raw in (b'\xff', b'<p>binary\0data</p>'):
        path.write_bytes(raw)
        with pytest.raises(application.HTTPException) as failure:
            html_preview.render(path)
        assert failure.value.status_code == 422 and '编码' in failure.value.detail
    path.write_text('<br>' * (html_preview.MAX_ELEMENTS + 1))
    with pytest.raises(application.HTTPException) as failure:
        html_preview.render(path)
    assert failure.value.status_code == 422 and '结构过于复杂' in failure.value.detail
    for source in ['<div>' * (html_preview.MAX_DEPTH + 1), '<svg>' + '<g>' * html_preview.MAX_DEPTH]:
        path.write_text(source)
        with pytest.raises(application.HTTPException) as failure:
            html_preview.render(path)
        assert failure.value.status_code == 422 and '嵌套层级' in failure.value.detail
    image = BytesIO()
    Image.new('RGB', (12, 12), 'green').save(image, format='PNG')
    raster = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
    svg = 'data:image/svg+xml;base64,' + base64.b64encode(b'<svg onload="alert(1)"></svg>').decode()
    path.write_text(f'<img src="{raster}" alt="安全图"><img src="{svg}" alt="向量图"><img src="data:text/html;base64,PHNjcmlwdD4=">')
    document = html_preview.render(path)['html']
    images = [attrs for tag, attrs in Elements(document).items if tag == 'img']
    assert images == [{'src': raster, 'alt': '安全图'}]
    path.write_text('<img src="data:image/png;base64,bm90IGFuIGltYWdl" alt="坏图像">')
    assert not any(tag == 'img' for tag, _ in Elements(html_preview.render(path)['html']).items)
    path.write_text(f'<img src="{raster}"><img src="{raster}">')
    monkeypatch.setattr(html_preview, 'MAX_TOTAL_IMAGE_PIXELS', 200)
    assert len([tag for tag, _ in Elements(html_preview.render(path)['html']).items if tag == 'img']) == 1
    monkeypatch.setattr(html_preview, 'MAX_IMAGE_PIXELS', 100)
    assert not any(tag == 'img' for tag, _ in Elements(html_preview.render(path)['html']).items)


def test_html_detects_changes_during_sanitising(foundation, monkeypatch):
    import html_preview
    client, root, add = foundation
    monkeypatch.setattr(application, 'ALLOWED_EXTENSIONS', {'.html'})
    fid = add('changing.html', content='<p>before</p>')
    original = html_preview.sanitise
    def change_source(source):
        result = original(source)
        (root / 'changing.html').write_text('<p>new version changed concurrently</p>')
        return result
    monkeypatch.setattr(html_preview, 'sanitise', change_source)
    assert client.get(f'/api/file/{fid}/preview-data').status_code == 409


def test_pdf_preview_reports_page_truncation(foundation, monkeypatch):
    client, root, _ = foundation
    monkeypatch.setattr(application, 'ALLOWED_EXTENSIONS', {'.pdf'})
    path = root / 'long.pdf'
    make_pdf(path, pages=101)
    fid = index_path(path)
    response = client.get(f'/api/file/{fid}/preview-data')
    assert response.status_code == 200, response.text
    assert response.json()['page_count'] == 100 and response.json()['truncated']
    assert client.get(f'/api/file/{fid}/preview-data?page=101').status_code == 422
