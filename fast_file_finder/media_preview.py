"""Bounded image/slide previews, rendered in a timed subprocess and served via ACL routes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile

from fastapi import HTTPException

IMAGE_EXTENSIONS={'.png','.jpg','.jpeg','.webp'}
SLIDE_EXTENSIONS={'.ppt','.pptx'}
EXTENSIONS=IMAGE_EXTENSIONS|SLIDE_EXTENSIONS
_gate=threading.BoundedSemaphore(1)
MAX_SOURCE=32*1024*1024
MAX_PAGES=100


def _key(path):
    st=path.stat()
    return hashlib.sha256(f'fonts-v2\0{path}\0{st.st_size}\0{st.st_mtime_ns}'.encode()).hexdigest()


def _prune(folder,active):
    candidates=sorted((p for p in folder.iterdir() if p.is_dir() and p.name!=active),key=lambda p:p.stat().st_mtime)
    sizes={p:sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) for p in candidates}
    total=sum(sizes.values())
    for p in candidates:
        if time.time()-p.stat().st_mtime>1800 or total>256*1024*1024:
            total-=sizes[p];shutil.rmtree(p)


def render(path,cache_root,page=1):
    if path.suffix.lower() not in EXTENSIONS:raise HTTPException(415,'此格式不支持图片或幻灯片预览。')
    if path.stat().st_size>MAX_SOURCE:raise HTTPException(413,'图片与 PPT 预览最多支持 32 MB 原文件。')
    if path.stat().st_size==0:raise HTTPException(422,'空文件无法预览。')
    if not 1<=page<=MAX_PAGES:raise HTTPException(422,'仅预览前 100 页。')
    key=_key(path);folder=cache_root/'media_previews';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=folder/key;asset=target/f'page-{page}.png';manifest=target/'manifest.json'
    if not _gate.acquire(blocking=False):raise HTTPException(429,'预览转换繁忙，请稍后重试。',headers={'Retry-After':'2'})
    try:
        if not asset.is_file() or not manifest.is_file():
            target.mkdir(exist_ok=True)
            command=[sys.executable,str(Path(__file__).resolve()),'--render',str(path),str(target),str(page)]
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=os.name!='nt')
            try:
                stdout,stderr=process.communicate(timeout=75)
            except subprocess.TimeoutExpired:
                if os.name!='nt':os.killpg(process.pid,signal.SIGKILL)
                else:process.kill()
                process.communicate();shutil.rmtree(target,ignore_errors=True)
                raise HTTPException(504,'预览转换超时，请拆分文件或申请下载。')
            if process.returncode:
                try:message=json.loads(stdout.decode('utf-8'))['error']
                except Exception:message='无法转换此文件，请检查文件是否损坏或加密。'
                raise HTTPException(422,message)
        if _key(path)!=key:
            shutil.rmtree(target,ignore_errors=True)
            raise HTTPException(409,'文件转换期间发生变化，请刷新预览。')
        data=json.loads(manifest.read_text(encoding='utf-8'))
        if page>data['page_count']:raise HTTPException(422,'页码超出预览范围。')
        _prune(folder,key)
        return data,asset
    finally:
        _gate.release()


def _worker(path,target,page):
    from PIL import Image,ImageOps
    Image.MAX_IMAGE_PIXELS=25_000_000
    ext=path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        if page!=1:raise ValueError('图片只有一页。')
        with Image.open(path) as source:
            if source.width*source.height>25_000_000:raise ValueError('图片像素超过预览上限，请缩小后重试。')
            image=ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((1920,1920));image.save(target/'page-1.png','PNG')
        data={'kind':'image','page_count':1,'truncated':False}
    else:
        pdf=target/'source.pdf'
        if not pdf.exists():
            if ext=='.pptx':
                with zipfile.ZipFile(path) as archive:
                    if len(archive.infolist())>20000 or sum(x.file_size for x in archive.infolist())>128*1024*1024:
                        raise ValueError('文稿解压后超过预览资源上限。')
                    from xml.etree import ElementTree
                    for item in archive.infolist():
                        if item.filename.endswith('.rels'):
                            root=ElementTree.fromstring(archive.read(item))
                            if any(r.attrib.get('TargetMode')=='External' and not r.attrib.get('Type','').endswith('/hyperlink') for r in root):
                                raise ValueError('文稿含外部关联资源，请先嵌入资源后重试。')
            binary=os.getenv('LIBREOFFICE_BIN') or shutil.which('soffice') or shutil.which('libreoffice')
            if not binary:raise ValueError('服务器尚未安装 PPT 转换组件 LibreOffice。')
            profile=target/'profile';profile.mkdir(exist_ok=True)
            user=profile/'user';user.mkdir(exist_ok=True)
            (user/'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>''',encoding='utf-8')
            converter_env = os.environ.copy()
            if sys.platform == 'darwin':
                font_dir = Path('/System/Library/Fonts/Supplemental')
                if font_dir.is_dir():
                    converter_env.setdefault('SAL_FONTPATH_PRIVATE', str(font_dir))
                    if not converter_env.get('FONTCONFIG_FILE'):
                        from xml.sax.saxutils import escape
                        config = profile / 'fonts.conf'
                        config.write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><dir>'
                            + escape(str(font_dir)) + '</dir><cachedir>'
                            + escape(str(profile / 'font-cache')) + '</cachedir></fontconfig>', encoding='utf-8')
                        converter_env['FONTCONFIG_FILE'] = str(config.resolve())
            local=target/('source'+ext);shutil.copyfile(path,local)
            try:
                subprocess.run([binary,'-env:UserInstallation='+profile.resolve().as_uri(),'--headless','--nologo','--nodefault','--nofirststartwizard','--convert-to','pdf:impress_pdf_Export','--outdir',str(target),str(local)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,env=converter_env)
            finally:
                local.unlink(missing_ok=True);shutil.rmtree(profile,ignore_errors=True)
            if not pdf.is_file() or pdf.stat().st_size>128*1024*1024:raise ValueError('文稿转换失败或输出过大。')
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(pdf) as document:
            count=min(len(document),MAX_PAGES)
            if not 1<=page<=count:raise ValueError('页码超出预览范围。')
            slide=document[page-1]
            try:
                width,height=slide.get_size()
                scale=min(2,1920/max(width,height))
                bitmap=slide.render(scale=scale)
                try:bitmap.to_pil().convert('RGB').save(target/f'page-{page}.png','PNG')
                finally:bitmap.close()
            finally:slide.close()
            data={'kind':'slides','page_count':count,'truncated':len(document)>MAX_PAGES}
    (target/'manifest.json').write_text(json.dumps(data),encoding='utf-8')


if __name__=='__main__':
    try:
        if sys.platform.startswith('linux'):
            import resource
            resource.setrlimit(resource.RLIMIT_AS,(1536*1024*1024,1536*1024*1024))
            resource.setrlimit(resource.RLIMIT_FSIZE,(128*1024*1024,128*1024*1024))
            resource.setrlimit(resource.RLIMIT_CPU,(65,65))
        _worker(Path(sys.argv[2]),Path(sys.argv[3]),int(sys.argv[4]))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:240]},ensure_ascii=False));sys.exit(1)
