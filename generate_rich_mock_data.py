#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为「网盘模拟数据」生成真实、丰富、多工作表（Multi-Sheet）且带数值指标与分类维度的业务表格。
包含：
1. 覆盖 25 个顶级目录中的关键业务表格（电商、财务、推广ROI、日报、销售汇总、库存周转等）；
2. 注入真实合理的业务数据，每张表包含数千行结构化记录；
3. 支持多工作表（明细、汇总看板、说明），支持日期、分类、数值计算。
"""

import os
import random
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

ROOT_DIR = Path("/Users/ltq/Desktop/项目/文件检索平台/网盘模拟数据")
random.seed(42)

PLATFORMS = ["天猫旗舰店", "淘宝自营", "京东自营", "京东POP", "抖音直播", "抖音商城", "拼多多主站", "拼多多百亿补贴", "快手小店"]
REGIONS = ["华东", "华南", "华北", "华中", "西南", "西北", "东北"]
PROVINCES = {
    "华东": ["浙江省", "江苏省", "上海市", "山东省", "安徽省"],
    "华南": ["广东省", "福建省", "广西壮族自治区"],
    "华北": ["北京市", "天津市", "河北省", "山西省"],
    "华中": ["湖北省", "湖南省", "河南省"],
    "西南": ["四川省", "重庆市", "云南省", "贵州省"],
    "西北": ["陕西省", "甘肃省", "新疆维吾尔自治区"],
    "东北": ["辽宁省", "吉林省", "黑龙江省"],
}
CATEGORIES = {
    "数码家电": ["智能手表", "无线耳机", "空气净化器", "扫地机器人", "微波炉", "电吹风"],
    "美妆个护": ["洁面乳", "爽肤水", "保湿面霜", "防晒霜", "洗发水", "沐浴露"],
    "食品饮料": ["高山绿茶", "纯牛奶", "坚果礼盒", "低糖麦片", "黑咖啡", "矿泉水"],
    "家居日用": ["乳胶枕头", "四件套", "保温水杯", "收纳整理箱", "智能台灯", "折叠衣架"],
}
SUPPLIERS = ["华星供应链", "远航贸易", "天际工业", "北辰制造", "启明实业", "海川机电", "联盛材料", "宏源公司", "创科贸易", "安泰供应", "锦程集团", "恒瑞科技"]
DEPARTMENTS = ["采购一部", "采购二部", "电商运营部", "供应链管理部", "市场推广部", "财务结算部", "设备运维组"]
CHANNELS = ["巨量千川", "阿里妈妈引力魔方", "直通车", "京东快车", "品销宝", "小红书聚光", "磁力金牛", "微信视频号广告"]
WAREHOUSES = ["华东嘉兴一号仓", "华南广州总仓", "华北廊坊枢纽仓", "华中武汉集散仓", "西南成都配送中心", "西北西安前置仓"]
MANAGERS = ["张一", "李二", "王三", "陈四", "刘五", "赵六", "周七", "吴八"]

def generate_ecommerce_sales_df(n_rows=3000):
    start_date = datetime(2026, 1, 1)
    records = []
    for i in range(1, n_rows + 1):
        delta = random.randint(0, 280)
        dt = start_date + timedelta(days=delta)
        plat = random.choice(PLATFORMS)
        reg = random.choice(REGIONS)
        prov = random.choice(PROVINCES[reg])
        cat1 = random.choice(list(CATEGORIES.keys()))
        prod = random.choice(CATEGORIES[cat1])
        qty = random.randint(1, 8)
        unit_price = round(random.uniform(29.9, 999.0), 2)
        discount = round(random.choice([0, 5, 10, 20, 50, 100]), 2)
        sales = round(qty * unit_price, 2)
        paid = max(round(sales - discount, 2), 0)
        refund = round(paid if random.random() < 0.04 else 0, 2)
        profit = round(paid * random.uniform(0.18, 0.45) - (refund * 0.8), 2)
        records.append({
            "订单编号": f"ORD{dt.strftime('%Y%m%d')}{i:05d}",
            "成交日期": dt.strftime("%Y-%m-%d"),
            "月份": f"{dt.month:02d}月",
            "季度": f"Q{(dt.month - 1) // 3 + 1}",
            "销售平台": plat,
            "区域": reg,
            "省份": prov,
            "一级品类": cat1,
            "商品名称": prod,
            "采购负责人": random.choice(MANAGERS),
            "购买件数": qty,
            "销售单价": unit_price,
            "销售原价": sales,
            "优惠减免": discount,
            "实付金额": paid,
            "退款金额": refund,
            "毛利润": profit,
            "物流履约": random.choice(["顺丰冷链", "京东快递", "中通速运", "圆通快递"]),
            "支付方式": random.choice(["支付宝", "微信支付", "白条分期", "花呗"]),
        })
    return pd.DataFrame(records)

def generate_supplier_settlement_df(n_rows=1500):
    start_date = datetime(2026, 1, 1)
    records = []
    for i in range(1, n_rows + 1):
        dt = start_date + timedelta(days=random.randint(0, 280))
        supp = random.choice(SUPPLIERS)
        dept = random.choice(DEPARTMENTS)
        reg = random.choice(REGIONS)
        cat = random.choice(["电子元器件", "精密材料", "外包装辅料", "生产配件", "日常耗材", "办公设备"])
        amount = round(random.uniform(3000, 180000), 2)
        paid_ratio = random.choice([1.0, 1.0, 0.8, 0.5, 0.0])
        paid = round(amount * paid_ratio, 2)
        unpaid = round(amount - paid, 2)
        overdue = "是" if (unpaid > 0 and random.random() < 0.25) else "否"
        status = "已结算" if unpaid == 0 else ("逾期未付" if overdue == "是" else "待对账")
        records.append({
            "结算单号": f"JS{dt.strftime('%Y%m')}{i:04d}",
            "对账日期": dt.strftime("%Y-%m-%d"),
            "月份": f"{dt.month:02d}月",
            "所属季度": f"Q{(dt.month - 1) // 3 + 1}",
            "供应商名称": supp,
            "业务板块": dept,
            "供应商区域": reg,
            "采购物料类别": cat,
            "对账责任人": random.choice(MANAGERS),
            "结算金额": amount,
            "已付金额": paid,
            "待付尾款": unpaid,
            "是否逾期": overdue,
            "对账状态": status,
            "账期天数": random.choice([30, 45, 60, 90]),
        })
    return pd.DataFrame(records)

def generate_marketing_roi_df(n_rows=1200):
    start_date = datetime(2026, 1, 1)
    records = []
    for i in range(1, n_rows + 1):
        dt = start_date + timedelta(days=random.randint(0, 280))
        channel = random.choice(CHANNELS)
        cat = random.choice(list(CATEGORIES.keys()))
        impressions = random.randint(10000, 500000)
        clicks = int(impressions * random.uniform(0.015, 0.065))
        cost = round(clicks * random.uniform(0.6, 3.8), 2)
        orders = int(clicks * random.uniform(0.02, 0.09))
        gmv = round(orders * random.uniform(90, 450), 2)
        roi = round(gmv / max(cost, 1.0), 2)
        records.append({
            "投放日期": dt.strftime("%Y-%m-%d"),
            "月份": f"{dt.month:02d}月",
            "季度": f"Q{(dt.month - 1) // 3 + 1}",
            "推广渠道": channel,
            "投放类目": cat,
            "运营负责人": random.choice(MANAGERS),
            "曝光量": impressions,
            "点击量": clicks,
            "点击率百分比": round(clicks / max(impressions, 1) * 100, 2),
            "广告消耗金额": cost,
            "成交订单数": orders,
            "成交GMV": gmv,
            "投入产出比ROI": roi,
            "客单价": round(gmv / max(orders, 1), 2),
        })
    return pd.DataFrame(records)

def generate_inventory_df(n_rows=1000):
    records = []
    for i in range(1, n_rows + 1):
        cat1 = random.choice(list(CATEGORIES.keys()))
        prod = random.choice(CATEGORIES[cat1])
        wh = random.choice(WAREHOUSES)
        stock = random.randint(50, 6000)
        safe = random.randint(300, 1500)
        daily_sale = random.randint(10, 200)
        days = round(stock / max(daily_sale, 1), 1)
        cost_price = round(random.uniform(20, 400), 2)
        total_val = round(stock * cost_price, 2)
        status = "积压预警" if days > 60 else ("库存短缺" if stock < safe else "正常周转")
        records.append({
            "SKU编码": f"SKU{cat1[:2]}{i:04d}",
            "品类": cat1,
            "商品名称": f"{prod}-{random.choice(['标准款', '升级款', '旗舰版', '经济装'])}",
            "所属仓库": wh,
            "库存件数": stock,
            "安全库存": safe,
            "日均销量": daily_sale,
            "库存可用天数": days,
            "成本单价": cost_price,
            "库存总金额": total_val,
            "库存状态": status,
            "仓管负责人": random.choice(MANAGERS),
        })
    return pd.DataFrame(records)

def save_rich_workbook(target_path: Path, df_main: pd.DataFrame, sheet1_name: str, group_by_col: str, sum_col: str):
    target_path.parent.mkdir(parents=True, exist_ok=True)
    pivot_summary = df_main.groupby(group_by_col)[sum_col].agg(["count", "sum"]).reset_index()
    pivot_summary.columns = [group_by_col, "记录数", f"{sum_col}合计"]
    
    meta_df = pd.DataFrame([
        {"属性项": "报表名称", "说明值": target_path.name},
        {"属性项": "生成时间", "说明值": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"属性项": "有效行数", "说明值": len(df_main)},
        {"属性项": "业务主键", "说明值": list(df_main.columns)[:3]},
        {"属性项": "数据口径", "说明值": "企业内网真实脱敏报表，包含多维度与可聚合指标"},
    ])
    
    if target_path.suffix.lower() == ".csv":
        df_main.to_csv(target_path, index=False, encoding="utf-8-sig")
        return

    with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
        df_main.to_excel(writer, sheet_name=sheet1_name, index=False)
        pivot_summary.to_excel(writer, sheet_name="分类统计汇总", index=False)
        meta_df.to_excel(writer, sheet_name="数据字典与说明", index=False)

def main():
    print("开始为「网盘模拟数据」生成真实、丰富的高频业务工作簿...")
    count = 0

    # 1. 根目录与常用入口快捷表
    key_files = [
        (ROOT_DIR / "20-日报" / "月度销售数据明细大表_2026.xlsx", "销售订单明细", generate_ecommerce_sales_df(3500), "销售平台", "实付金额"),
        (ROOT_DIR / "20-日报" / "供应商月度结算与对账明细汇总.xlsx", "结算明细", generate_supplier_settlement_df(2000), "供应商名称", "结算金额"),
        (ROOT_DIR / "12-推广ROI周报" / "2026各渠道推广消耗与ROI周报.xlsx", "推广ROI明细", generate_marketing_roi_df(1800), "推广渠道", "成交GMV"),
        (ROOT_DIR / "23-销售数据汇总" / "全国各仓库商品库存与周转监控表.xlsx", "库存周转明细", generate_inventory_df(1500), "所属仓库", "库存总金额"),
        (ROOT_DIR / "1-阿里平台-自营" / "天猫自营商品销售与利润明细表.xlsx", "天猫自营销售", generate_ecommerce_sales_df(2500), "一级品类", "实付金额"),
        (ROOT_DIR / "3-京东平台-自营" / "京东自营全品类采购与销售周报.xlsx", "京东销售", generate_ecommerce_sales_df(2500), "区域", "毛利润"),
        (ROOT_DIR / "5-抖音平台-自营" / "抖音自营直播间带货与投放转化分析.xlsx", "直播销售数据", generate_ecommerce_sales_df(3000), "销售平台", "实付金额"),
        (ROOT_DIR / "24-拼多多" / "拼多多百亿补贴销售与毛利核算表.xlsx", "拼多多明细", generate_ecommerce_sales_df(2200), "省份", "实付金额"),
        (ROOT_DIR / "4-京东平台-分销" / "京东分销渠道结算与库存对账.xlsx", "分销结算", generate_supplier_settlement_df(1500), "供应商名称", "结算金额"),
        (ROOT_DIR / "2-阿里平台-分销" / "分销商采购结算与返利明细.xlsx", "分销对账", generate_supplier_settlement_df(1500), "业务板块", "已付金额"),
        (ROOT_DIR / "10-站外巨量" / "巨量引擎千川广告投放ROI日报.xlsx", "广告明细", generate_marketing_roi_df(1600), "投放类目", "广告消耗金额"),
        (ROOT_DIR / "11-小红书达人底层" / "小红书聚光种草与ROI转化周报.xlsx", "种草转化", generate_marketing_roi_df(1200), "推广渠道", "成交GMV"),
        (ROOT_DIR / "20-日报" / "每日经营销售明细数据汇总.csv", "数据", generate_ecommerce_sales_df(1500), "销售平台", "实付金额"),
        (ROOT_DIR / "23-销售数据汇总" / "各区域销售与回款综合分析表.xlsx", "区域分析", generate_ecommerce_sales_df(2800), "区域", "实付金额"),
        (ROOT_DIR / "8-策略屋留存" / "供应链全链路经营指标分析表.xlsx", "经营指标", generate_supplier_settlement_df(1800), "供应商名称", "结算金额"),
    ]

    for path, sheet_name, df, group_col, sum_col in key_files:
        save_rich_workbook(path, df, sheet_name, group_col, sum_col)
        count += 1
        print(f"已生成主要报表: {path.name} ({len(df)} 行, {len(df.columns)} 字段)")

    # 2. 遍历现有 25 个顶级目录，挑选高频子目录中的空文件，将它们替换为真实带数据的报表
    keywords = ["销售", "结算", "ROI", "周报", "明细", "流量", "日报", "订单", "对账", "库存"]
    replaced_count = 0
    for root, dirs, files in os.walk(ROOT_DIR):
        for f in files:
            if replaced_count >= 60:
                break
            if any(k in f for k in keywords) and f.endswith((".xlsx", ".csv")):
                target = Path(root) / f
                if target.stat().st_size == 0:  # 如果是 0 字节模拟空文件
                    if "ROI" in f or "推广" in f:
                        df = generate_marketing_roi_df(random.randint(600, 1500))
                        save_rich_workbook(target, df, "推广明细", "推广渠道", "成交GMV")
                    elif "结算" in f or "对账" in f or "供应商" in f:
                        df = generate_supplier_settlement_df(random.randint(500, 1200))
                        save_rich_workbook(target, df, "结算明细", "供应商名称", "结算金额")
                    elif "库存" in f:
                        df = generate_inventory_df(random.randint(400, 1000))
                        save_rich_workbook(target, df, "库存明细", "所属仓库", "库存总金额")
                    else:
                        df = generate_ecommerce_sales_df(random.randint(800, 2000))
                        save_rich_workbook(target, df, "销售明细", "销售平台", "实付金额")
                    replaced_count += 1
        if replaced_count >= 60:
            break

    print(f"完成！共注入 {count + replaced_count} 个多工作表、包含真实维度与指标的业务表格！")

if __name__ == "__main__":
    main()
