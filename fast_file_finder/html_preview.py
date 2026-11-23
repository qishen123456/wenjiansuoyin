"""Bounded, inert HTML previews. Never fetch resources or return source HTML.

The returned document must be displayed in an iframe with an empty sandbox
attribute (no allow-scripts or allow-same-origin). The tag/attribute allowlist
and document CSP independently remove active content and network requests.
"""
import base64
import binascii
from html import escape
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import re
import warnings

from fastapi import HTTPException
from PIL import Image


EXTENSIONS = {'.html', '.htm'}
MAX_SOURCE = 2 * 1024 * 1024
MAX_ELEMENTS = 20_000
MAX_DEPTH = 256
MAX_EMBEDDED_IMAGE = 512 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_TOTAL_IMAGE_PIXELS = 25_000_000
CSP = (
    "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'none'; font-src 'none'; media-src 'none'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
_ALLOWED_TAGS = set('''a abbr address article aside b bdi bdo blockquote br caption
    center cite code col colgroup dd del details div dl dt em figcaption figure
    footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main mark nav ol p pre q
    s samp section small span strong sub summary sup table tbody td tfoot th thead
    time tr u ul var wbr'''.split())
_DROP_CONTENT = set('''script style iframe object embed applet svg math form input
    button select textarea template noscript noembed noframes xmp plaintext title
    audio video canvas frameset frame'''.split())
_VOID_TAGS = set('area base br col embed hr img input link meta param source track wbr'.split())
_STYLE_PROPERTIES = set('''color background background-color font font-family font-size
    font-style font-weight font-variant line-height letter-spacing word-spacing
    text-align text-decoration text-indent text-transform white-space word-break
    overflow-wrap vertical-align border border-width border-style border-color
    border-top border-right border-bottom border-left border-collapse border-spacing
    border-radius padding padding-top padding-right padding-bottom padding-left
    margin margin-top margin-right margin-bottom margin-left width min-width max-width
    height min-height max-height display list-style-type table-layout'''.split())
_STYLE_VALUE = re.compile(r'''^[\w\s#.,%()+\-/'"]{1,300}$''')
_RASTER_DATA = re.compile(r'^data:image/(?:png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)$', re.I)


def _style(value):
    # No escapes, comments, URLs, at-rules or executable legacy CSS. Retain only
    # presentational declarations; all external resources are also blocked by CSP.
    declarations = []
    for declaration in value[:4000].split(';'):
        name, separator, raw = declaration.partition(':')
        name, raw = name.strip().lower(), raw.strip()
        if (separator and name in _STYLE_PROPERTIES and _STYLE_VALUE.fullmatch(raw)
                and not re.search(r'url\s*\(|expression\s*\(|image-set\s*\(', raw, re.I)):
            declarations.append(f'{name}:{raw}')
    return ';'.join(declarations)


def _image(value):
    match = _RASTER_DATA.fullmatch(value)
    if not match or len(value) > MAX_EMBEDDED_IMAGE * 4 // 3 + 100:
        return '', 0
    try:
        data = base64.b64decode(re.sub(r'\s', '', match[1]), validate=True)
    except (binascii.Error, ValueError):
        return '', 0
    if len(data) > MAX_EMBEDDED_IMAGE:
        return '', 0
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                pixels = image.width * image.height
                if (image.format not in {'PNG', 'JPEG', 'GIF', 'WEBP'} or pixels > MAX_IMAGE_PIXELS
                        or max(image.size) > 8192 or getattr(image, 'n_frames', 1) > 1):
                    return '', 0
                image.verify()
    except Exception:
        # Malformed images must not break an otherwise readable HTML document.
        return '', 0
    return value, pixels


class _StaticHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.blocked = []
        self.open_tags = []
        self.elements = 0
        self.image_pixels = 0

    def handle_starttag(self, tag, attrs):
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            raise HTTPException(422, 'HTML 内容结构过于复杂，请简化后重试。')
        if self.blocked:
            if tag not in _VOID_TAGS:
                if len(self.blocked) >= MAX_DEPTH:
                    raise HTTPException(422, 'HTML 嵌套层级过深，请简化后重试。')
                self.blocked.append(tag)
            return
        if tag in _DROP_CONTENT:
            if tag not in _VOID_TAGS:
                self.blocked.append(tag)
            return
        if tag not in _ALLOWED_TAGS:
            return
        clean = {}
        for name, value in attrs:
            value = value or ''
            if name in {'title', 'lang', 'dir'}:
                clean[name] = value[:500]
            elif name == 'style':
                style = _style(value)
                if style:
                    clean[name] = style
            elif tag in {'td', 'th'} and name in {'colspan', 'rowspan'}:
                if value.isascii() and value.isdigit() and 1 <= int(value[:6]) <= 1000 and len(value) <= 4:
                    clean[name] = value
            elif tag == 'img' and name == 'src':
                src, pixels = _image(value)
                if src and self.image_pixels + pixels <= MAX_TOTAL_IMAGE_PIXELS:
                    clean[name] = src
                    self.image_pixels += pixels
            elif tag == 'img' and name == 'alt':
                clean[name] = value[:500]
            elif tag in {'img', 'table', 'td', 'th', 'col'} and name in {'width', 'height'}:
                if re.fullmatch(r'\d{1,4}%?', value):
                    clean[name] = value
        if tag == 'img' and 'src' not in clean:
            if clean.get('alt'):
                self.parts.append(escape(clean['alt']))
            return
        attributes = ''.join(f' {name}="{escape(value, quote=True)}"' for name, value in clean.items())
        self.parts.append(f'<{tag}{attributes}>')
        if tag not in _VOID_TAGS:
            if len(self.open_tags) >= MAX_DEPTH:
                raise HTTPException(422, 'HTML 嵌套层级过深，请简化后重试。')
            self.open_tags.append(tag)

    def handle_endtag(self, tag):
        if self.blocked:
            if tag in self.blocked:
                position = len(self.blocked) - 1 - self.blocked[::-1].index(tag)
                del self.blocked[position:]
            return
        if tag in self.open_tags:
            position = len(self.open_tags) - 1 - self.open_tags[::-1].index(tag)
            self.parts.extend(f'</{name}>' for name in reversed(self.open_tags[position:]))
            del self.open_tags[position:]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(escape(data))


def sanitise(source):
    parser = _StaticHTML()
    parser.feed(source)
    parser.close()
    parser.parts.extend(f'</{tag}>' for tag in reversed(parser.open_tags))
    body = ''.join(parser.parts)
    return (
        '<!doctype html><html lang="zh-CN"><head>'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(CSP, quote=True)}">'
        '<meta charset="utf-8"><meta name="referrer" content="no-referrer">'
        '<style>body{margin:24px;color:#292d29;font:15px/1.6 system-ui,sans-serif;overflow-wrap:anywhere}'
        'table{border-collapse:collapse;max-width:100%}th,td{border:1px solid #dddfd7;padding:8px 12px}'
        'img{max-width:100%;height:auto}pre{white-space:pre-wrap}</style>'
        f'</head><body>{body}</body></html>'
    )


def render(path: Path):
    if path.suffix.lower() not in EXTENSIONS:
        raise HTTPException(415, '此格式不支持 HTML 预览。')
    before = path.stat()
    if before.st_size > MAX_SOURCE:
        raise HTTPException(413, 'HTML 预览最多支持 2 MB 原文件，请精简文件或申请下载。')
    with path.open('rb') as source:
        raw = source.read(MAX_SOURCE + 1)
    if len(raw) > MAX_SOURCE:
        raise HTTPException(413, 'HTML 预览最多支持 2 MB 原文件。')
    if not raw.strip():
        raise HTTPException(422, '空 HTML 文件，暂无可预览内容。')
    if b'\0' in raw:
        raise HTTPException(422, 'HTML 编码不受支持，请使用 UTF-8 或 GB18030。')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            if encoding == 'gb18030':
                raise HTTPException(422, 'HTML 编码无法识别，请使用 UTF-8 或 GB18030。')
    document = sanitise(text)
    after = path.stat()
    if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
        raise HTTPException(409, '文件正在更新，请重新打开预览。')
    return {'kind': 'html', 'html': document, 'encoding': encoding, 'page_count': 1, 'truncated': False}
