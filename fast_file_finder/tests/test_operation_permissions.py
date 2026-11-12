"""Operation grants preserve legacy ACLs and enforce downloads at every entry point."""
import json
import sqlite3
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import directory_access
from test_product_foundation import foundation, login
from test_directory_access import secure, grant, ids


def set_permissions(c, root, path='业务域', subject='4', permissions=None, subject_type='user'):
    response = c.post('/api/system/directory-grants', json={
        'subject_type': subject_type, 'subject_key': subject,
        'target_path': str(root / path), 'permissions': permissions})
    assert response.status_code == 200, response.text


def batch(c, root, **options):
    body = {'subject_type':'user', 'subject_keys':['4'], 'target_paths':[str(root / '业务域')],
            'permissions':{'read':'allow'}, **options}
    preview = c.post('/api/system/directory-grants/batch', json=body)
    assert preview.status_code == 200, preview.text
    return c.post('/api/system/directory-grants/batch', json={**body, 'preview':False, 'revision':preview.json()['revision']})


def test_legacy_schema_migration_is_repeatable_and_retains_rules(tmp_path):
    conn = sqlite3.connect(tmp_path/'old.db');conn.row_factory=sqlite3.Row
    conn.execute('''CREATE TABLE directory_grants(id INTEGER PRIMARY KEY, root_key TEXT,
        subject_type TEXT,subject_key TEXT,target_path TEXT,effect TEXT,created_by INTEGER,created_at REAL,
        UNIQUE(root_key,subject_type,subject_key,target_path))''')
    conn.execute("INSERT INTO directory_grants VALUES(1,'root','user','4','/share/a','allow',1,1)")
    directory_access.initialize(conn);directory_access.initialize(conn)
    row=conn.execute('SELECT * FROM directory_grants').fetchone()
    assert directory_access.grant_permissions(row)=={'read':'allow','download':'legacy'}
    policy=directory_access.load_policy(conn,{'id':4,'role':'employee','department':''},'root','/share')
    assert policy.visible('/share/a/child')
    assert policy.decision('/share/a','download')=='approval'
    assert policy.decision('/share/a','upload')==policy.decision('/share/a','update')=='deny'
    conn.close()


def test_read_only_does_not_allow_original_download_or_analysis_export(secure):
    c, root, add=secure;fid=add();login(c)
    set_permissions(c,root,permissions={'read':'allow'})
    login(c,admin=False)
    assert c.get(f'/api/file/{fid}/preview-data').status_code==200
    assert c.get(f'/api/file/{fid}/permissions').json()['download']=='deny'
    for method,url,kwargs in [('get',f'/api/file/{fid}/download',{}),
                              ('head',f'/api/file/{fid}/download',{}),
                              ('post','/api/downloads/requests',{'json':{'file_id':fid,'reason':'业务'}}),
                              ('post','/api/analysis/export',{'json':{'file_id':fid}})]:
        assert getattr(c,method)(url,**kwargs).status_code==403


def test_explicit_direct_download_and_revoke_even_after_cached_preview(secure):
    c,root,add=secure;fid=add();login(c)
    set_permissions(c,root,permissions={'read':'allow','download':'direct'})
    login(c,admin=False)
    assert c.get(f'/api/file/{fid}/download').status_code==200
    assert c.get(f'/api/file/{fid}/preview-data').status_code==200
    login(c);set_permissions(c,root,permissions={'read':'deny','download':'direct'})
    login(c,admin=False)
    assert ids(c,'报表')==[]
    assert c.get(f'/api/file/{fid}/preview-data').status_code==404
    assert c.get(f'/api/file/{fid}/download').status_code==404


def test_approval_ticket_is_user_bound_and_cannot_override_download_denial(secure):
    c,root,add=secure;fid=add();login(c)
    for uid in ('4','5'):
        set_permissions(c,root,subject=uid,permissions={'read':'allow','download':'approval'})
    login(c,admin=False)
    req=c.post('/api/downloads/requests',json={'file_id':fid,'reason':'复盘'}).json()['id']
    login(c)
    assert c.post(f'/api/downloads/requests/{req}/review',json={'action':'approve'}).status_code==200
    login(c,admin=False)
    ticket=c.get('/api/downloads/requests').json()['requests'][0]['ticket']
    assert c.get(f'/api/file/{fid}/download',params={'ticket':ticket}).status_code==200
    c.post('/api/auth/login',json={'phone':'13800100005','password':'123456'})
    assert c.get(f'/api/file/{fid}/download',params={'ticket':ticket}).status_code==403
    login(c);set_permissions(c,root,permissions={'read':'allow','download':'deny'})
    login(c,admin=False)
    assert c.get(f'/api/file/{fid}/download',params={'ticket':ticket}).status_code==403


def test_denied_applicant_cannot_be_approved(secure):
    c,root,add=secure;fid=add();login(c)
    set_permissions(c,root,permissions={'read':'allow','download':'approval'})
    login(c,admin=False)
    req=c.post('/api/downloads/requests',json={'file_id':fid,'reason':'复盘'}).json()['id']
    login(c);set_permissions(c,root,permissions={'read':'allow','download':'deny'})
    assert c.post(f'/api/downloads/requests/{req}/review',json={'action':'approve'}).status_code==403


