"""Opt-in, varied business reports. All values remain explicitly marked as demo data."""
import csv
from datetime import date, datetime, timedelta, time
import hashlib
from io import StringIO
import os
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = 2
ZONE = ZoneInfo('Asia/Shanghai')
UNCLASSIFIED_DIRECTORY = '_未列入清单_1014全量扫描补齐'
REPORT_DIRECTORY = '每日经营报表'
# Domain-specific volume and report mix; stable for a given business date.
PROFILES = {
 '01':(8,17,('店铺销售日报','商品动销明细','店铺流量分析','订单履约日报')),
 '02':(13,26,('经销订单日报','渠道进销存','经销商回款明细','渠道毛利日报')),
 '03':(5,13,('门店销售日报','门店库存明细','退换货日报')),
 '04':(3,9,('经销回款台账','区域订单明细','渠道进销存')),
 '05':(3,10,('直播成交日报','直播场次明细','直播商品库存')),
 '06':(2,7,('达人带货日报','内容发布明细','佣金结算台账')),
 '07':(2,8,('站外推广日报','广告计划效果','营销费用明细')),
 '08':(0,3,('经营指标汇总','品类经营分析')),
 '09':(1,4,('新品上市简报','新品进度台账','产品测试记录')),
 '10':(4,12,('广告投放日报','推广计划明细','投放预算执行')),
 '11':(0,5,('达人内容周报','内容发布明细','达人结算台账')),
 '12':(2,6,('渠道投放日报','营销费用明细','投放预算执行')),
 '13':(0,2,('供应商对账明细','供应商档案更新')),
 '14':(0,2,('行业大盘摘要','行业样本数据')),
 '15':(1,5,('直播成交日报','专场佣金明细')),
 '16':(0,3,('品牌内容日清','达人带货日报')),
 '17':(2,6,('视频电商成交日报','订单履约日报')),
 '18':(0,2,('行业趋势观察','消费调研汇总')),
 '19':(2,7,('发货开单日报','运费结算明细','订单履约日报')),
 '20':(1,3,('集团经营快报','经营费用明细')),
 '21':(0,2,('调研样本汇总','行业数据摘录')),
 '22':(0,3,('创意素材效果','内容发布明细')),
 '23':(3,9,('全渠道销售日报','销售目标达成','退款明细日报')),
 '24':(1,5,('社交电商订单日报','渠道毛利日报')),
 '25':(0,2,('历史经营数据核对','归档目录清单')),
}
MONITORS = (
 ('09','新品上市简报更新监控','新品上市简报','10:00','新品运营组'),
 ('04','渠道回款台账更新监控','经销回款台账','10:30','渠道财务组'),
 ('12','投放日报有效交付监控','渠道投放日报','11:00','投放分析组'),
 ('23','销售日报更新监控','全渠道销售日报','09:00','销售运营组'),
)

def seed_for(*parts):
    return int(hashlib.sha256('|'.join(map(str,parts)).encode()).hexdigest()[:12],16)

def report_content(day, domain, report, seed):
    output=StringIO(newline='');writer=csv.writer(output)
    if any(word in report for word in ('库存','进销存')):
        header=['业务日期','仓库','商品编码','期初库存','入库数量','出库数量','期末库存','数据说明'];kind='inventory'
    elif any(word in report for word in ('回款','对账','结算','佣金','费用','运费')):
        header=['业务日期','往来单位','应结金额','已结金额','未结金额','账龄天数','数据说明'];kind='finance'
    elif any(word in report for word in ('投放','广告','推广','预算')):
        header=['投放日期','渠道','计划名称','消耗','曝光量','点击量','成交金额','ROI','数据说明'];kind='ads'
    elif any(word in report for word in ('新品','产品','测试')):
        header=['更新日期','产品线','项目编码','当前阶段','完成度','负责人组','数据说明'];kind='product'
    else:
        header=['业务日期','渠道','店铺','订单数','支付金额','退款金额','净销售额','数据说明'];kind='sales'
    writer.writerow(header)
    channels=('天猫','京东','抖音','自营商城','经销渠道','线下零售')
    for index in range(5+seed%19):
        value=seed_for(seed,index);channel=channels[value%len(channels)]
        amount=round(2800+value%180000+(value%100)/100,2)
        if kind=='inventory':
            initial=120+value%2000;incoming=value%210;outgoing=value%180
            row=[day.isoformat(),('华东仓','华南仓','华北仓')[index%3],f'SKU-{10000+value%90000}',initial,incoming,outgoing,initial+incoming-outgoing]
        elif kind=='finance':
            paid=round(amount*(.55+(value%46)/100),2)
            row=[day.isoformat(),f'{domain[3:].split("-")[0]}合作商{index+1:02d}',amount,paid,round(amount-paid,2),value%26]
        elif kind=='ads':
            cost=round(amount*.17,2);gmv=round(cost*(1.4+value%290/100),2)
            row=[day.isoformat(),channel,f'{channel}-计划{index+1:02d}',cost,3000+value%180000,80+value%2200,gmv,round(gmv/cost,2)]
        elif kind=='product':
            row=[day.isoformat(),('家电','个护','家居')[index%3],f'NEW-{26000+value%1000}',('样机测试','上市准备','渠道铺货','复盘')[value%4],40+value%61,'新品运营组']
        else:
            refund=round(amount*(value%12)/100,2)
            row=[day.isoformat(),channel,f'{domain[3:].split("-")[0]}-{channel}店',15+value%760,amount,refund,round(amount-refund,2)]
        writer.writerow([*row,'模拟演示数据'])
    return output.getvalue().encode('utf-8-sig')

