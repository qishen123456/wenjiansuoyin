from test_product_foundation import foundation, login
import app
import feishu_integration as feishu


def setup_feishu(monkeypatch, people):
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_fixture')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'synthetic-secret')
    monkeypatch.setenv('FEISHU_REDIRECT_URI', 'http://testserver/integrations/feishu/callback')
    monkeypatch.setattr(app, 'DIRECTORY_ACL_ENABLED', True)

    def request(method, path, **kwargs):
        if path.endswith('tenant_access_token/internal'):
            return {'tenant_access_token': 'test'}
        if path.endswith('/0/children'):
            return {'data': {'items': [{'open_department_id': 'od_finance', 'name': '飞书财务部'}]}}
        if path.endswith('/od_finance/children'):
            return {'data': {'items': [{'open_department_id': 'od_ledger', 'name': '利润组'}]}}
        if path.endswith('/children'):
            return {'data': {'items': []}}
        if path.endswith('/find_by_department'):
            dept = kwargs['params']['department_id']
            return {'data': {'items': [user for user in people if dept in user.get('department_ids', [])]}}
        if path.endswith('/oauth/token'):
            return {'access_token': kwargs['json']['code']}
        if path.endswith('/user_info'):
            return {'data': {'open_id': kwargs['token']}}
        raise AssertionError(path)

    monkeypatch.setattr(feishu, 'call', request)


def oauth(client, open_id):
    response = client.get('/integrations/feishu/login', follow_redirects=False)
    assert response.status_code == 307
    return client.get('/integrations/feishu/callback', params={
        'state': client.cookies['ff_feishu_state'], 'code': open_id,
    }, follow_redirects=False)


def add_grant(client, root, subject_type, key, folder, **permissions):
    response = client.post('/api/system/directory-grants', json={
        'subject_type': subject_type, 'subject_key': key, 'target_path': str(root / folder),
        'permissions': permissions,
    })
    assert response.status_code == 200, response.text


def test_phone_matching_drives_department_and_job_access_then_revokes_on_change(foundation, monkeypatch):
    client, root, add = foundation
    common = add('部门共享/经营报表.csv')
    job_only = add('岗位报表/岗位报表.csv')
    private = add('部门共享/受限/受限报表.csv')
    person = {'open_id': 'ou_member', 'name': '真实员工', 'mobile': '+86 (138) 0010-0004',
              'department_ids': ['od_ledger'], 'job_title': '成本会计'}
    setup_feishu(monkeypatch, [person])

    login(client)
    assert client.post('/api/system/feishu/sync').json()['matched'] == 1
    member = client.get('/api/system/feishu').json()['members'][0]
    assert member['local_user_id'] == member['bound_user_id'] == 4
    assert member['phone_tail'] == '0004' and 'mobile' not in member and 'phone' not in member
    assert member['department'] == '飞书财务部 / 利润组'
    assert member['job_title'] == '成本会计'

    job_subjects = client.get('/api/system/directory-grants').json()['feishu_subjects']['feishu_job']
    assert any(item['key'] == 'od_finance|title:成本会计' for item in job_subjects)
    add_grant(client, root, 'feishu_department', 'od_finance', '部门共享', read='allow', download='approval')
    add_grant(client, root, 'feishu_job', 'od_finance|title:成本会计', '岗位报表',
              read='allow', download='approval', upload='allow', update='deny')
    add_grant(client, root, 'user', '4', '部门共享/受限', read='deny')
    result = client.get('/api/system/directory-grants/effective', params={'user_id':4,'path':str(root/'岗位报表')}).json()
    assert result['permissions'] == {'read':'allow','download':'approval','upload':'allow','update':'deny'}
    assert result['sources'][0]['subject_type'] == 'feishu_job'

    assert oauth(client, 'ou_member').status_code == 307
    identity = client.get('/api/auth/me').json()['user']
    assert identity['role'] == 'user'
    assert identity['name'] == '真实员工' and identity['identity_source'] == 'feishu'
    assert identity['department'] == '飞书财务部 / 利润组' and identity['job_title'] == '成本会计'
    request = client.post('/api/downloads/requests', json={'file_id':common,'reason':'联调'})
    assert request.status_code == 200
    with app.db_session() as conn:
        log = conn.execute("SELECT user_name, department FROM download_requests WHERE file_id=?", (common,)).fetchone()
        assert tuple(log) == ('真实员工', '飞书财务部 / 利润组')
    assert client.get(f'/api/file/{common}/preview-data').status_code == 200
    assert client.get(f'/api/file/{job_only}/preview-data').status_code == 200
    assert client.get(f'/api/file/{private}/preview-data').status_code == 404
    session = client.cookies['ff_session']

    person['job_title'] = '出纳'
    login(client)
    client.post('/api/system/feishu/sync').raise_for_status()
    client.cookies.set('ff_session', session)
    assert client.get(f'/api/file/{common}/preview-data').status_code == 200
    assert client.get(f'/api/file/{job_only}/preview-data').status_code == 404

    person['department_ids'] = ['0']
    client.cookies.clear()
    login(client)
    assert client.post('/api/system/feishu/sync').json()['matched'] == 1
    client.cookies.clear()
    client.cookies.set('ff_session', session)
    assert client.get(f'/api/file/{common}/preview-data').status_code == 404

    person['mobile'] = '13912345678'
    client.cookies.clear()
    login(client)
    assert client.post('/api/system/feishu/sync').json()['matched'] == 0
    assert client.get('/api/system/feishu').json()['members'][0]['bound_user_id'] is None
    client.cookies.set('ff_session', session)
    assert client.get(f'/api/file/{common}/preview-data').status_code == 401


