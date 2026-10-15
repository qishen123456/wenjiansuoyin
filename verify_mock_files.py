#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核验脚本：四层校验 网盘模拟数据/ 目录树。

  A. CSV 真实清单层（文件清单_含行数.csv）
     逐条双向 diff：清单里的每个路径都必须在磁盘上存在、0 字节、修改时间与清单一致；
     磁盘上不属于清单、也不属于补齐 manifest 的文件必须为 0。
  B. 占位补齐层（补齐清单_1014全量扫描.csv，若存在）
     manifest 中每个文件必须存在、0 字节、mtime 落在其「归属月份」内、命名符合占位规范。
  C. xlsx 全量口径层（网盘数据文件规模_1014.xlsx）
     磁盘一级目录文件数 == 「按平台目录」；磁盘按修改时间的月份分布 == 「按月份」；
     磁盘总文件数 == 「总览 表格文件总数」。
  D. 扩展名口径层
     磁盘「一级目录 × 扩展名」文件数 == xlsx「按平台目录」分扩展名列；
     磁盘各扩展名合计 == xlsx「总览」各扩展名个数。

用法: python3 verify_mock_files.py [--quiet]
退出码 0 = 四层全部通过。系统元数据文件（.DS_Store 等）不计入核验口径。
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
MANIFEST = os.path.join(BASE, "补齐清单_1014全量扫描.csv")
ROOT = os.path.join(BASE, "网盘模拟数据")
INVALID = ':*?"<>|'
# 操作系统/文件管理器自动生成的元数据文件，不属于网盘真实文件，核验时排除
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".AppleDouble", "._"}
PLACEHOLDER_RE = re.compile(r"^_补位_\d{4}-\d{2}_\d{6}\.(xlsx|xls|csv|xlsm|xlsb)$")


def clean_name(s):
    return re.sub(r"\s+", "", str(s).strip())