def reports_for(root: Path, today: date, excluded_terms=()):
    domains=sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
                   and len(path.name)>3 and path.name[:2].isdigit() and path.name[2]=='-'
                   and not any(term in path.name for term in excluded_terms))
    # Cap planned times to the current hour, never manufacturing a future update.
    wall_time=datetime.now(ZONE).replace(minute=0,second=0,microsecond=0)
    current=datetime.combine(today,wall_time.time(),tzinfo=ZONE).timestamp()
    for domain in domains:
        code=domain.name[:2];low,high,names=PROFILES.get(code,(1,4,('业务明细日报','经营费用明细')))
        for age in range(7):
            day=today-timedelta(days=age);seed=seed_for(day,domain.name)
            count=low+seed%(high-low+1)
            if day.weekday()>=5:count=max(0,round(count*.55))
            chosen=[names[i%len(names)] for i in range(count)]
            # These explicit scenarios exercise report-specific monitors, while
            # other reports in the same department continue to update normally.
            for monitor_code,_,keyword,_,_ in MONITORS:
                if code==monitor_code:
                    chosen=[name for name in chosen if name!=keyword]
                    if not (code=='09' and age<2) and not (code=='04' and age==0):chosen.insert(0,keyword)
            seen={}
            for index,report in enumerate(chosen):
                seen[report]=seen.get(report,0)+1
                suffix='' if seen[report]==1 else f'_批次{seen[report]:02d}'
                relative=Path(domain.name)/REPORT_DIRECTORY/f'{report}_{day.isoformat()}{suffix}.csv'
                report_seed=seed_for(day,domain.name,report,index)
                seconds=(8+report_seed%9)*3600+(report_seed//13%60)*60
                timestamp=datetime.combine(day,time.min,tzinfo=ZONE).timestamp()+seconds
                if code=='23' and report=='全渠道销售日报':timestamp=datetime.combine(day,time(8,35),tzinfo=ZONE).timestamp()
                content=b'' if code=='12' and report=='渠道投放日报' and age==0 else report_content(day,domain.name,report,report_seed)
                yield relative.as_posix(),content,min(timestamp,current)

def is_owned_report(relative):
    parts=Path(relative).parts
    prefixes={name for _,_,names in PROFILES.values() for name in names}|{'渠道销售日报','经营费用明细','业务明细日报'}
    return (len(parts)==3 and parts[1]==REPORT_DIRECTORY and parts[0][:2].isdigit() and parts[0][2:3]=='-'
            and parts[2].endswith('.csv') and any(parts[2].startswith(prefix+'_') for prefix in prefixes)
            and not any(part in ('.','..') for part in parts))

def refresh(root: Path, today: date, previous: list[str], *, excluded_terms=(), enabled=True):
    previous={name for name in previous if is_owned_report(name)}
    created,updated,removed,existing=[],[],[],[];current=set()
    for relative,content,timestamp in (reports_for(root,today,excluded_terms) if enabled else []):
        path=root/relative;current.add(relative)
        if path.exists() and relative not in previous:continue
        if any(parent.is_symlink() for parent in path.parents if parent!=root.parent):continue
        if not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content);os.utime(path,(timestamp,timestamp));created.append(relative)
        elif path.read_bytes()!=content or abs(path.stat().st_mtime-timestamp)>.01:
            path.write_bytes(content);os.utime(path,(timestamp,timestamp));updated.append(relative)
        existing.append(relative)
    for relative in previous-current:
        path=root/relative
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):path.unlink()
        removed.append(relative)
    return {'paths':sorted(existing),'created':created,'updated':updated,'removed':removed,'day':today.isoformat(),'version':VERSION}