def test_conflicts_and_privileged_accounts_require_a_verified_administrator_confirmation(foundation, monkeypatch):
    client, _, _ = foundation
    people = [
        {'open_id':'ou_admin', 'mobile':'13800100001', 'department_ids':['od_ledger']},
        {'open_id':'ou_one', 'mobile':'13800100004', 'department_ids':['od_ledger']},
        {'open_id':'ou_two', 'mobile':'+8613800100004', 'department_ids':['od_ledger']},
        {'open_id':'ou_hidden', 'department_ids':['od_ledger']},
    ]
    setup_feishu(monkeypatch, people)
    login(client)
    assert client.post('/api/system/feishu/sync').json()['matched'] == 0
    match_states = {m['open_id']:m['match_status'] for m in client.get('/api/system/feishu').json()['members']}
    assert match_states == {'ou_admin':'review','ou_one':'conflict','ou_two':'conflict','ou_hidden':'no_phone'}
    assert oauth(client, 'ou_admin').status_code == 403
    assert client.post('/api/system/feishu/bind', json={'user_id':4,'open_id':'ou_one'}).status_code == 422
    assert client.post('/api/system/feishu/bind', json={'user_id':5,'open_id':'ou_admin'}).status_code == 422
    assert client.post('/api/system/feishu/bind', json={'user_id':1,'open_id':'ou_admin'}).status_code == 200
    assert oauth(client, 'ou_admin').status_code == 307
    assert client.get('/api/auth/me').json()['user']['role'] == 'super_admin'

    assert client.post('/api/system/feishu/bind', json={'user_id':1,'open_id':''}).status_code == 200
    login(client)
    assert client.post('/api/system/feishu/sync').json()['matched'] == 0
    assert oauth(client, 'ou_admin').status_code == 403


def test_profile_edit_invalidates_matching_and_validates_normalized_numbers(foundation, monkeypatch):
    client, _, _ = foundation
    person = {'open_id':'ou_member','mobile':'13800100004','department_ids':['od_ledger']}
    setup_feishu(monkeypatch, [person])
    login(client)
    assert client.post('/api/system/feishu/sync').json()['matched'] == 1
    info = {'name':'实际成员','phone':'+86 13800100005','department':'自定义财务组','job_title':'业务分析'}
    endpoint = '/api/system/permissions/users/4/profile'
    assert client.put(endpoint, json=info).status_code == 400
    assert client.put(endpoint, json=dict(info, phone='13812345678')).status_code == 200
    assert oauth(client, 'ou_member').status_code == 403
    info['phone'] = '13800100004'
    assert client.put(endpoint, json=info).status_code == 200
    assert client.post('/api/system/feishu/sync').json()['matched'] == 1
    assert oauth(client, 'ou_member').status_code == 307
    user = client.get('/api/auth/me').json()['user']
    assert user['department'] == '飞书财务部 / 利润组' and user['job_title'] == ''
    with app.db_session() as conn:
        profile = conn.execute('SELECT department,job_title FROM system_users WHERE id=4').fetchone()
        assert tuple(profile) == ('自定义财务组', '业务分析')


def test_demo_administrator_identity_is_displayed_using_its_feishu_member(foundation, monkeypatch):
    client, _, _ = foundation
    person = {'open_id':'ou_admin_demo','name':'飞书真实姓名','mobile':'13912345678','department_ids':['0']}
    setup_feishu(monkeypatch, [person])
    monkeypatch.setattr(app, 'DEMO_MODE', True)
    monkeypatch.setattr(app, '_ensure_demo_data', lambda: None)
    with app.db_session() as conn:
        conn.execute("UPDATE system_users SET feishu_id='ou_admin_demo' WHERE id=1")
    login(client)
    assert client.post('/api/system/feishu/sync').json()['matched'] == 0
    assert oauth(client, 'ou_admin_demo').status_code == 307
    user = client.get('/api/auth/me').json()['user']
    assert user['name'] == '飞书真实姓名' and user['department'] == '根组织'
    assert user['identity_source'] == 'feishu' and user['role'] == 'super_admin'

    accounts = client.get('/api/system/permissions').json()['users']
    administrator = next(account for account in accounts if account['id'] == user['id'])
    assert administrator['display_name'] == user['name'] and administrator['identity_source'] == 'feishu'

    monkeypatch.setattr(app, 'DEMO_MODE', False)
    assert login(client)['identity_source'] == 'platform'
    assert oauth(client, 'ou_admin_demo').status_code == 403