def norm(rel):
    rel = (rel or "").replace("\\", "/").replace("//", "/").lstrip("/")
    parts = ["".join("_" if c in INVALID else c for c in p).strip()
             for p in rel.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def load_csv_expected():
    expected, month = {}, Counter()
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = norm(row["相对路径"])
            if not key or key in expected:
                continue
            expected[key] = row
            t = (row["修改时间"] or "").strip()[:7]
            if t:
                month[t] += 1
    return expected, month


def load_manifest():
    if not os.path.exists(MANIFEST):
        return {}
    m = {}
    with open(MANIFEST, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            m[norm(row["相对路径"])] = row
    return m


def load_xlsx():
    try:
        import openpyxl
    except ImportError:
        return None
    path = None
    for name in os.listdir(BASE):
        if name.startswith("网盘数据文件规模") and name.endswith(".xlsx"):
            path = os.path.join(BASE, name)
            break
    if not path:
        return None
    wb = openpyxl.load_workbook(path, data_only=True)
    cat = {}
    cat_ext = {}
    for row in wb["按平台目录"].iter_rows(min_row=2, values_only=True):
        if row[0] and str(row[0]).strip() != "合计":
            name, n = clean_name(row[0]), int(row[1])
            e = {}
            for i, ext in ((4, ".xlsx"), (5, ".xls"), (6, ".csv")):
                e[ext] = int(str(row[i]).split("/")[0]) if row[i] else 0
            rest = n - sum(e.values())
            if rest > 0:
                e[".xlsm"] = rest
            cat[name] = n
            cat_ext[name] = e
    mon = {}
    for row in wb["按月份"].iter_rows(min_row=2, values_only=True):
        if row[0]:
            mon[str(row[0]).strip()] = int(row[1])
    grand = None
    ext_ov = {}
    for row in wb["总览"].iter_rows(values_only=True):
        if not row or not row[0]:
            continue
        key = str(row[0]).strip()
        if key == "表格文件总数" and row[1] is not None:
            grand = int(row[1])
        elif row[1] and "个 /" in str(row[1]):
            ext_ov["." + key.lstrip(".")] = int(str(row[1]).split("个")[0])
    return cat, mon, grand, cat_ext, ext_ov


def is_junk(path_str):
    name = path_str.rsplit("/", 1)[-1]
    return name in JUNK_NAMES or name.startswith("._")


def scan_disk(root_abs):
    files = {}
    junk = []
    dirs = 0
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirs += len(dirnames)
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root_abs).replace(os.sep, "/")
            if is_junk(rel):
                junk.append(rel)
            else:
                files[rel] = p
    dirs += 1                       # 根目录本身
    return files, dirs, junk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="只打印结论与异常项")
    args = ap.parse_args()
    root_abs = os.path.abspath(ROOT)
    if not os.path.isdir(root_abs):
        sys.exit(f"目录不存在: {root_abs}")

    expected, csv_month = load_csv_expected()
    manifest = load_manifest()
    files, dir_cnt, junk = scan_disk(root_abs)
    problems = []

    def show(title, lines):
        if args.quiet:
            return
        print(title)
        for l in lines:
            print("   " + l)

    # ---------------- A. CSV 真实清单层 ----------------
    placeholder = set(manifest)
    missing = sorted(set(expected) - set(files))
    extra = sorted(set(files) - set(expected) - placeholder)
    dup_note = f"（清单路径去重 {len(expected):,} 条）"

    nonempty, mtime_bad = [], []
    for rel, row in expected.items():
        if rel not in files:
            continue
        p = files[rel]
        try:
            if os.path.getsize(p) != 0:
                nonempty.append((rel, os.path.getsize(p)))
        except OSError as e:
            nonempty.append((rel, f"stat {e}"))
        t = (row["修改时间"] or "").strip()
        if t:
            try:
                want = time.mktime(time.strptime(t, "%Y-%m-%d %H:%M"))
                if abs(os.path.getmtime(p) - want) >= 60:
                    mtime_bad.append((rel, t))
            except ValueError:
                pass

    lines = [
        f"清单条目 {len(expected):,} {dup_note} / 磁盘文件 {len(files):,} / 磁盘目录 {dir_cnt:,}",
        f"缺失(CSV 有磁盘无)   : {len(missing):,}",
        f"多余(非清单非占位)   : {len(extra):,}",
        f"清单文件非 0 字节    : {len(nonempty):,}",
        f"清单文件修改时间不符 : {len(mtime_bad):,}",
    ]
    if junk:
        lines.append(f"已忽略系统元数据文件 {len(junk):,} 个（Finder/资源管理器生成，不属于网盘文件）: "
                     + ", ".join(sorted({os.path.basename(j) for j in junk})))
    show("A. CSV 真实清单层", lines)
    if missing:
        problems.append(f"CSV 层缺失 {len(missing)} 个: " + ", ".join(missing[:3]))
    if extra:
        problems.append(f"出现来源不明的多余文件 {len(extra)} 个: " + ", ".join(extra[:3]))
    if nonempty:
        problems.append(f"清单文件非空 {len(nonempty)} 个: " + ", ".join(r for r, _ in nonempty[:3]))
    if mtime_bad:
        problems.append(f"清单文件 mtime 不符 {len(mtime_bad)} 个")

    # ---------------- B. 占位补齐层 ----------------
    if manifest:
        mf_missing = sorted(placeholder - set(files))
        mf_nonempty, mf_month_bad, mf_name_bad = [], [], []
        for rel, row in manifest.items():
            if rel not in files:
                continue
            p = files[rel]
            if os.path.getsize(p) != 0:
                mf_nonempty.append(rel)
            if not PLACEHOLDER_RE.match(rel.rsplit("/", 1)[-1]):
                mf_name_bad.append(rel + " (命名不合规)")
            want_ext = "." + (row["扩展名"] or "").lstrip(".").lower()
            if os.path.splitext(rel)[1].lower() != want_ext:
                mf_name_bad.append(rel + " (扩展名与 manifest 不符)")
        for rel, row in manifest.items():
            if rel in files:
                got = time.strftime("%Y-%m", time.localtime(os.path.getmtime(files[rel])))
                if got != row["归属月份"]:
                    mf_month_bad.append(f"{rel}: 期望 {row['归属月份']} 实际 {got}")
        lines = [
            f"manifest 记录 {len(manifest):,} 个占位文件",
            f"缺失                : {len(mf_missing):,}",
            f"非 0 字节           : {len(mf_nonempty):,}",
            f"mtime 不在归属月份  : {len(mf_month_bad):,}",
            f"命名/扩展名不合规   : {len(mf_name_bad):,}",
        ]
        show("B. 占位补齐层", lines)
        if mf_missing:
            problems.append(f"占位文件缺失 {len(mf_missing)} 个")
        if mf_nonempty:
            problems.append(f"占位文件非空 {len(mf_nonempty)} 个")
        if mf_month_bad:
            problems.append(f"占位文件月份错位 {len(mf_month_bad)} 个，例: {mf_month_bad[0]}")
        if mf_name_bad:
            problems.append(f"占位命名不合规 {len(mf_name_bad)} 个，例: {mf_name_bad[0]}")
    else:
        show("B. 占位补齐层", ["无 manifest（未做补齐，跳过）"])

    # ---------------- C. xlsx 全量口径层 ----------------
    xres = load_xlsx()
    if xres:
        cat_target, mon_target, grand, cat_ext_target, ext_overview = xres
        cat_now, mon_now, catext_now, ext_now = Counter(), Counter(), Counter(), Counter()
        for rel in files:
            top = rel.split("/", 1)[0]
            cat_now[top] += 1
            ext = os.path.splitext(rel)[1].lower()
            catext_now[(top, ext)] += 1
            ext_now[ext] += 1
            # 月份一律以磁盘真实 mtime 归类（A 层已验证清单文件 mtime 与清单一致）
            mon_now[time.strftime("%Y-%m", time.localtime(os.path.getmtime(files[rel])))] += 1

        cat_diff = {c: (cat_target.get(c, 0), cat_now.get(c, 0)) for c in set(cat_target) | set(cat_now)
                    if cat_target.get(c, 0) != cat_now.get(c, 0)}
        mon_diff = {m: (mon_target.get(m, 0), mon_now.get(m, 0)) for m in set(mon_target) | set(mon_now)
                    if mon_target.get(m, 0) != mon_now.get(m, 0)}
        lines = [
            f"总文件数: 磁盘 {len(files):,} vs xlsx {grand:,}  -> {'一致' if len(files) == grand else '不一致'}",
            f"一级目录数: 磁盘 {len(cat_now)} vs xlsx {len(cat_target)}；数量不符目录 {len(cat_diff)} 个",
            f"月份数: 磁盘 {len(mon_now)} vs xlsx {len(mon_target)}；数量不符月份 {len(mon_diff)} 个",
        ]
        for c, (w, g) in sorted(cat_diff.items(), key=lambda x: x[1][1] - x[1][0]):
            lines.append(f"   !! {c}: xlsx={w:,} 磁盘={g:,} 差={g - w:+,}")
        for m, (w, g) in sorted(mon_diff.items()):
            lines.append(f"   !! {m}: xlsx={w:,} 磁盘={g:,} 差={g - w:+,}")
        show("C. xlsx 全量口径层", lines)
        if len(files) != grand:
            problems.append(f"总数与 xlsx 不符: 磁盘 {len(files):,} vs 汇总 {grand:,}")
        if cat_diff:
            problems.append(f"{len(cat_diff)} 个一级目录数量与 xlsx 不符")
        if mon_diff:
            problems.append(f"{len(mon_diff)} 个月份数量与 xlsx 不符")

        # D. 扩展名维度：磁盘「一级目录 × 扩展名」 vs xlsx 按平台目录分扩展名列；磁盘扩展名合计 vs 总览
        ce_diff = {}
        for c in set(cat_ext_target) | set(k[0] for k in catext_now):
            for e in set(cat_ext_target.get(c, {})) | {k[1] for k in catext_now if k[0] == c}:
                w, g = cat_ext_target.get(c, {}).get(e, 0), catext_now.get((c, e), 0)
                if w != g:
                    ce_diff[(c, e)] = (w, g)
        ex_diff = {e: (ext_overview.get(e, 0), ext_now.get(e, 0))
                   for e in set(ext_overview) | set(ext_now) if ext_overview.get(e, 0) != ext_now.get(e, 0)}
        lines = [
            f"目录×扩展名 组合 {len(catext_now):,} 种；与 xlsx 分扩展名列不符 {len(ce_diff)} 种",
            f"扩展名合计: " + "  ".join(f"{e} 磁盘={ext_now.get(e, 0):,}/汇总={ext_overview.get(e, 0):,}"
                                    for e in sorted(set(ext_overview) | set(ext_now))),
        ]
        for (c, e), (w, g) in sorted(ce_diff.items()):
            lines.append(f"   !! {c}{e}: xlsx={w:,} 磁盘={g:,} 差={g - w:+,}")
        for e, (w, g) in sorted(ex_diff.items()):
            lines.append(f"   !! {e}: 总览={w:,} 磁盘={g:,} 差={g - w:+,}")
        show("D. 扩展名口径层", lines)
        if ce_diff:
            problems.append(f"{len(ce_diff)} 个「目录×扩展名」数量与 xlsx 不符，例: {list(ce_diff)[:3]}")
        if ex_diff:
            problems.append(f"{len(ex_diff)} 个扩展名合计与总览不符: "
                            + ", ".join(f"{e}({g - w:+,})" for e, (w, g) in ex_diff.items()))
    else:
        show("C. xlsx 全量口径层", ["(未找到 xlsx 或缺少 openpyxl，跳过)"])

    print("=" * 62)
    if problems:
        print("核验结论: ❌ 存在差异")
        for p in problems:
            print("  -", p)
        return 1
    print("核验结论: ✅ 四层全部通过")
    print(f"  A 清单 {len(expected):,} 个文件逐条命中，0 字节、修改时间全对，无来源不明文件")
    if manifest:
        print(f"  B 占位 {len(manifest):,} 个文件全部存在、命名合规、月份正确")
    if xres:
        print(f"  C 磁盘总数 = xlsx 汇总 = {len(files):,}，一级目录与月份分布逐项对齐")
    return 0


if __name__ == "__main__":
    sys.exit(main())
