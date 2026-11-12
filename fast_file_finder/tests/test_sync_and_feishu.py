from test_product_foundation import foundation, login
import app as application
import feishu_integration as feishu


def test_directory_sync_preserves_siblings_and_full_scan_timestamp(foundation):
    client,root,add=foundation
    removed=add('本部/旧.csv')
    sibling=add('外部/保留.csv')
    (root/'本部/旧.csv').unlink()
    (root/'外部/保留.csv').unlink()
    (root/'本部/新.csv').write_text('a,b')
    application._scan_worker(mode='directory',target_path=str(root/'本部'))
    with application.db_session() as conn:
        assert conn.execute('SELECT id FROM files WHERE id=?',(sibling,)).fetchone()
        assert not conn.execute('SELECT id FROM files WHERE id=?',(removed,)).fetchone()
        assert not conn.execute("SELECT value FROM app_meta WHERE key='last_scan_at'").fetchone()


def test_rebuild_repairs_search_and_rolls_back_partial_walk(foundation,monkeypatch):
    client,root,add=foundation
    fid=add()
    with application.db_session() as conn:
        conn.execute('DELETE FROM file_fts WHERE rowid=?',(fid,))
    application._scan_worker(mode='rebuild')
    with application.db_session() as conn:
        assert conn.execute('SELECT rowid FROM file_fts WHERE rowid=?',(fid,)).fetchone()
    monkeypatch.setattr(application.scan_pipeline,'walk_metadata',lambda *a,**k:iter([('directory_error','broken','offline')]))
    application._scan_worker(mode='rebuild')
    assert application._scan_state_snapshot()['phase']=='partial'
    with application.db_session() as conn:
        assert conn.execute('SELECT rowid FROM file_fts WHERE rowid=?',(fid,)).fetchone()


def test_feishu_sync_bind_login_and_state_replay(foundation,monkeypatch):
    client,root,add=foundation
    monkeypatch.setenv('FEISHU_APP_ID','synthetic')
    monkeypatch.setenv('FEISHU_APP_SECRET','fixture')
    monkeypatch.setenv('FEISHU_REDIRECT_URI','http://testserver/integrations/feishu/callback')
    def mock(method,path,**kwargs):
        if path.endswith('tenant_access_token/internal'):return {'tenant_access_token':'tenant'}
        if path.endswith('/children'):return {'data':{'items':[]}}
        if path.endswith('/find_by_department'):return {'data':{'items':[{'open_id':'ou_fixture','name':'测试成员','status':{}}]}}
        if path.endswith('/oauth/token'):return {'access_token':'member'}
        if path.endswith('/user_info'):return {'data':{'open_id':'ou_fixture'}}
        raise AssertionError(path)
    monkeypatch.setattr(feishu,'call',mock)
    login(client)
    assert client.post('/api/system/feishu/sync').json()['users']==1
    assert client.post('/api/system/feishu/bind',json={'user_id':4,'open_id':'ou_fixture'}).status_code==200
    assert client.post('/api/system/feishu/bind',json={'user_id':5,'open_id':'ou_fixture'}).status_code==409
    client.post('/api/auth/logout')
    response=client.get('/integrations/feishu/login',follow_redirects=False)
    state=client.cookies['ff_feishu_state']
    params={'state':state,'code':'synthetic'}
    assert response.status_code==307
    assert client.get('/integrations/feishu/callback',params=params,follow_redirects=False).status_code==307
    assert client.get('/api/auth/me').json()['user']['id']==4
    assert client.get('/integrations/feishu/callback',params=params).status_code==403
    assert client.post('/api/system/feishu/sync').status_code==403


def test_notice_delivery_uses_binding_and_deduplicates(foundation,monkeypatch):
    client,root,add=foundation
    fid=add()
    monkeypatch.setenv('FEISHU_NOTIFY_ENABLED','true')
    monkeypatch.setattr(feishu,'configured',lambda:True)
    monkeypatch.setattr(feishu,'tenant_token',lambda:'test')
    sent=[]
    monkeypatch.setattr(feishu,'call',lambda method,path,**kwargs:sent.append(kwargs['json']['receive_id']))
    with application.db_session() as conn:
        conn.execute("INSERT INTO feishu_members VALUES ('ou_test','测试负责人','测试部门',1)")
        conn.execute("UPDATE system_users SET feishu_id='ou_test' WHERE id=1")
        conn.execute("INSERT OR REPLACE INTO app_meta VALUES('feishu_notify_since','0')")
    login(client,admin=False)
    client.post('/api/downloads/requests',json={'file_id':fid,'reason':'测试'})
    feishu.deliver_notices(application)
    feishu.deliver_notices(application)
    assert sent==['ou_test']
