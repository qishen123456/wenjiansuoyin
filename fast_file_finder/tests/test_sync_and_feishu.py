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
        if path.endswith('/find_by_department'):return {'data':{'items':[{'open_id':'ou_fixture','name':'测试成员','mobile':'+86 13800100004','status':{}}]}}
        if path.endswith('/oauth/token'):return {'access_token':'member'}
        if path.endswith('/user_info'):return {'data':{'open_id':'ou_fixture'}}
        raise AssertionError(path)
    monkeypatch.setattr(feishu,'call',mock)
    login(client)
    assert client.post('/api/system/feishu/sync').json()['users']==1
    assert client.post('/api/system/feishu/bind',json={'user_id':4,'open_id':'ou_fixture'}).status_code==200
    assert client.post('/api/system/feishu/bind',json={'user_id':5,'open_id':'ou_fixture'}).status_code==409
    client.post('/api/auth/logout')
    day = '2026-11-23'
    page = client.get('/?tab=updates&date=' + day).text
    assert 'href="/integrations/feishu/login?report_date=' + day + '"' in page
    response=client.get('/integrations/feishu/login',params={'report_date':day},follow_redirects=False)
    state=client.cookies['ff_feishu_state']
    params={'state':state,'code':'synthetic'}
    assert response.status_code==307
    callback = client.get('/integrations/feishu/callback',params=params,follow_redirects=False)
    assert callback.status_code == 307 and callback.headers['location'] == '/?tab=updates&date=' + day
    assert client.get('/api/auth/me').json()['user']['id']==4
    assert client.get('/integrations/feishu/callback',params=params).status_code==403
    assert client.post('/api/system/feishu/sync').status_code==403

    client.post('/api/auth/logout')
    csv_path = '/reports/daily/abnormal.csv?date=' + day
    landing = client.get(csv_path, follow_redirects=False)
    page = client.get(landing.headers['location']).text
    assert f'href="/integrations/feishu/login?report_date={day}&amp;report_view=exceptions"' in page
    login_redirect = client.get('/integrations/feishu/login', params={
        'report_date': day, 'report_view': 'exceptions',
    }, follow_redirects=False)
    assert login_redirect.status_code == 307
    callback = client.get('/integrations/feishu/callback', params={
        'state': client.cookies['ff_feishu_state'], 'code': 'synthetic',
    }, follow_redirects=False)
    assert callback.status_code == 307 and callback.headers['location'] == csv_path
    assert client.get(csv_path).headers['content-disposition'].startswith('attachment;')


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
        conn.execute("INSERT INTO feishu_member_profiles VALUES ('ou_test','0001','','',1,'matched')")
        conn.execute("UPDATE system_users SET feishu_id='ou_test' WHERE id=1")
        conn.execute("INSERT OR REPLACE INTO app_meta VALUES('feishu_notify_since','0')")
    login(client,admin=False)
    client.post('/api/downloads/requests',json={'file_id':fid,'reason':'测试'})
    feishu.deliver_notices(application)
    feishu.deliver_notices(application)
    assert sent==['ou_test']


def test_sync_uses_explicit_contacts_when_root_not_authorized(foundation, monkeypatch):
    import httpx

    client, _, _ = foundation
    monkeypatch.setattr(feishu, 'tenant_token', lambda: 'test')
    calls = []

    def request(method, url, **kwargs):
        path = url.split('/open-apis', 1)[1]
        calls.append((path, kwargs['params']))
        if path == '/contact/v3/departments/0/children':
            return httpx.Response(403, json={'code': 40004})
        if path == '/contact/v3/scopes':
            page = kwargs['params'].get('page_token')
            data = {'user_ids': ['ou_direct'], 'has_more': True, 'page_token': 'second'} if not page else {
                'department_ids': ['od_pilot'], 'has_more': False}
        elif path == '/contact/v3/departments/od_pilot':
            data = {'department': {'name': '试点部门'}}
        elif path.endswith('/children'):
            data = {'items': []}
        elif path.endswith('/find_by_department'):
            data = {'items': [{'open_id': 'ou_pilot', 'name': '部门成员'}]}
        elif path == '/contact/v3/users/ou_direct':
            data = {'user': {'open_id': 'ou_direct'}}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={'code': 0, 'data': data})

    monkeypatch.setattr(feishu.httpx, 'request', request)
    login(client)
    assert client.post('/api/system/feishu/sync').json() == {'users': 2, 'departments': 1, 'matched': 0}
    entries = {member['open_id']: member for member in client.get('/api/system/feishu').json()['members']}
    assert entries['ou_direct']['name'] == '飞书成员（direct）'
    assert entries['ou_pilot']['department'] == '试点部门'
    assert all(params.get('department_id') != '0' for path, params in calls if path.endswith('find_by_department'))


def test_personnel_directory_prefers_real_contacts_and_keeps_platform_accounts_separate(foundation, monkeypatch):
    client, _, _ = foundation
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_fixture')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'test-secret')
    monkeypatch.delenv('FEISHU_REDIRECT_URI', raising=False)

    def request(method, path, **kwargs):
        if path.endswith('tenant_access_token/internal'):
            return {'tenant_access_token': 'test'}
        if path.endswith('/0/children'):
            raise feishu.FeishuAPIError(40004)
        if path.endswith('/scopes'):
            return {'data': {'user_ids': ['ou_real']}}
        if path.endswith('/users/ou_real'):
            return {'data': {'user': {
                'open_id': 'ou_real', 'name': '飞书真实成员',
                'mobile': '13912345678', 'department_ids': ['0'],
            }}}
        raise AssertionError(path)

    monkeypatch.setattr(feishu, 'call', request)
    login(client)
    response = client.get('/api/system/permissions').json()
    assert response['directory']['source'] == 'feishu'
    assert response['directory']['members'] == []
    assert response['feishu_config']['configured'] is False
    assert client.post('/api/system/feishu/sync').json()['users'] == 1
    response = client.get('/api/system/permissions').json()
    assert response['directory']['coverage'] == 'specified'
    person, = response['directory']['members']
    assert person['name'] == '飞书真实成员'
    assert person['department'] == '根组织' and not person['job_title']
    assert person['match_status'] == 'no_account' and person['phone_tail'] == '5678'
    assert response['directory']['summary'] == {'total':1, 'active':1, 'matched':0, 'departments':0}
    assert len(response['users']) == 5
    assert all(account['name'] != person['name'] for account in response['users'])

    monkeypatch.delenv('FEISHU_APP_ID')
    response = client.get('/api/system/permissions').json()
    assert response['directory']['source'] == 'local' and response['directory']['members'] == []
    assert len(response['users']) == 5


def test_login_page_renders_feishu_option_when_configured(foundation, monkeypatch):
    client, _, _ = foundation
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_fixture')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'synthetic-secret')
    monkeypatch.delenv('FEISHU_REDIRECT_URI', raising=False)
    assert 'data-feishu-login hidden' in client.get('/').text

    monkeypatch.setenv('FEISHU_REDIRECT_URI', 'http://testserver/integrations/feishu/callback')
    for path in ('/', '/index.html'):
        page = client.get(path).text
        assert 'data-feishu-login>' in page
        assert 'href="/integrations/feishu/login"' in page
        assert '<span>使用飞书登录</span>' in page
        assert 'type="submit">账号密码登录</button>' in page
