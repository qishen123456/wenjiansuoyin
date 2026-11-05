"""End-to-end access boundaries, including cached reads and mutation targets."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import directory_access
from test_product_foundation import foundation, login, rule_payload


@pytest.fixture
def secure(foundation, monkeypatch):
    monkeypatch.setattr(application, 'DIRECTORY_ACL_ENABLED', True)
    monkeypatch.setattr(application, 'PREVIEW_CACHE_DIR', foundation[1].parent / 'preview-cache')
    return foundation


def grant(client, root, path, *, subject_type='user', subject_key='4', effect='allow'):
    res = client.post('/api/system/directory-grants', json={
        'subject_type': subject_type, 'subject_key': subject_key,
        'target_path': str(root / path), 'effect': effect})
    assert res.status_code == 200, res.text
    return client.get('/api/system/directory-grants').json()['grants'][0]['id']


def ids(client, query='销售'):
    res = client.get('/api/search', params={'q':query})
    assert res.status_code == 200, res.text
    return [r['id'] for r in res.json()['results']]


def flatten(node):
    return [node] + [desc for child in node['children'] for desc in flatten(child)]


def test_acl_forces_login_even_when_development_auth_is_disabled(secure):
    c, root, add = secure
    fid = add()
    for endpoint in ['/api/catalog/tree', '/api/search?q=报表', f'/api/file/{fid}/preview-source/a.csv?key={application.PREVIEW_SOURCE_KEY}', '/api/index/status']:
        assert c.get(endpoint).status_code == 401
    assert c.get('/api/health').json()['indexed_files'] == 0
    login(c, admin=False)
    assert c.get('/api/catalog/tree').json()['tree']['file_count'] == 0
    assert c.get(f'/api/file/{fid}/preview').status_code == 404
    assert ids(c, '报表') == []


def test_department_inheritance_denials_and_prefix_boundaries(secure):
    c, root, add = secure
    ok = add('财务/报表/销售.csv')
    hidden = add('财务/机密/销售.csv')
    sibling = add('财务部/销售.csv')
    login(c)
    grant(c, root, '财务', subject_type='department', subject_key='财务运营部 / 利润核算组')
    grant(c, root, '财务/机密', effect='deny')
    login(c, admin=False)
    assert ids(c) == [ok]
    tree = c.get('/api/catalog/tree').json()['tree']
    assert tree['file_count'] == 1
    assert all('机密' not in n['path'] and '财务部' not in n['path'] for n in flatten(tree))
    for fid in [hidden, sibling]:
        for suffix in ['preview', 'download', 'preview-source/a.csv']:
            assert c.get(f'/api/file/{fid}/{suffix}').status_code == 404
        assert c.get(f'/api/analysis/file/{fid}').status_code == 404
        assert c.post('/api/analysis/saved', json={'file_id':fid,'name':'越权','config':{}}).status_code == 404
    assert c.get('/api/index/status').json()['indexed_files'] == 1
    assert c.get('/api/index/status').json()['scan_history'] == []


def test_deep_grant_only_shows_required_ancestors_and_revocation_invalidates_tree(secure):
    c, root, add = secure
    fid = add('公司/财务/利润/销售.csv')
    add('公司/财务/预算/销售.csv')
    login(c)
    assert c.get('/api/catalog/tree').json()['tree']['file_count'] == 2
    gid = grant(c, root, '公司/财务/利润')
    login(c, admin=False)
    tree = c.get('/api/catalog/tree').json()['tree']
    assert tree['file_count'] == 1
    assert {n['name'] for n in flatten(tree)} >= {'公司','财务','利润'}
    assert not any(n['name'] == '预算' for n in flatten(tree))
    assert c.get(f'/api/file/{fid}/preview-data').status_code == 200
    assert c.get(f'/api/file/{fid}/preview-source/a.csv').status_code == 403
    login(c)
    assert c.delete(f'/api/system/directory-grants/{gid}').status_code == 200
    login(c, admin=False)
    assert c.get('/api/catalog/tree').json()['tree']['file_count'] == 0
    assert c.get(f'/api/file/{fid}/preview-source/a.csv').status_code == 404


def test_literal_wildcards_and_unauthorized_results_do_not_exhaust_limit(secure):
    c, root, add = secure
    for i in range(15):
        add(f'隔离/销售{i}.csv')
    wanted = add('100%_完成/销售.csv')
    add('100甲完成/销售.csv')
    login(c)
    grant(c, root, '100%_完成')
    login(c, admin=False)
    with application.db_session() as conn:
        user = application._get_session_user(type('Req', (), {'cookies':c.cookies})())
        policy = directory_access.load_policy(conn, user, application.root_scope_key(), str(root))
    token = directory_access.current.set(policy)
    try:
        with application.db_session() as conn:
            results = application.candidate_search(conn, '销售', limit=1)
            assert [r['id'] for r in results] == [wanted]
    finally:
        directory_access.current.reset(token)
    assert ids(c) == [wanted]


def test_ai_only_sees_authorized_candidates_and_directory_context(secure, monkeypatch):
    c, root, add = secure
    wanted = add('可见/销售.csv')
    add('隐秘客户/销售.csv')
    login(c)
    grant(c, root, '可见')
    login(c, admin=False)
    messages = []
    def llm(prompt, **kwargs):
        messages.extend(prompt)
        return '可以尝试“销售”', 'test-model'
    monkeypatch.setattr(application, 'call_llm', llm)
    answer = c.post('/api/ai/chat', json={'message':'销售'}).json()
    assert [r['id'] for r in answer['matched_files']] == [wanted]
    c.post('/api/ai/chat', json={'message':'没有命中的主题'})
    assert '隐秘客户' not in str(messages)
    assert '可见' in str(messages)


def test_department_admin_cannot_escalate_or_manage_accounts(secure):
    c, root, add = secure
    c.post('/api/auth/login', json={'phone':'13800100002','password':'mgr123'})
    for endpoint, method, body in [
        ('/api/system/permissions','get',None),
        ('/api/system/permissions/users/1/password','put',{'password':'stolen'}),
        ('/api/system/permissions/users/2/role','put',{'role':'super_admin'}),
        ('/api/system/directory-grants','get',None),
        ('/api/index/start','post',{}),
        ('/api/governance/summary','get',None),
    ]:
        res = getattr(c, method)(endpoint, **({'json':body} if body is not None else {}))
        assert res.status_code == 403, (endpoint, res.text)


def test_archive_saved_and_approvals_do_not_leak_and_mutations_check_scope(secure):
    c, root, add = secure
    fid = add('机密/销售.csv', age=86400*20)
    login(c)
    rule = c.post('/api/archive/rules',json=rule_payload(root,'机密')).json()['id']
    task = c.get('/api/archive/tasks').json()['items'][0]['id']
    saved = c.post('/api/analysis/saved',json={'file_id':fid,'name':'秘密方案','config':{}}).json()['id']
    request = c.post('/api/downloads/requests',json={'file_id':fid,'reason':'测试'}).json()['id']
    c.post('/api/auth/login',json={'phone':'13800100002','password':'mgr123'})
    assert c.get('/api/archive/rules').json()['rules'] == []
    assert c.get('/api/archive/tasks').json()['items'] == []
    assert c.get('/api/analysis/saved').json()['results'] == []
    assert c.get('/api/downloads/requests?scope=all').json()['requests'] == []
    assert c.delete(f'/api/analysis/saved/{saved}').status_code == 404
    assert c.delete(f'/api/archive/rules/{rule}').status_code == 404
    assert c.put(f'/api/archive/rules/{rule}/toggle').status_code == 404
    assert c.put(f'/api/archive/rules/{rule}',json=rule_payload(root,'机密')).status_code == 404
    assert c.post(f'/api/archive/tasks/{task}/submit',json={'note':'x','proof_image':'x'}).status_code == 404
    assert c.post(f'/api/downloads/requests/{request}/review',json={'action':'approve'}).status_code == 404


def test_denied_child_blocks_directory_wide_rule_management(secure):
    c, root, add = secure
    add('财务/机密/销售.csv')
    add('财务/公开/销售.csv')
    login(c)
    grant(c,root,'财务',subject_key='2')
    grant(c,root,'财务/机密',subject_key='2',effect='deny')
    c.post('/api/auth/login',json={'phone':'13800100002','password':'mgr123'})
    assert c.post('/api/archive/rules',json=rule_payload(root,'财务')).status_code == 403
    assert c.post('/api/archive/rules',json=rule_payload(root,'财务/公开')).status_code == 200


def test_authorized_mutations_and_grant_audit_remain_functional(secure):
    c, root, add = secure
    fid = add('公开/销售.csv',age=86400*20)
    login(c)
    gid = grant(c,root,'公开',subject_key='2')
    c.post('/api/auth/login',json={'phone':'13800100002','password':'mgr123'})
    rule = c.post('/api/archive/rules',json=rule_payload(root,'公开')).json()['id']
    assert c.put(f'/api/archive/rules/{rule}/toggle').status_code == 200
    saved = c.post('/api/analysis/saved',json={'file_id':fid,'name':'销售方案','config':{}}).json()['id']
    assert c.delete(f'/api/analysis/saved/{saved}').status_code == 200
    assert c.delete(f'/api/archive/rules/{rule}').status_code == 200
    assert c.get('/api/archive/tasks').json()['items'] == []
    login(c)
    assert c.delete(f'/api/system/directory-grants/{gid}').status_code == 200
    audit = c.get('/api/system/directory-grants').json()['audit']
    assert [row['action'] for row in audit[:2]] == ['delete','save']


def test_directory_predicate_handles_quotes_wildcards_and_case_exactly():
    import sqlite3
    with sqlite3.connect(':memory:') as conn:
        target = "/share/O'Reilly%_"
        clause = directory_access.directory_sql(target)
        for path, expected in [(target,1),(target+'/nested',1),('/share/OxReillyAB',0),(target+'2',0),(target.lower(),0)]:
            assert conn.execute('SELECT '+clause+' FROM (SELECT ? AS parent)',(path,)).fetchone()[0] == expected
        assert conn.execute('SELECT '+directory_access.directory_sql('/')+' FROM (SELECT ? AS parent)',('/',)).fetchone()[0] == 1


def test_preview_cache_separates_sources_and_subsecond_versions(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(application,'PREVIEW_CACHE_DIR',tmp_path/'cache')
    paths = [tmp_path/'one.csv',tmp_path/'two.csv']
    paths[0].write_bytes(b'a,b\n1,2\n')
    paths[1].write_bytes(b'a,b\n3,4\n')
    stamp = 1_700_000_000_100_000_000
    for path in paths:
        os.utime(path,ns=(stamp,stamp))
    first = application._preview_cache_get(1,paths[0])
    second = application._preview_cache_get(1,paths[1])
    assert first != second and first.read_bytes() != second.read_bytes()
    paths[0].write_bytes(b'a,b\n5,6\n')
    os.utime(paths[0],ns=(stamp+100_000_000,stamp+100_000_000))
    changed = application._preview_cache_get(1,paths[0])
    assert changed != first and changed.read_bytes() == b'a,b\n5,6\n'


def test_ai_latest_date_search_keeps_directory_permissions(secure, monkeypatch):
    c, root, add = secure
    wanted = add('可见/京东订单.csv')
    add('隐秘客户/京东订单.csv')
    login(c)
    grant(c, root, '可见')
    login(c, admin=False)
    monkeypatch.setattr(application, 'is_ai_configured', lambda: False)
    answer = c.post('/api/ai/chat', json={'message':'找下今天京东最新的文件'}).json()
    assert [r['id'] for r in answer['matched_files']] == [wanted]
    assert answer['retrieval']['total'] == 1
    assert '隐秘客户' not in str(answer)
