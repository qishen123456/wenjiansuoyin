"""Guarded file writes and database recovery copies; no external storage calls."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid

from fastapi import HTTPException, Request, Query
from starlette.concurrency import run_in_threadpool
import directory_access

_backup_lock = threading.Lock()


def version(path):
    st = path.stat()
    return f'{st.st_mtime_ns}:{st.st_size}:{st.st_ino}'


def storage_dir(app, kind):
    path = Path(os.environ['BACKUP_DIRECTORY']) if kind == 'backups' and os.getenv('BACKUP_DIRECTORY') else app.DB_PATH.parent / kind
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def checked_directory(app, value):
    path = Path(app._resolve_catalog_path(value))
    root = app.get_root()
    if not path.is_dir() or not directory_access.within(str(path.resolve()), str(root)):
        raise HTTPException(403, '文件夹不存在或不在文件根目录内。')
    if path.resolve() != path:
        raise HTTPException(403, '不能向符号链接目录写入文件。')
    relative = path.relative_to(root)
    excluded = {'$RECYCLE.BIN', 'System Volume Information', '.snapshot'}
    if app.DEMO_MODE:
        excluded.add(app.demo_data.UNCLASSIFIED_DIRECTORY)
    if any(part in excluded for part in relative.parts) or any(term in part for part in relative.parts for term in app.SENSITIVE_DIRS):
        raise HTTPException(403, '该目录不允许上传或更新。')
    return path


def require_operation(app, request, path, operation):
    user = app._require_user(request)
    with app.db_session() as conn:
        policy = directory_access.load_policy(conn, user, app.root_scope_key(), str(app.get_root()))
    if policy.decision(str(path), operation) != 'allow':
        raise HTTPException(403, f'没有此文件夹的{"上传" if operation == "upload" else "更新"}权限。')
    if not (app.DEMO_MODE or os.getenv('FILE_WRITES_ENABLED', 'false').lower() in {'1','true','yes'}):
        raise HTTPException(403, '当前数据源只读；请由管理员开启 FILE_WRITES_ENABLED 并配置存储写权限。')
    return user


def safe_filename(app, name):
    if (not name or len(name.encode('utf-8')) > 240 or name in {'.','..'} or name.startswith(('.', '~$'))
            or re.search(r'[\x00-\x1f/\\:*?"<>|]', name) or name.rstrip(' .') != name
            or name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}):
        raise HTTPException(422, '文件名包含不支持的字符或超过长度上限。')
    if Path(name).suffix.lower() not in app.ALLOWED_EXTENSIONS:
        raise HTTPException(415, '此文件格式未加入当前数据源支持范围。')
    if any(term in name for term in app.SENSITIVE_DIRS):
        raise HTTPException(403, '此文件名不允许上传。')
    return name


@contextmanager
def index_unscoped():
    token = directory_access.current.set(None)
    try:
        yield
    finally:
        directory_access.current.reset(token)


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS file_write_audit (
        id TEXT PRIMARY KEY, root_key TEXT NOT NULL, path TEXT NOT NULL,
        actor_id INTEGER NOT NULL, action TEXT NOT NULL, created_at REAL NOT NULL,
        backup_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL)''')


def keep_previous(app, path, actor):
    backup_id = uuid.uuid4().hex
    folder = storage_dir(app, 'file_versions')
    blob = folder / (backup_id + '.bin')
    shutil.copyfile(path, blob)
    os.chmod(blob, 0o600)
    with blob.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    metadata = {'id':backup_id, 'path':str(path), 'root_key':app.root_scope_key(), 'actor_id':actor,
                'created_at':time.time(), 'size':blob.stat().st_size, 'sha256':digest, 'name':path.name}
    (folder / (backup_id+'.json')).write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
    return backup_id


def finish_write(app, path, actor, action, backup_id):
    with index_unscoped(), app.db_session() as conn:
        initialize(conn)
        app.update_index_file(conn, path, path.stat())
        if action in ('update', 'restore'):
            conn.execute("UPDATE download_requests SET expires_at=? WHERE file_path=? AND status='approved' AND expires_at>?", (time.time()-1,str(path),time.time()))
        conn.execute('INSERT INTO file_write_audit VALUES(?,?,?,?,?,?,?,?)',
                     (uuid.uuid4().hex, app.root_scope_key(), str(path), actor, action, time.time(), backup_id, 'complete'))
    app._tree_cache['revision'] = app._tree_cache.get('revision', 0) + 1


def publish_replacement(app, staged, path, actor, action, backup):
    os.chmod(staged, path.stat().st_mode & 0o777)
    staged.replace(path)
    published = version(path)
    try:
        finish_write(app, path, actor, action, backup)
    except Exception:
        if version(path) == published:
            source = storage_dir(app, 'file_versions') / (backup + '.bin')
            shutil.copy2(source, staged)
            staged.replace(path)
        raise


def _database_backup(app):
    folder = storage_dir(app, 'backups')
    name = time.strftime('backup-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.sqlite3'
    pending = folder / (name + '.partial')
    try:
        with closing(sqlite3.connect(app.DB_PATH)) as source, closing(sqlite3.connect(pending)) as destination:
            source.backup(destination, pages=256, sleep=.02)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('备份完整性检查失败')
        os.chmod(pending, 0o600)
        target = folder / name
        pending.replace(target)
        keep = max(2, int(os.getenv('BACKUP_KEEP_COUNT', '7')))
        for old in sorted(folder.glob('backup-*.sqlite3'), key=lambda p:p.stat().st_mtime, reverse=True)[keep:]:
            old.unlink()
        return {'name':name, 'size':target.stat().st_size, 'created_at':target.stat().st_mtime}
    finally:
        pending.unlink(missing_ok=True)


def database_backup(app):
    if not _backup_lock.acquire(blocking=False):
        raise HTTPException(409, '备份任务正在执行，请稍后重试。')
    try:
        return _database_backup(app)
    finally:
        _backup_lock.release()


def backup_loop(app):
    interval = max(0, float(os.getenv('BACKUP_INTERVAL_HOURS', '24'))) * 3600
    if not interval:
        return
    while not app._auto_scan_stop.wait(60):
        try:
            snapshots = list(storage_dir(app, 'backups').glob('backup-*.sqlite3'))
            last = max((p.stat().st_mtime for p in snapshots), default=0)
            if time.time() - last >= interval:
                database_backup(app)
        except Exception:
            app.logger.exception('Scheduled database backup failed')


def verify_backup(path):
    """Recover into an isolated database before reporting a usable snapshot."""
    try:
        with tempfile.TemporaryDirectory(prefix='file-finder-recovery-') as directory:
            recovered = Path(directory) / 'recovered.db'
            source_uri = path.resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(sqlite3.connect(recovered)) as destination:
                source.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise HTTPException(422, '备份损坏，无法恢复。')
                return {table: destination.execute('SELECT count(*) FROM '+table).fetchone()[0]
                        for table in ('files','system_users','directory_grants','archive_rules','download_requests')}
    except sqlite3.DatabaseError:
        raise HTTPException(422, '备份损坏或缺少必要数据，无法恢复。')


def register(app):
    @app.app.get('/api/files/write-context')
    def write_context(request: Request, path: str = ''):
        user=app._require_user(request)
        target=checked_directory(app,path)
        with app.db_session() as conn:
            policy=directory_access.load_policy(conn,user,app.root_scope_key(),str(app.get_root()))
        return {'path':str(target), 'upload':policy.decision(str(target),'upload')=='allow',
                'enabled':app.DEMO_MODE or os.getenv('FILE_WRITES_ENABLED','false').lower() in {'1','true','yes'},
                'max_mb':int(os.getenv('MAX_UPLOAD_FILE_MB','32')),
                'extensions':sorted(app.ALLOWED_EXTENSIONS)}

    @app.app.get('/api/file/{file_id}/write-version')
    def write_version(file_id: int, request: Request):
        info,path=app._get_indexed_file(file_id)
        require_operation(app,request,path.parent,'update')
        return {'version':version(path),'name':path.name,'path':str(path.parent)}

    async def receive(request, parent):
        limit=max(1,min(256,int(os.getenv('MAX_UPLOAD_FILE_MB','32'))))*1024*1024
        size=0
        descriptor,name=tempfile.mkstemp(prefix='.file-finder-upload-',dir=parent)
        staged=Path(name)
        try:
            with os.fdopen(descriptor,'wb') as output:
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>limit:raise HTTPException(413,'文件超过上传大小上限。')
                    await run_in_threadpool(output.write,chunk)
                await run_in_threadpool(output.flush)
                await run_in_threadpool(os.fsync,output.fileno())
            if size==0:raise HTTPException(422,'不能上传空文件。')
            return staged
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    @app.app.post('/api/files/upload')
    async def upload(request: Request,path: str=Query(...),name: str=Query(...)):
        parent=checked_directory(app,path);user=require_operation(app,request,parent,'upload')
        target=parent/safe_filename(app,name)
        if not app._scan_lock.acquire(blocking=False):raise HTTPException(409,'索引或文件写入任务运行中，请稍后重试。')
        staged=None
        try:
            if target.exists() or target.is_symlink():raise HTTPException(409,'同名文件已存在，请改名或使用更新操作。')
            staged=await receive(request,parent)
            checked_directory(app,str(parent))
            require_operation(app,request,parent,'upload')
            # Exclusive publication: never overwrite an externally created same-name file.
            try:
                os.link(staged, target)
            except FileExistsError:raise HTTPException(409,'同名文件刚刚被创建，请改名重试。')
            except OSError:raise HTTPException(422,'数据源不支持安全创建文件或没有写权限，请检查存储配置。')
            try:
                await run_in_threadpool(finish_write,app,target,user['id'],'upload','')
            except Exception:
                if target.exists() and target.samefile(staged):
                    target.unlink()
                raise
            return {'ok':True,'message':'文件已上传并同步索引。','name':name}
        finally:
            if staged:staged.unlink(missing_ok=True)
            app._scan_lock.release()

    @app.app.put('/api/file/{file_id}/content')
    async def update(file_id: int,request: Request,expected_version: str=Query(...)):
        info,path=app._get_indexed_file(file_id);checked_directory(app,str(path.parent))
        user=require_operation(app,request,path.parent,'update')
        if path.is_symlink():raise HTTPException(403,'不能替换符号链接文件。')
        if not app._scan_lock.acquire(blocking=False):raise HTTPException(409,'索引或文件写入任务运行中，请稍后重试。')
        staged=None
        try:
            if version(path)!=expected_version:raise HTTPException(409,'文件已被修改，请刷新后重新选择。')
            staged=await receive(request,path.parent)
            checked_directory(app,str(path.parent))
            if version(path)!=expected_version:raise HTTPException(409,'上传期间原文件发生变化，已取消更新。')
            backup=await run_in_threadpool(keep_previous,app,path,user['id'])
            if version(path)!=expected_version:raise HTTPException(409,'备份期间原文件发生变化，已取消更新。')
            require_operation(app,request,path.parent,'update')
            await run_in_threadpool(publish_replacement,app,staged,path,user['id'],'update',backup)
            return {'ok':True,'message':'文件已更新，旧副本已保留，索引已同步。','backup_id':backup}
        finally:
            if staged:staged.unlink(missing_ok=True)
            app._scan_lock.release()

    @app.app.get('/api/file/{file_id}/versions')
    def versions(file_id: int,request: Request):
        info,path=app._get_indexed_file(file_id);require_operation(app,request,path.parent,'update')
        result=[]
        for metadata in storage_dir(app,'file_versions').glob('*.json'):
            item=json.loads(metadata.read_text(encoding='utf-8'))
            if item['root_key']==app.root_scope_key() and item['path']==str(path):
                result.append({k:item[k] for k in ('id','created_at','size','name','actor_id')})
        return {'items':sorted(result,key=lambda x:x['created_at'],reverse=True)[:100], 'version':version(path)}

    @app.app.post('/api/file/{file_id}/versions/{backup_id}/restore')
    def restore(file_id: int,backup_id: str,request: Request,expected_version: str=Query(...)):
        info,path=app._get_indexed_file(file_id);checked_directory(app,str(path.parent))
        user=require_operation(app,request,path.parent,'update')
        if not re.fullmatch('[a-f0-9]{32}',backup_id):raise HTTPException(404,'恢复副本不存在。')
        folder=storage_dir(app,'file_versions');metadata=folder/(backup_id+'.json');blob=folder/(backup_id+'.bin')
        if not metadata.is_file() or not blob.is_file():raise HTTPException(404,'恢复副本不存在。')
        item=json.loads(metadata.read_text(encoding='utf-8'))
        if item['path']!=str(path) or item['root_key']!=app.root_scope_key():raise HTTPException(403,'恢复副本不属于此文件。')
        with blob.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=item['sha256']:raise HTTPException(422,'副本校验失败。')
        if not app._scan_lock.acquire(blocking=False):raise HTTPException(409,'文件任务运行中，请稍后重试。')
        staged=None
        try:
            if version(path)!=expected_version:raise HTTPException(409,'文件已被修改，请刷新后重试。')
            previous=keep_previous(app,path,user['id'])
            descriptor,temp_name=tempfile.mkstemp(prefix='.file-finder-upload-',dir=path.parent);os.close(descriptor);staged=Path(temp_name)
            shutil.copyfile(blob,staged)
            if version(path)!=expected_version:raise HTTPException(409,'恢复期间文件发生变化，已取消。')
            require_operation(app,request,path.parent,'update')
            publish_replacement(app,staged,path,user['id'],'restore',previous)
            return {'ok':True,'message':'已恢复旧副本，恢复前文件也已保留。'}
        finally:
            if staged:staged.unlink(missing_ok=True)
            app._scan_lock.release()

    @app.app.get('/api/system/backups')
    def backups(request: Request):
        app._require_super_admin(request)
        folder=storage_dir(app,'backups')
        files=[{'name':p.name,'size':p.stat().st_size,'created_at':p.stat().st_mtime} for p in folder.glob('backup-*.sqlite3')]
        return {'items':sorted(files,key=lambda x:x['created_at'],reverse=True)[:100],
                'interval_hours':float(os.getenv('BACKUP_INTERVAL_HOURS','24')),
                'description':'备份索引、账号、权限、规则与审批数据库；不包含网盘原件、上传凭证或环境密钥。',
                'storage':str(folder)}

    @app.app.post('/api/system/backups')
    def create_backup(request: Request):
        app._require_super_admin(request)
        return {'ok':True,**database_backup(app)}

    @app.app.post('/api/system/backups/{name}/verify')
    def verify(name: str,request: Request):
        app._require_super_admin(request)
        if not re.fullmatch(r'backup-[0-9-]+[a-f0-9]{8}\.sqlite3',name):raise HTTPException(404,'备份不存在。')
        path=storage_dir(app,'backups')/name
        if not path.is_file():raise HTTPException(404,'备份不存在。')
        return {'ok':True,'counts':verify_backup(path),'message':'已恢复到隔离数据库并通过完整性检查；未覆盖正在运行的系统。'}
