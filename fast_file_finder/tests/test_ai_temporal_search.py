from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
from test_product_foundation import foundation
import app as application


def test_today_latest_is_filtered_and_sorted_before_limit(foundation, monkeypatch):
    client, root, add = foundation
    monkeypatch.setattr(application, 'is_ai_configured', lambda: False)
    day = datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0)
    def at(name, when):
        add(name)
        path = root / name
        os.utime(path, (when.timestamp(), when.timestamp()))
        with application.db_session() as conn:
            application.update_index_file(conn,path,path.stat(),root_key=application.root_scope_key(),scan_id='time-test')
    for i in range(8): at(f'京东/订单{i}.csv', day + timedelta(minutes=i))
    at('京东/昨天.csv', day - timedelta(hours=1))
    at('天猫/京东对比.csv', day - timedelta(days=3))
    result = client.post('/api/ai/chat',json={'message':'找下今天京东最新的文件'}).json()
    assert result['retrieval']['total'] == 8
    assert [f['name'] for f in result['matched_files']] == [f'订单{i}.csv' for i in range(7,2,-1)]
    assert not result['related_files']
    assert '北京时间' in result['reply']


def test_no_today_results_keeps_history_separate_and_uses_model(foundation, monkeypatch):
    client, root, add = foundation
    add('京东/历史订单.csv',age=86400 * 3)
    add('天猫/今日订单.csv')
    prompts=[]
    monkeypatch.setattr(application,'is_ai_configured',lambda:True)
    def llm(messages, **kwargs):
        prompts.extend(messages)
        return '可以扩大日期范围继续查找。','test-model'
    monkeypatch.setattr(application,'call_llm',llm)
    result=client.post('/api/ai/chat',json={'message':'找下今天京东最新的文件'}).json()
    assert result['matched_files']==[]
    assert len(result['related_files'])==1
    assert '没有找到今天修改的京东文件' in result['reply']
    assert '历史文件' in result['reply']
    assert result['model_used']=='test-model'
    assert '天猫' not in str(prompts)
