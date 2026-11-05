import os
from pathlib import Path
import pytest
from test_product_foundation import foundation
import app as application
import scan_pipeline


def test_dynamic_scan_balances_nested_directories_and_skips_unchanged_writes(foundation,monkeypatch):
    client,root,add=foundation
    monkeypatch.setenv('SCAN_QUEUE_SIZE','16');monkeypatch.setenv('SCAN_WORKERS','4')
    for i in range(35):add(f'大目录/层{i%4}/销售{i}.csv')
    removed=add('小目录/旧表.csv');(root/'小目录/旧表.csv').unlink()
    with application.db_session() as conn:
        stamps={r['id']:r['last_seen_scan'] for r in conn.execute('SELECT id,last_seen_scan FROM files')}
    application._scan_worker()
    state=application._scan_state_snapshot()
    assert state['phase']=='complete' and state['seen']==35 and state['unchanged']==35 and state['deleted']==1
    with application.db_session() as conn:
        rows=conn.execute('SELECT id,last_seen_scan FROM files').fetchall()
    assert len(rows)==35 and all(r['last_seen_scan']==stamps[r['id']] for r in rows)
    add('新增/销售.csv');(root/'大目录/层0/销售0.csv').write_text('日期,金额\n今天,999\n')
    application._scan_worker()
    assert application._scan_state_snapshot()['seen']==36


def test_result_queue_can_be_closed_early_without_deadlock(tmp_path):
    for i in range(60):(tmp_path/f'{i}.csv').touch()
    iterator=scan_pipeline.walk_metadata(tmp_path,workers=4,capacity=1)
    next(iterator);iterator.close()


def test_preview_paging_limits_and_cache_invalidation(foundation,monkeypatch):
    client,root,add=foundation
    fid=add('业务域/报表.csv',content='日期,金额\n'+''.join(f'{i},{i*3}\n' for i in range(250)))
    response=client.get(f'/api/file/{fid}/preview-data',params={'page_size':100})
    assert response.status_code==200,response.text
    data=response.json()
    assert len(data['rows'])==100 and data['row_count']==251 and data['has_more']
    assert response.headers['cache-control']=='no-store'
    second=client.get(f'/api/file/{fid}/preview-data',params={'page':2,'page_size':100}).json()
    assert second['start_row']==101 and second['rows'][0]==['99','297']
    path=root/'业务域/报表.csv';path.write_text('新字段,金额\n最新,999\n')
    changed=client.get(f'/api/file/{fid}/preview-data',params={'page_size':100}).json()
    assert changed['rows'][0]==['新字段','金额'] and changed['row_count']==2
    assert client.get(f'/api/file/{fid}/preview-data?page=101').status_code==422
    assert client.get(f'/api/file/{fid}/preview-data?page_size=201').status_code==422
    monkeypatch.setenv('MAX_PREVIEW_FILE_MB','0')
    assert client.get(f'/api/file/{fid}/preview-data').status_code==413


def test_preview_truncates_rows_columns_cells_and_controls_concurrency(foundation):
    client,root,add=foundation
    fid=add(content=('字段,'*101)+'末尾\n'+(('x'*2200)+',值\n')*10002)
    d=client.get(f'/api/file/{fid}/preview-data?page_size=2').json()
    assert d['truncated'] and d['row_count']==10000 and d['column_count']==100
    assert len(d['rows'][0])==100 and len(d['rows'][1][0])==2000
    assert application._limited_preview_gate.acquire(blocking=False)
    try:assert client.get(f'/api/file/{fid}/preview-data?page=2').status_code==429
    finally:application._limited_preview_gate.release()


def test_xlsx_preview_lists_sheets_and_does_not_execute_formulas(foundation,monkeypatch):
    import openpyxl
    client,root,add=foundation
    monkeypatch.setattr(application,'ALLOWED_EXTENSIONS',{'.csv','.xlsx'})
    path=root/'业务域/工作簿.xlsx';path.parent.mkdir(exist_ok=True)
    wb=openpyxl.Workbook();ws=wb.active;ws.title='销售';ws.append(['日期','金额']);ws.append(['11月',100]);ws.append(['公式','=1+1']);wb.create_sheet('库存').append(['商品','数量']);wb.save(path)
    with application.db_session() as conn:
        application.update_index_file(conn,path,path.stat())
        fid=conn.execute('SELECT id FROM files WHERE path=?',(str(path),)).fetchone()[0]
    d=client.get(f'/api/file/{fid}/preview-data').json()
    assert d['sheets']==['销售','库存'] and d['rows'][2][1]==''
    assert client.get(f'/api/file/{fid}/preview-data?sheet=库存').json()['rows'][0]==['商品','数量']
    assert client.get(f'/api/file/{fid}/preview-data?sheet=未知').status_code==422


def test_retired_preview_assets_and_raw_route_are_removed(foundation):
    client,root,add=foundation
    fid=add()
    assert client.get(f'/api/file/{fid}/preview-source/report.csv').status_code==404
    for name in ['preview-worker.js','xlsx.full.min.js','index.html.bak_v1','index.html.bak_ui_redesign','preview.html.bak_ui_redesign','guide.html.bak_ui_redesign','bi_workbench.html.bak_ui_redesign']:
        assert client.get('/static/'+name).status_code==404
    assert client.get('/workbench').status_code==200
    assert client.get(f'/api/file/{fid}/preview-data').status_code==200