def test_department_denial_and_stricter_download_rule_take_priority(secure):
    c,root,add=secure;fid=add();login(c)
    set_permissions(c,root,permissions={'read':'allow','download':'direct','upload':'allow'})
    set_permissions(c,root,subject_type='department',subject='财务运营部 / 利润核算组',permissions={'download':'approval','upload':'deny'})
    result=c.get('/api/system/directory-grants/effective',params={'user_id':4,'path':str(root/'业务域')}).json()
    assert result['permissions']=={'read':'allow','upload':'deny','download':'approval','update':'deny'}
    assert len(result['sources'])==2
    login(c,admin=False)
    assert c.get(f'/api/file/{fid}/download').status_code==403
    assert c.post('/api/system/directory-grants/batch',json={'subject_type':'user','subject_keys':['4'],'target_paths':[str(root)],'permissions':{'read':'allow'}}).status_code==403


def test_department_admin_explicit_approval_is_not_bypassed(secure):
    c,root,add=secure;fid=add();login(c)
    set_permissions(c,root,subject='2',permissions={'read':'allow','download':'approval'})
    c.post('/api/auth/login',json={'phone':'13800100002','password':'mgr123'})
    assert c.get(f'/api/file/{fid}/download').status_code==403
    response=c.post('/api/downloads/requests',json={'file_id':fid,'reason':'复盘'}).json()
    assert response['status']=='pending'
    assert c.post(f"/api/downloads/requests/{response['id']}/review",json={'action':'approve'}).status_code==403


def test_batch_preview_conflict_and_atomic_validation(secure):
    c,root,add=secure;add();login(c)
    body={'subject_type':'user','subject_keys':['4','5'],'target_paths':[str(root/'业务域')],'permissions':{'read':'allow'}}
    preview=c.post('/api/system/directory-grants/batch',json=body).json()
    assert len(preview['changes'])==2
    assert c.get('/api/system/directory-grants').json()['grants']==[]
    set_permissions(c,root,permissions={'read':'deny'})
    result=c.post('/api/system/directory-grants/batch',json={**body,'preview':False,'revision':preview['revision']})
    assert result.status_code==409
    result=c.post('/api/system/directory-grants/batch',json={**body,'subject_keys':['4','nonexistent']})
    assert result.status_code==422
    rows=c.get('/api/system/directory-grants').json()['grants']
    assert len(rows)==1 and rows[0]['permissions']=={'read':'deny'}


def test_batch_merge_revoke_and_replace_preserve_unselected_operations(secure):
    c,root,add=secure;add();login(c)
    grant(c,root,'业务域')
    assert batch(c,root,permissions={'upload':'allow'}).status_code==200
    p=c.get('/api/system/directory-grants').json()['grants'][0]['permissions']
    assert p=={'read':'allow','download':'legacy','upload':'allow'}
    assert batch(c,root,permissions={'upload':'allow'},mode='revoke').status_code==200
    p=c.get('/api/system/directory-grants').json()['grants'][0]['permissions']
    assert p=={'read':'allow','download':'legacy'}
    assert batch(c,root,permissions={'read':'allow'},mode='replace').status_code==200
    p=c.get('/api/system/directory-grants').json()['grants'][0]['permissions']
    assert p=={'read':'allow'}
    assert batch(c,root,permissions={'read':'allow'},mode='revoke').status_code==200
    assert c.get('/api/system/directory-grants').json()['grants']==[]
    assert len(c.get('/api/system/directory-grants').json()['audit'])==5


def test_empty_folder_permissions_and_invalid_operations(secure,tmp_path):
    c,root,add=secure;(root/'空文件夹').mkdir();login(c)
    set_permissions(c,root,'空文件夹',permissions={'upload':'allow'})
    d=c.get('/api/system/directory-grants/effective',params={'user_id':4,'path':str(root/'空文件夹')}).json()
    assert d['permissions']=={'read':'deny','upload':'allow','download':'deny','update':'deny'}
    body={'subject_type':'user','subject_keys':['4'],'target_paths':[str(root)],'permissions':{'delete':'allow'}}
    assert c.post('/api/system/directory-grants/batch',json=body).status_code==422
    assert c.post('/api/system/directory-grants/batch',json={**body,'permissions':{}}).status_code==422
    assert c.post('/api/system/directory-grants/batch',json={**body,'permissions':{'read':'direct'}}).status_code==422
    outside=tmp_path/'outside';outside.mkdir();(root/'外部链接').symlink_to(outside,target_is_directory=True)
    response=c.post('/api/system/directory-grants',json={'subject_type':'user','subject_key':'4','target_path':str(root/'外部链接'),'permissions':{'upload':'allow'}})
    assert response.status_code==403


def test_local_open_route_is_retired(secure):
    c,root,add=secure;fid=add();login(c)
    assert c.post(f'/api/file/{fid}/open').status_code==404
    html=(Path(application.APP_DIR)/'static/index.html').read_text()
    assert 'openFileLocally' not in html and 'btn-open-local' not in html


def test_replaced_file_link_cannot_cross_read_scope(secure):
    c,root,add=secure
    fid=add('公开/报表.csv');add('机密/报表.csv');login(c)
    set_permissions(c,root,'公开',permissions={'read':'allow','download':'direct'})
    path=root/'公开/报表.csv';path.unlink();path.symlink_to(root/'机密/报表.csv')
    login(c,admin=False)
    assert c.get(f'/api/file/{fid}/preview-data').status_code==403
    assert c.get(f'/api/file/{fid}/download').status_code==403


def test_blank_search_keeps_acl_and_allows_filter_only_queries(secure):
    c, root, add = secure
    wanted=add('可见/报表.csv');add('机密/报表.csv')
    login(c);set_permissions(c,root,'可见',permissions={'read':'allow'})
    login(c,admin=False)
    data=c.get('/api/search',params={'q':'','ext':'csv','enable_ai':'true'}).json()
    assert data['total_candidates']==1 and data['results'][0]['id']==wanted
    assert not data['ai_used']
