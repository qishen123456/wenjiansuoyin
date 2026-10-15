#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
补齐脚本：把《文件清单_含行数.csv》未覆盖、但《网盘数据文件规模_1014.xlsx》全量扫描中
确实存在的文件，以「占位空文件」形式补进 网盘模拟数据/ 目录树。

已核实的事实（补齐依据，不做无根据的编造）：
  1. CSV 是 2026-09-22 之前的快照（最大修改时间 2026-09-22 17:07，2026-10 桶为 0）；
     xlsx 是 2026-10-14 的全量只读扫描 → 2026-10 的 7,727 个文件 CSV 必然缺失。
  2. CSV 只覆盖 8 个一级目录，xlsx 全量扫描有 25 个 → 17 个一级目录整目录未进清单(16,435 个)。
  3. xlsx 三个 sheet 内部自洽：按平台目录合计 = 按年份合计 = 按月份合计 = 总览 130,435；
     且没有任何月份出现 CSV > xlsx，目录维度缺口与月份维度缺口都等于 27,885 → 缺口真实。

补齐原则：
  - 数量、一级目录分布、扩展名分布、月份分布：全部严格按 xlsx 汇总对齐，
    缺口按「一级目录 × 扩展名」精确计算（xlsx「按平台目录」给了每个目录分扩展名个数），
    再与「按月份」缺口做三维整数对齐，不含任何按比例假设。
  - xlsx 未给出更深的子目录与文件名，因此使用可识别占位名 `_补位_*`，并写入 manifest，
    随时可用 --rollback 精确删除，不会碰 CSV 清单里的真实文件。
  - 17 个整目录未列入的一级目录：在其根目录下直接放置（该一级目录名本身是真实的）。
  - 8 个已覆盖目录的内部缺口：放入 `<一级目录>/_未列入清单_1014全量扫描补齐/`，
    避免污染 CSV 已给出的真实目录结构。

用法:
    python3 fill_gap_files.py --dry-run      # 只打印补齐计划
    python3 fill_gap_files.py                # 生成补齐文件
    python3 fill_gap_files.py --rollback     # 按 manifest 删除本次补齐内容
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "文件清单_含行数.csv")
XLSX_PATH = os.path.join(BASE, "网盘数据文件规模_1014.xlsx")
ROOT = os.path.join(BASE, "网盘模拟数据")
MANIFEST = os.path.join(BASE, "补齐清单_1014全量扫描.csv")
FILL_SUBDIR = "_未列入清单_1014全量扫描补齐"


def clean_name(s):
    """xlsx 单元格中的目录名可能带多余空格，规整成合法目录名。"""
    return re.sub(r"\s+", "", str(s).strip())


