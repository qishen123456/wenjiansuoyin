import json
import time
from test_product_foundation import foundation,login
import app as application
import demo_approvals


def test_demo_workflows_have_varied_consistent_states_and_never_grant_access(foundation):
    client,root,add=foundation
    file_id=add('业务域/经营销售库存.csv',age=86400*20)
    now=time.time()
    with application.db_session() as conn:
        assert demo_approvals.seed(conn,application.root_scope_key(),now)==14
        assert demo_approvals.seed(conn,application.root_scope_key(),now)==0
        rows=conn.execute('SELECT * FROM download_requests').fetchall()
    assert len(rows)==14
    assert {r['status'] for r in rows}=={'pending','approved','rejected'}
    assert any(r['status']=='approved' and r['downloaded_at'] for r in rows)
    assert any(r['status']=='approved' and r['expires_at']<now and not r['downloaded_at'] for r in rows)
    assert any(r['status']=='approved' and r['expires_at']>now and not r['downloaded_at'] for r in rows)
    for row in rows:
        events=json.loads(row['workflow_events'])
        assert [e['at'] for e in events]==sorted(e['at'] for e in events)
        assert all(e['at']<=now for e in events)
        assert row['ticket']==''
        if row['downloaded_at']:assert row['reviewed_at']<row['downloaded_at']<row['expires_at']
    login(client)
    data=client.get('/api/downloads/requests?scope=all').json()['requests']
    assert all(r['is_demo'] for r in data)
    pending=next(r for r in data if r['status']=='pending')
    assert client.post(f"/api/downloads/requests/{pending['id']}/review",json={'action':'approve'}).status_code==200
    with application.db_session() as conn:
        reviewed=conn.execute('SELECT * FROM download_requests WHERE id=?',(pending['id'],)).fetchone()
        assert reviewed['ticket']=='' and reviewed['status']=='approved'
    assert not application._consume_download_ticket(file_id,'')
    # A demo approval is never reused as actual member authorization.
    login(client,admin=False)
    result=client.post('/api/downloads/requests',json={'file_id':file_id,'reason':'核对月度经营分析'}).json()
    assert result['status']=='pending' and result['id'] not in [r['id'] for r in data]
