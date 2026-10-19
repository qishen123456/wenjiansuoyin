import sqlite3
import time
from datetime import datetime

conn = sqlite3.connect('/app/data/file_index.db')
conn.row_factory = sqlite3.Row

# 清理并重建规则
conn.execute('DELETE FROM archive_rules')
conn.execute('DELETE FROM archive_tasks')

now = time.time()

rules = [
    (1, '21-中怡康 - 日增合规监控', '/data/share/21-中怡康', 'daily', 1, '市场情报组 · 陈专员'),
    (2, '7-站外UD - 7天周度归档监控', '/data/share/7-站外UD', 'weekly', 7, '站外投放组 · 小张'),
    (3, '16-星图种草 - 3天频次监控', '/data/share/16-星图种草', 'custom', 3, '媒介达人组 · 王主管'),
    (4, '9-新品简报 - 日增合规监控', '/data/share/9-新品简报', 'daily', 1, '新品运营组 · 李运营'),
    (5, '12-推广ROI周报 - 7天周增监控', '/data/share/12-推广ROI周报', 'weekly', 7, '投放推广组 · 钱总监'),
    (6, '2-阿里平台-分销 - 30天月度核算', '/data/share/2-阿里平台-分销', 'monthly', 30, '渠道分销部 · 赵助理'),
]

for r in rules:
    conn.execute('''
        INSERT INTO archive_rules (id, name, target_path, period_type, period_days, responsible_person, is_active, last_checked_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
    ''', (r[0], r[1], r[2], r[3], r[4], r[5], now - 180, now - 86400 * 30))

# 示例截图 base64 仿真图
svg_proof_1 = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="360" height="200" viewBox="0 0 360 200"><rect width="360" height="200" fill="%231e293b" rx="8"/><rect x="15" y="15" width="330" height="30" fill="%23334155" rx="4"/><circle cx="30" cy="30" r="5" fill="%23ef4444"/><circle cx="45" cy="30" r="5" fill="%23f59e0b"/><circle cx="60" cy="30" r="5" fill="%2310b981"/><text x="80" y="34" fill="%2394a3b8" font-size="11" font-family="monospace">\\\\172.11.111.11\\share\\7-站外UD</text><rect x="15" y="55" width="330" height="130" fill="%230f172a" rx="4"/><text x="30" y="85" fill="%2338bdf8" font-size="12" font-family="sans-serif">📁 2026年10月站外投放复盘与消耗明细.xlsx</text><text x="30" y="110" fill="%2310b981" font-size="11" font-family="sans-serif">✓ 大小: 248.5 KB · 写入时间: 今天 09:12</text><text x="30" y="135" fill="%2394a3b8" font-size="11" font-family="sans-serif">校验状态: MD5完整性一致 · 财务核验讫</text><rect x="230" y="140" width="100" height="28" fill="%232563eb" rx="4"/><text x="245" y="158" fill="white" font-size="11" font-family="sans-serif">归档凭证有效</text></svg>'

svg_proof_2 = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="360" height="200" viewBox="0 0 360 200"><rect width="360" height="200" fill="%230f172a" rx="8"/><rect x="15" y="15" width="330" height="30" fill="%231e293b" rx="4"/><text x="30" y="34" fill="%23a855f7" font-size="12" font-weight="bold" font-family="sans-serif">星图平台达人带货转化底表归档</text><line x1="15" y1="52" x2="345" y2="52" stroke="%23334155"/><text x="30" y="80" fill="%23e2e8f0" font-size="11" font-family="sans-serif">文件: 星图达人投放底表_20261017.xlsx</text><text x="30" y="105" fill="%2394a3b8" font-size="11" font-family="sans-serif">归档路径: /data/share/16-星图种草/</text><text x="30" y="130" fill="%2334d399" font-size="11" font-family="sans-serif">已包含: 38位达人播放、佣金与回款数据</text><rect x="210" y="145" width="120" height="24" fill="%237c3aed" rx="12"/><text x="225" y="161" fill="white" font-size="10" font-family="sans-serif">媒介组王主管确认</text></svg>'