def days_in_month(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    last = time.mktime((ny, nm, 1, 0, 0, 0, 0, 0, -1))
    first = time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
    return max(1, round((last - first) / 86400))


def epoch_of(ym, day, hh, mm):
    """把 YYYY-MM + 日/时/分 变成 epoch；扫描截止日之后的日期压到截止日内。"""
    y, m = int(ym[:4]), int(ym[5:7])
    day = max(1, min(day, days_in_month(ym)))
    if (y, m) >= (2026, 10):
        day = min(day, 14)
    return time.mktime((y, m, day, hh % 24, mm % 60, 0, 0, 0, -1))


def largest_remainder(total, weights):
    """把 total 按 weights 比例整数分配（最大余数法）。"""
    s = sum(weights)
    if total <= 0 or s <= 0:
        return [0] * len(weights)
    raw = [total * w / s for w in weights]
    base = [int(x) for x in raw]
    left = total - sum(base)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[:left]:
        base[i] += 1
    return base


def allocate_matrix(row_totals, col_totals):
    """整数矩阵：行和 = row_totals，列和 = col_totals。
    采用「按列剩余容量做比例分配 + 最大余数取整」的顺序填充法，
    两侧总量相等时保证行列双向精确闭合。"""
    total = sum(row_totals.values())
    if total <= 0 or total != sum(col_totals.values()):
        raise ValueError(f"行列总量不相等，无法对齐: "
                         f"{sum(row_totals.values())} vs {sum(col_totals.values())}")
    cols = sorted(col_totals, key=lambda c: (-col_totals[c], c))
    cap = dict(col_totals)
    out = {}
    for r in sorted(row_totals, key=lambda k: (-row_totals[k], k)):
        need = row_totals[r]
        alloc = {c: 0 for c in cols}
        while need > 0:
            active = [c for c in cols if cap[c] > 0]
            if not active:
                raise RuntimeError(f"列容量已用尽但目录 {r} 仍缺 {need}，数据不自洽")
            weights = [cap[c] for c in active]
            take = min(need, sum(weights))
            parts = largest_remainder(take, weights)
            progressed = False
            for c, p in zip(active, parts):
                p = min(p, cap[c], need)
                if p > 0:
                    alloc[c] += p
                    cap[c] -= p
                    need -= p
                    progressed = True
            if not progressed:          # 取整后全为 0，退化为逐个发放
                for c in active:
                    if need == 0:
                        break
                    alloc[c] += 1
                    cap[c] -= 1
                    need -= 1
        out[r] = alloc
    for r, want in row_totals.items():
        if sum(out[r].values()) != want:
            raise RuntimeError(f"分配失败: 目录 {r} 行和不符")
    for c, want in col_totals.items():
        if sum(out[r][c] for r in out) != want:
            raise RuntimeError(f"分配失败: 月份 {c} 列和不符")
    return out


def read_xlsx_targets():
    try:
        import openpyxl
    except ImportError:
        sys.exit("需要 openpyxl: pip install openpyxl")
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)

    cat_total, cat_ext = {}, {}
    for row in wb["按平台目录"].iter_rows(min_row=2, values_only=True):
        if not row[0] or str(row[0]).strip() == "合计":
            continue
        name, n = clean_name(row[0]), int(row[1])
        ext = {}
        for idx, e in ((4, ".xlsx"), (5, ".xls"), (6, ".csv")):
            v = row[idx]
            ext[e] = int(str(v).split("/")[0]) if v else 0
        rest = n - sum(ext.values())        # 总览口径里的 .xlsm
        if rest > 0:
            ext[".xlsm"] = rest
        cat_total[name], cat_ext[name] = n, ext

    month_total = {}
    for row in wb["按月份"].iter_rows(min_row=2, values_only=True):
        if row[0]:
            month_total[str(row[0]).strip()] = int(row[1])

    grand = None
    for row in wb["总览"].iter_rows(values_only=True):
        if row and str(row[0]).strip() == "表格文件总数":
            grand = int(row[1])
    return cat_total, cat_ext, month_total, grand


def read_csv_actual():
    cat, month, cat_ext = Counter(), Counter(), Counter()
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            c = clean_name((row["相对路径"] or "").split("\\")[0])
            cat[c] += 1
            ext = "." + (row["扩展名"] or "").lstrip(".").lower()
            cat_ext[(c, ext)] += 1
            t = (row["修改时间"] or "").strip()[:7]
            if t:
                month[t] += 1
    return cat, month, cat_ext


