from pathlib import Path
import sys
import shutil
import subprocess

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_product_foundation import foundation,login
from test_directory_access import secure
from test_operation_permissions import set_permissions
import app as application
import media_preview
from PIL import Image
import pytest


def index_path(path):
    with application.db_session() as conn:
        application.update_index_file(conn,path,path.stat())
        return conn.execute('SELECT id FROM files WHERE path=?',(str(path),)).fetchone()[0]


@pytest.mark.parametrize('extension', ['.png', '.jpg', '.jpeg', '.webp'])
def test_image_preview_is_rendered_and_rechecks_permissions(secure,monkeypatch,extension):
    c,root,add=secure;monkeypatch.setattr(application,'ALLOWED_EXTENSIONS',{extension})
    path=root/('业务域/示例'+extension);path.parent.mkdir();Image.new('RGB',(2600,1000),'blue').save(path)
    fid=index_path(path);login(c);set_permissions(c,root,permissions={'read':'allow'});login(c,admin=False)
    response=c.get(f'/api/file/{fid}/preview-data');assert response.status_code==200,response.text
    assert response.json()['kind']=='image'
    media=c.get(response.json()['asset_url']);assert media.status_code==200
    from io import BytesIO
    assert Image.open(BytesIO(media.content)).width==1920
    login(c);set_permissions(c,root,permissions={'read':'deny'});login(c,admin=False)
    assert c.get(f'/api/file/{fid}/preview-media').status_code==404


def test_media_preview_limits_and_invalid_content(foundation,monkeypatch):
    c,root,add=foundation;monkeypatch.setattr(application,'ALLOWED_EXTENSIONS',{'.png'})
    fid=add('业务域/伪装.png',content='not an image')
    assert c.get(f'/api/file/{fid}/preview-data').status_code==422
    assert c.get(f'/api/file/{fid}/preview-data?page=101').status_code==422
    assert media_preview._gate.acquire(blocking=False)
    try:assert c.get(f'/api/file/{fid}/preview-data').status_code==429
    finally:media_preview._gate.release()


@pytest.mark.skipif(not shutil.which('soffice') and not shutil.which('libreoffice'),reason='requires LibreOffice')
def test_real_pptx_conversion_and_paging(foundation,monkeypatch):
    pptx=pytest.importorskip('pptx')
    c,root,add=foundation;monkeypatch.setattr(application,'ALLOWED_EXTENSIONS',{'.pptx','.ppt'})
    path=root/'测试文稿.pptx';presentation=pptx.Presentation()
    for title in ('业务说明','目录权限'):
        slide=presentation.slides.add_slide(presentation.slide_layouts[0]);slide.shapes.title.text=title
    presentation.save(path);fid=index_path(path)
    response=c.get(f'/api/file/{fid}/preview-data');assert response.status_code==200,response.text
    assert response.json()['kind']=='slides' and response.json()['page_count']==2
    # A successful conversion must preserve visible Chinese, not just page count.
    _, rendered = media_preview.render(path, application.DB_PATH.parent)
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(rendered.parent / 'source.pdf') as document:
        assert '业务说明' in document[0].get_textpage().get_text_range()
        assert '目录权限' in document[1].get_textpage().get_text_range()
    with Image.open(rendered) as image:
        assert image.convert('L').getextrema()[0] < 200

    assert c.get(f'/api/file/{fid}/preview-media?page=2').status_code==200
    assert c.get(f'/api/file/{fid}/preview-data?page=3').status_code==422

    # Exercise the separate legacy binary import filter as well.
    binary = shutil.which('soffice') or shutil.which('libreoffice')
    subprocess.run([binary, '-env:UserInstallation='+(root/'lo-test-profile').as_uri(),
                    '--headless','--convert-to','ppt:MS PowerPoint 97','--outdir',str(root),str(path)],
                   check=True,capture_output=True,timeout=60)
    legacy_id=index_path(path.with_suffix('.ppt'))
    legacy=c.get(f'/api/file/{legacy_id}/preview-data')
    assert legacy.status_code==200,legacy.text
    assert legacy.json()['page_count']==2
