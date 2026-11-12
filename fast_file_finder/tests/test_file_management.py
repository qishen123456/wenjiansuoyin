import os
import time
from pathlib import Path
import sqlite3

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import file_management
from test_product_foundation import foundation, login
from test_directory_access import secure
from test_operation_permissions import set_permissions


def allow(c,root,monkeypatch,permissions):
    monkeypatch.setenv('FILE_WRITES_ENABLED','true')
    login(c);set_permissions(c,root,permissions=permissions);login(c,admin=False)


def test_upload_cannot_overwrite_and_is_immediately_searchable(secure,monkeypatch):
    c,root,add=secure;add()
    allow(c,root,monkeypatch,{'read':'allow','upload':'allow'})
    url='/api/files/upload';params={'path':str(root/'业务域'),'name':'新增.csv'}
    response=c.post(url,params=params,content=b'a,b\n1,2\n')
    assert response.status_code==200,response.text
    assert (root/'业务域/新增.csv').read_bytes()==b'a,b\n1,2\n'
    assert c.get('/api/search?q=新增').json()['results'][0]['name']=='新增.csv'
    assert c.post(url,params=params,content=b'overwritten').status_code==409
    assert c.post(url,params={**params,'name':'../escape.csv'},content=b'x').status_code==422
    assert not list((root/'业务域').glob('.file-finder-upload-*'))


def test_write_permissions_limits_and_read_only_sources(secure,monkeypatch):
    c,root,add=secure;fid=add();allow(c,root,monkeypatch,{'read':'allow'})
    assert c.post('/api/files/upload',params={'path':str(root/'业务域'),'name':'无权.csv'},content=b'a').status_code==403
    assert c.get(f'/api/file/{fid}/write-version').status_code==403
    login(c);set_permissions(c,root,permissions={'read':'allow','upload':'allow','update':'allow'});login(c,admin=False)
    monkeypatch.setenv('MAX_UPLOAD_FILE_MB','1')
    assert c.post('/api/files/upload',params={'path':str(root/'业务域'),'name':'超限.csv'},content=b'a'*(1024*1024+1)).status_code==413
    assert not (root/'业务域/超限.csv').exists()
    monkeypatch.setenv('FILE_WRITES_ENABLED','false')
    assert c.get(f'/api/file/{fid}/write-version').status_code==403


def test_update_conflicts_keep_old_copy_and_restore(secure,monkeypatch):
    c,root,add=secure;fid=add();path=root/'业务域/报表.csv';old=path.read_bytes()
    allow(c,root,monkeypatch,{'read':'allow','update':'allow'})
    version=c.get(f'/api/file/{fid}/write-version').json()['version']
    updated=c.put(f'/api/file/{fid}/content',params={'expected_version':version},content=b'a,b\n5,6\n')
    assert updated.status_code==200,updated.text
    assert path.read_bytes()==b'a,b\n5,6\n'
    assert c.put(f'/api/file/{fid}/content',params={'expected_version':version},content=b'a').status_code==409
    data=c.get(f'/api/file/{fid}/versions').json()
    assert len(data['items'])==1
    restored=c.post(f"/api/file/{fid}/versions/{data['items'][0]['id']}/restore",params={'expected_version':data['version']})
    assert restored.status_code==200,restored.text
    assert path.read_bytes()==old
    assert len(c.get(f'/api/file/{fid}/versions').json()['items'])==2
    with application.db_session() as conn:
        assert conn.execute('SELECT count(*) FROM file_write_audit').fetchone()[0]==2


def test_database_backup_recovery_is_isolated_and_admin_only(secure):
    c,root,add=secure;add();login(c)
    response=c.post('/api/system/backups');assert response.status_code==200,response.text
    backup=response.json()['name']
    checked=c.post('/api/system/backups/'+backup+'/verify')
    assert checked.status_code==200,checked.text
    assert checked.json()['counts']['files']==1
    add('业务域/新文件.csv')
    assert c.post('/api/system/backups/'+backup+'/verify').json()['counts']['files']==1
    with application.db_session() as conn:assert conn.execute('SELECT count(*) FROM files').fetchone()[0]==2
    login(c,admin=False)
    assert c.get('/api/system/backups').status_code==403
    assert c.post('/api/system/backups').status_code==403


def test_upload_only_empty_folder_does_not_grant_read(secure,monkeypatch):
    c,root,add=secure;(root/'业务域').mkdir()
    allow(c,root,monkeypatch,{'upload':'allow'})
    r=c.post('/api/files/upload',params={'path':str(root/'业务域'),'name':'新增.csv'},content=b'a,b\n1,2')
    assert r.status_code==200,r.text
    assert c.get('/api/search?q=新增').json()['results']==[]


def test_failed_upload_and_update_roll_back_files_and_release_lock(secure,monkeypatch):
    c,root,add=secure
    fid=add()
    path=root/'业务域/报表.csv'
    before=path.read_bytes()
    allow(c,root,monkeypatch,{'read':'allow','upload':'allow','update':'allow'})
    def fail(*args,**kwargs):
        raise sqlite3.OperationalError('simulated full disk')
    monkeypatch.setattr(file_management,'finish_write',fail)
    # TestClient propagates unexpected errors, as intended for fault injection.
    import pytest
    with pytest.raises(sqlite3.OperationalError):
        c.post('/api/files/upload',params={'path':str(path.parent),'name':'失败.csv'},content=b'a,b')
    assert not (path.parent/'失败.csv').exists()
    expected=c.get(f'/api/file/{fid}/write-version').json()['version']
    with pytest.raises(sqlite3.OperationalError):
        c.put(f'/api/file/{fid}/content',params={'expected_version':expected},content=b'new data')
    assert path.read_bytes()==before
    assert not list(path.parent.glob('.file-finder-upload-*'))
    assert application._scan_lock.acquire(blocking=False)
    application._scan_lock.release()


def test_corrupt_backup_produces_actionable_error(secure):
    c,root,add=secure
    add();login(c)
    folder=file_management.storage_dir(application,'backups')
    name='backup-20261107-123456-abcdef12.sqlite3'
    (folder/name).write_bytes(b'corrupt database')
    result=c.post('/api/system/backups/'+name+'/verify')
    assert result.status_code==422
    assert '损坏' in result.json()['detail']


def test_scan_lock_blocks_writes_and_backup_retention(secure,monkeypatch):
    c,root,add=secure
    fid=add()
    allow(c,root,monkeypatch,{'read':'allow','upload':'allow','update':'allow'})
    assert application._scan_lock.acquire(blocking=False)
    try:
        result=c.post('/api/files/upload',params={'path':str(root/'业务域'),'name':'冲突.csv'},content=b'a,b')
        assert result.status_code==409
        expected=c.get(f'/api/file/{fid}/write-version').json()['version']
        assert c.put(f'/api/file/{fid}/content',params={'expected_version':expected},content=b'a,b').status_code==409
    finally:
        application._scan_lock.release()
    login(c);monkeypatch.setenv('BACKUP_KEEP_COUNT','2')
    for i in range(3):
        assert c.post('/api/system/backups').status_code==200
    items=c.get('/api/system/backups').json()['items']
    assert len(items)==2
    assert c.post('/api/system/backups/'+items[0]['name']+'/verify').status_code==200