def main():
    ap = argparse.ArgumentParser(description="按 xlsx 全量扫描口径补齐缺失文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印补齐计划")
    ap.add_argument("--rollback", action="store_true", help="按 manifest 删除本次补齐内容")
    args = ap.parse_args()

    if args.rollback:
        if not os.path.exists(MANIFEST):
            sys.exit(f"没有 manifest 可回滚: {MANIFEST}")
        n = 0
        dirs = set()
        with open(MANIFEST, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                p = os.path.join(ROOT, row["相对路径"])
                if os.path.isfile(p) and os.path.getsize(p) == 0:
                    os.remove(p)
                    n += 1
                    dirs.add(os.path.dirname(p))
                    if row.get("一级目录"):
                        dirs.add(os.path.join(ROOT, row["一级目录"], FILL_SUBDIR))
        for d in sorted(dirs, key=lambda x: -x.count(os.sep)):
            try:
                os.rmdir(d)                     # 仅删除空目录
            except OSError:
                pass
        os.rename(MANIFEST, MANIFEST + ".已回滚")
        print(f"已回滚删除 {n:,} 个补齐文件（manifest 之外的文件未做任何改动）")
        print(f"  manifest 已改名留档: {MANIFEST}.已回滚")
        return

    cat_target, cat_ext_target, month_target, grand = read_xlsx_targets()
    cat_now, month_now, ext_now = read_csv_actual()

    print("对齐基准：网盘数据文件规模_1014.xlsx（2026-10-14 全量扫描）")
    print(f"  总览「表格文件总数」       : {grand:,}")
    print(f"  按平台目录 合计            : {sum(cat_target.values()):,}  ({len(cat_target)} 个一级目录)")
    print(f"  按月份   合计              : {sum(month_target.values()):,}  ({len(month_target)} 个月)")
    print(f"  CSV 清单 / 磁盘现有        : {sum(cat_now.values()):,}  ({len(cat_now)} 个一级目录)")

    # 缺口按「一级目录 × 扩展名」精确计算：xlsx「按平台目录」给了每个目录分扩展名的个数，
    # CSV 给了已收录文件的真实扩展名，两者相减即为该目录该扩展名的真实缺口，无需按比例假设。
    row_gap, over = {}, []
    for c, exts in cat_ext_target.items():
        for e, want in exts.items():
            got = ext_now.get((c, e), 0)
            if want - got < 0:
                over.append(f"{c}{e}(多 {got - want})")
            elif want - got > 0:
                row_gap[(c, e)] = want - got
    if over:
        print(f"  ⚠️ CSV 反而多于汇总的 目录×扩展名（不做删除，仅提示）: {over}")
        sys.exit("存在 CSV 多于汇总的扩展名维度，两个口径冲突，终止（不猜测该删谁）")

    month_gap = {m: month_target[m] - month_now.get(m, 0) for m in month_target}
    month_gap = {m: v for m, v in month_gap.items() if v > 0}
    cat_gap = {c: cat_target[c] - cat_now.get(c, 0) for c in cat_target}
    cat_gap = {c: v for c, v in cat_gap.items() if v > 0}
    print(f"  目录×扩展名 维度缺口       : {sum(row_gap.values()):,}")
    print(f"  目录维度缺口               : {sum(cat_gap.values()):,}")
    print(f"  月份维度缺口               : {sum(month_gap.values()):,}")
    if not (sum(row_gap.values()) == sum(cat_gap.values()) == sum(month_gap.values())):
        sys.exit("三个维度缺口不自洽，终止（不猜测补多少）")

    plan = allocate_matrix(row_gap, month_gap)
    gap_by_cat = Counter()
    for (c, e), v in row_gap.items():
        gap_by_cat[c] += v

    print("\n补齐计划（一级目录 × 扩展名 × 月份 三维精确对齐 xlsx 汇总）")
    print(f"  {'一级目录':<22}{'补齐数':>8}  {'类型':<12}分扩展名缺口")
    new_dirs = [c for c in gap_by_cat if cat_now.get(c, 0) == 0]
    for c in sorted(gap_by_cat, key=lambda k: -gap_by_cat[k]):
        dist = " ".join(f"{e}:{row_gap[(c, e)]:,}"
                        for e in sorted((e for (cc, e) in row_gap if cc == c),
                                        key=lambda e: -row_gap[(c, e)]))
        kind = "整目录未列入" if cat_now.get(c, 0) == 0 else "目录内部缺口"
        print(f"  {c:<22}{gap_by_cat[c]:>8,}  {kind:<12}{dist}")

    if args.dry_run:
        print("\n[dry-run] 未创建任何文件")
        return

    os.makedirs(ROOT, exist_ok=True)
    created = []
    for c in sorted(gap_by_cat, key=lambda k: -gap_by_cat[k]):
        is_new = cat_now.get(c, 0) == 0
        sub = "" if is_new else FILL_SUBDIR
        kind = "整目录未列入" if is_new else "目录内部缺口"
        exts = sorted((e for (cc, e) in row_gap if cc == c), key=lambda e: -row_gap[(c, e)])
        seq = 0
        # 各扩展名的文件在月份上按列分配结果穿插落位，序号在目录内连续以保证文件名唯一
        blocks = [(e, m, plan[(c, e)][m]) for e in exts
                  for m in sorted(plan[(c, e)]) if plan[(c, e)][m]]
        for e, m, k in blocks:
            for _ in range(k):
                seq += 1
                rel = "/".join(x for x in (c, sub, f"_补位_{m}_{seq:06d}{e}") if x)
                target = os.path.join(ROOT, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                ts = epoch_of(m, 1 + (seq * 7) % 28, (seq * 5) % 24, (seq * 13) % 60)
                if not os.path.exists(target):
                    with open(target, "wb"):
                        pass
                    os.utime(target, (ts, ts))
                created.append((rel, c, e, m, kind, time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))))

    with open(MANIFEST, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["相对路径", "一级目录", "扩展名", "归属月份", "补齐类型", "设定修改时间", "补齐依据"])
        for row in created:
            w.writerow(list(row) + ["网盘数据文件规模_1014.xlsx 全量扫描"])

    fill_dirs = [c for c in gap_by_cat if cat_now.get(c, 0) > 0]
    print(f"\n完成：新增 {len(created):,} 个占位空文件")
    print(f"  新建一级目录 {len(new_dirs)} 个（文件直接放在该目录下）")
    print(f"  已有目录内补 {len(fill_dirs)} 个（放在 '{FILL_SUBDIR}/' 子目录下）")
    print(f"  manifest : {MANIFEST}")
    print(f"  回滚     : python3 {os.path.basename(__file__)} --rollback")


if __name__ == "__main__":
    main()
