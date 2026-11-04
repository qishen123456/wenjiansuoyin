from test_product_foundation import foundation
import app as application


def test_created_and_modified_sorts_are_independent_before_paging(foundation):
    client,root,add=foundation
    a=add('业务域/销售甲.csv')
    b=add('业务域/销售乙.csv')
    with application.db_session() as conn:
        conn.execute('UPDATE files SET ctime=?,mtime=? WHERE id=?',(1700000000,1800000000,a))
        conn.execute('UPDATE files SET ctime=?,mtime=? WHERE id=?',(1800000000,1700000000,b))
    for sort,order,wanted in [('ctime','desc',b),('ctime','asc',a),('mtime','desc',a),('mtime','asc',b)]:
        response=client.get('/api/catalog/files',params={'sort_by':sort,'sort_order':order,'page_size':1})
        assert response.status_code==200
        row=response.json()['items'][0]
        assert row['id']==wanted and row['ctime']!=row['mtime']
        assert len(row['mtime_display'])==19 and len(row['ctime_display'])==19
        assert '今天' not in row['time_display']
    for sort,wanted in [('created_desc',b),('created_asc',a),('time_desc',a),('time_asc',b)]:
        data=client.get('/api/search',params={'q':'销售','sort':sort,'page_size':1}).json()
        assert data['results'][0]['id']==wanted
        assert data['results'][0]['ctime']
