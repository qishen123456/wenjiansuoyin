import app as application
from test_product_foundation import foundation


def test_combined_facets_filter_before_pagination_and_apply_to_fallback(foundation):
    client,root,add=foundation
    add('其他/销售.csv',content='')
    wanted=add('业务%_域/销售.csv',content='月份,金额\n11,100\n')
    add('业务%_域/销售空.csv',content='')
    result=client.get('/api/search',params={'q':'销售','directory':str(root/'业务%_域'),'content_state':'nonempty','min_size':1,'max_size':100,'page_size':1}).json()
    assert result['total_candidates']==1 and result['results'][0]['id']==wanted
    empty=client.get('/api/search',params={'q':'销售','directory':str(root/'业务%_域'),'content_state':'empty'}).json()
    assert empty['total_candidates']==1 and empty['results'][0]['size']==0
    assert client.get('/api/search',params={'q':'销售','min_size':100,'max_size':1}).status_code==422
    assert client.get('/api/search',params={'q':'销售','directory':str(root.parent)}).status_code==403


def test_candidate_limit_and_ai_cannot_escape_active_facets(foundation,monkeypatch):
    client,root,add=foundation
    for index in range(5):add(f'其他/销售{index}.csv',content='')
    wanted=add('目标/销售.csv')
    with application.db_session() as conn:
        result=application.candidate_search(conn,'销售',limit=1,directory=str(root/'目标'),content_state='nonempty')
    assert [item['id'] for item in result]==[wanted]
    monkeypatch.setattr(application,'is_ai_configured',lambda:True)
    def unexpected(*args,**kwargs):raise AssertionError('AI must not suggest files outside filters')
    monkeypatch.setattr(application,'ai_guess_and_supplement',unexpected)
    result=client.get('/api/search',params={'q':'不存在','enable_ai':'true','content_state':'empty'}).json()
    assert result['results']==[] and not result['ai_used']