svg_proof_rejected = 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="360" height="200" viewBox="0 0 360 200"><rect width="360" height="200" fill="%231e293b" rx="8"/><rect x="15" y="15" width="330" height="170" fill="%230f172a" rx="4"/><text x="30" y="50" fill="%23f87171" font-size="13" font-weight="bold">天猫分销销售台账(草稿未核完).xlsx</text><text x="30" y="80" fill="%2394a3b8" font-size="11">部分月份缺少毛利字段</text><rect x="220" y="135" width="110" height="28" fill="%23ef4444" rx="4"/><text x="235" y="153" fill="white" font-size="11">驳回整改记录</text></svg>'

# 插入 6 条具备全生命周期示范价值的工单
tasks = [
    # 1. 待整改
    (1, '21-中怡康 - 日增合规监控', '/data/share/21-中怡康', '每日新增', '市场情报组 · 陈专员',
     now - 86400 * 4.4, '_补位_2026-10_000019.xlsx', 4, 'pending',
     '', '', '', 0, '', 0, '', 0, '', now - 86400 * 4, now - 3600),

    # 2. 待审核 - 1
    (2, '7-站外UD - 7天周度归档监控', '/data/share/7-站外UD', '每 7 天新增', '站外投放组 · 小张',
     now - 86400 * 17.5, '_补位_2026-10_000004.xlsx', 11, 'submitted',
     '已按合规规范将《2026年10月站外投放复盘与消耗明细.xlsx》上传至内网共享盘，完成各渠道数据核对校验，请领导审核消结。',
     svg_proof_1, '投放专员小张', now - 3600 * 2, '', 0, '', 0, '', now - 86400 * 10, now - 3600 * 2),

    # 3. 待审核 - 2
    (3, '16-星图种草 - 3天频次监控', '/data/share/16-星图种草', '每 3 天新增', '媒介达人组 · 王主管',
     now - 86400 * 4.5, '_补位_2026-10_000014.xlsx', 2, 'submitted',
     '上周末达人回款与视频播放数据已汇总完毕，文件名《星图达人投放底表_20261017.xlsx》已上传，附协同核对截图证明。',
     svg_proof_2, '媒介组王主管', now - 3600 * 4, '', 0, '', 0, '', now - 86400 * 2, now - 3600 * 4),

    # 4. 定时扫描自动消结
    (4, '9-新品简报 - 日增合规监控', '/data/share/9-新品简报', '每日新增', '新品运营组 · 李运营',
     now - 3600 * 10, '_补位_2026-10_005731.xlsx', 0, 'auto_resolved',
     '后台定时巡检于 08:30:15 自动检测到该文件夹已新增合规报表文件《_补位_2026-10_005731.xlsx》，系统已自动消结待办。',
     '', '', 0, '', 0, '', now - 3600 * 2, '_补位_2026-10_005731.xlsx', now - 86400, now - 3600 * 2),

    # 5. 管理员审核通过
    (5, '12-推广ROI周报 - 7天周增监控', '/data/share/12-推广ROI周报', '每 7 天新增', '投放推广组 · 钱总监',
     now - 3600 * 11, '_补位_2026-10_000187.xlsx', 0, 'approved',
     '各渠道推广消耗周报已生成并归档至内网 12-推广ROI周报 目录。',
     svg_proof_1, '钱总监', now - 86400, '高级管理员', now - 3600 * 5, '数据完整准确，与财务系统对账一致，审核通过消结。', now - 3600 * 5, '_补位_2026-10_000187.xlsx', now - 86400 * 3, now - 3600 * 5),

    # 6. 管理员已驳回待整改
    (6, '2-阿里平台-分销 - 30天月度核算', '/data/share/2-阿里平台-分销', '每月新增', '渠道分销部 · 赵助理',
     now - 86400 * 35, '店铺商品销售分析-2025-12-28--安吉尔净水器旗舰店.xlsx', 5, 'rejected',
     '已上传本月分销初步台账。',
     svg_proof_rejected, '分销专员小赵', now - 3600 * 8, '高级管理员', now - 3600 * 3, '驳回原因：缺少分销毛利核算副表，且截图不清晰，请补全明细后重新提交。', 0, '', now - 86400 * 6, now - 3600 * 3),
]

for t in tasks:
    conn.execute('''
        INSERT INTO archive_tasks (
            rule_id, rule_name, target_path, period_desc, responsible_person,
            last_file_time, last_file_name, overdue_days, status,
            submit_note, proof_image, submitted_by, submitted_at,
            reviewed_by, reviewed_at, review_comment, resolved_at, resolved_file,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', t)

conn.commit()
print("Rich demonstration archive tasks seeded successfully.")
