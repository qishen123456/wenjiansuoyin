"""Opt-in workflow examples; demo rows never confer download authorization."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

VERSION = 1
ZONE = ZoneInfo('Asia/Shanghai')
CASES = [
 ('pending',0.12,'月度经营复盘，需核对渠道销售与退款口径',''),
 ('reviewing',0.7,'区域经营例会材料，仅用于团队内部复盘','已转交业务负责人核实使用范围'),
 ('pending',1.8,'补充本周库存周转分析，核对缺货与滞销商品',''),
 ('reviewing',3.4,'渠道对账出现差异，需要核对原始报表','财务复核中，待确认脱敏范围'),
 ('ready',5.3,'制作投放复盘，核对推广花费与转化效率','同意用于内部复盘，请勿转发外部群组'),
 ('downloaded',22,'核对区域订单与经销商回款，准备周例会','用途明确，已核实业务范围'),
 ('rejected',29,'希望获取全量客户及交易明细，用于外部伙伴分析','外部共享范围不明确，请先申请脱敏版本并说明接收方'),
 ('downloaded',47,'月度结算复核，核对运费与发货开单差额','同意，结算完成后按规定保管数据'),
 ('expired',56,'补充部门经营分析材料，核对商品毛利','同意用于所述用途，授权到期后需重新申请'),
 ('ready',2.6,'商品经营分析，核对新品首月销量与毛利','同意，只用于新品运营小组内部分析'),
 ('rejected',79,'下载跨部门历史销售底表进行业务研究','申请范围过大，请明确月份、业务域与所需字段后重新提交'),
 ('downloaded',103,'复核达人佣金结算，核对成交订单与退款','业务负责人已确认，允许按结算用途使用'),
 ('expired',134,'补录经营周报图表，需要核对历史渠道数据','已批准；逾期未下载，授权自动失效'),
 ('downloaded',188,'季度库存盘点复核，核对商品出入库记录','同意，本次申请仅用于库存盘点复核'),
]

def seed(conn, root_key, now, ttl_hours=24):
    day=datetime.fromtimestamp(now,ZONE).date().isoformat()
    marker=f'demo_approvals:{root_key}:{day}:v{VERSION}'
    users=conn.execute("SELECT id,name,department FROM system_users WHERE status='active' AND role!='super_admin' ORDER BY id").fetchall()
    reviewers=conn.execute("SELECT name FROM system_users WHERE status='active' AND role IN ('super_admin','dept_admin') ORDER BY id").fetchall()
    if not users or not reviewers:return 0
    # Repair only untouched examples from the initial seed; never rewrite human reviews.
    seed_notes={item[3] for item in CASES}
    rows=conn.execute("SELECT id,user_name,reviewed_at,review_note,workflow_events FROM download_requests WHERE instr(demo_key,?)=1 AND reviewed_by=user_name AND reviewed_at>0",(f'demo_approvals:{root_key}:',)).fetchall()
    for row in rows:
        if row['review_note'] not in seed_notes:continue
        actor=next((r['name'] for r in reviewers if r['name']!=row['user_name']),None)
        if not actor:continue
        events=json.loads(row['workflow_events'])
        for event in events:
            if event['kind'] in {'approved','rejected'} and event['at']==row['reviewed_at']:event['actor']=actor
        conn.execute('UPDATE download_requests SET reviewed_by=?,workflow_events=? WHERE id=?',(actor,json.dumps(events,ensure_ascii=False),row['id']))
    if conn.execute('SELECT 1 FROM app_meta WHERE key=?',(marker,)).fetchone(): return 0
    count=0
    for i,(state,age,reason,note) in enumerate(CASES):
        created=now-age*3600
        # Historical requests reference files already present at the time of the application.
        files=conn.execute('SELECT id,name,path FROM files WHERE root_key=? AND size>0 AND mtime<=? ORDER BY mtime DESC,id LIMIT 100',(root_key,created)).fetchall()
        if not files:continue
        topic=['销售','经营','库存','回款','投放','订单','交易','运费','毛利','新品','销售','佣金','渠道','库存'][i]
        relevant=conn.execute('SELECT id,name,path FROM files WHERE root_key=? AND size>0 AND mtime<=? AND instr(name,?)>0 ORDER BY mtime DESC,id LIMIT 12',(root_key,created,topic)).fetchall()
        if relevant:files=relevant
        file=files[(i*7)%len(files)];user=users[i%len(users)]
        reviewer=next((r['name'] for r in reviewers[i%len(reviewers):]+reviewers[:i%len(reviewers)] if r['name']!=user['name']),reviewers[0]['name'])
        reviewed=0;expires=0;downloaded=0
        events=[{'kind':'submitted','at':created,'actor':user['name'],'note':reason}]
        pending=state in {'pending','reviewing'}
        if state=='reviewing':events.append({'kind':'reviewing','at':created+age*3600/3,'actor':reviewer,'note':note})
        if not pending:
            reviewed=created+min(age*3600/4,1800+137*i)
            status='rejected' if state=='rejected' else 'approved'
            expires=reviewed+max(1,ttl_hours)*3600 if status=='approved' else 0
            if state=='ready':expires=max(expires,now+3600)
            if state=='expired':expires=min(expires,now-3600)
            events.append({'kind':status,'at':reviewed,'actor':reviewer,'note':note})
            if state=='downloaded':
                viewed=reviewed+180;downloaded=min(reviewed+600,expires-1)
                events += [{'kind':'previewed','at':viewed,'actor':user['name'],'note':'在线核对表头、字段与业务范围'},
                           {'kind':'downloaded','at':downloaded,'actor':user['name'],'note':'下载完成，用于已批准的业务用途'}]
            elif state=='ready':events.append({'kind':'previewed','at':reviewed+120,'actor':user['name'],'note':'已在线预览，等待下载归档'})
            elif state=='expired':events.append({'kind':'expired','at':expires,'actor':'系统','note':'授权有效期已结束，本次申请未下载'})
        else:status='pending'
        conn.execute('''INSERT INTO download_requests(user_id,user_name,department,file_id,file_name,file_path,reason,status,
          ticket,created_at,reviewed_by,reviewed_at,expires_at,downloaded_at,demo_key,review_note,workflow_events)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (user['id'],user['name'],user['department'],file['id'],file['name'],file['path'],reason,status,'',created,
           reviewer if reviewed else '',reviewed,expires,downloaded,f'{marker}:{i}',note,json.dumps(events,ensure_ascii=False)))
        count+=1
    if count:conn.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)',(marker,json.dumps({'count':count,'version':VERSION})))
    return count
