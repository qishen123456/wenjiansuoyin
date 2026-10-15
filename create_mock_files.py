#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
根据《文件清单_含行数.csv》中的「相对路径」，在本地模拟创建一整棵网盘目录树，
所有文件都创建为 0 字节空文件（只复刻目录结构 + 文件名 + 扩展名 + 后缀）。

用法:
    python3 create_mock_files.py                      # 默认输出到 ./网盘模拟数据
    python3 create_mock_files.py --root /tmp/网盘模拟数据
    python3 create_mock_files.py --workers 8          # 并发创建，加快速度
    python3 create_mock_files.py --dry-run            # 只统计不创建

可选: --keep-time 按 CSV 的「修改时间」把空文件的 mtime 也设置成清单里的时间，
      这样目录树在按修改时间排序/筛选时表现和真实网盘一致。
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

CSV_NAME = "文件清单_含行数.csv"
PATH_COL = "相对路径"          # 形如: 1-阿里平台-自营\测试\xxx.xlsx
NAME_COL = "文件名"
MTIME_COL = "修改时间"          # 形如: 2025-12-23 13:41
INVALID_CHARS = ':*?"<>|'      # Windows 非法字符, 在 macOS/Linux 上做一次替换兜底


def sanitize_segment(seg: str) -> str:
    """把 Windows 非法字符替换成 _，避免在 macOS/Linux 上出现怪异文件名。"""
    if not seg:
        return "_"
    cleaned = "".join("_" if ch in INVALID_CHARS else ch for ch in seg)
    # macOS 历史上用 ':' 作路径分隔，Finder 会显示为 '/'，这里统一兜底
    cleaned = cleaned.strip()
    if cleaned in (".", ".."):
        cleaned = "_" + cleaned
    return cleaned


def parse_mtime(text: str):
    """CSV 里的修改时间 -> epoch 秒；解析失败返回 None。"""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return time.mktime(time.strptime(text.strip(), fmt))
        except ValueError:
            continue
    return None


def load_rows(csv_path: str):
    """读取 CSV，返回去重后的 [(相对路径, 文件名, 修改时间字符串)]。"""
    seen = set()
    rows = []
    skipped = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if PATH_COL not in (reader.fieldnames or []):
            sys.exit(f"CSV 里找不到列「{PATH_COL}」，实际列: {reader.fieldnames}")
        for row in reader:
            rel = (row.get(PATH_COL) or "").strip()
            if not rel:
                skipped += 1
                continue
            # 统一分隔符，去掉可能的前导盘符/共享根斜杠
            rel = rel.replace("\\", "/").replace("//", "/").lstrip("/")
            parts = [sanitize_segment(p) for p in rel.split("/") if p not in ("", ".", "..")]
            if not parts:
                skipped += 1
                continue
            key = "/".join(parts)
            if key in seen:      # 同名文件重复出现只建一次
                continue
            seen.add(key)
            rows.append((key, row.get(NAME_COL) or parts[-1], row.get(MTIME_COL) or ""))
    return rows, skipped


def create_one(root: str, rel: str, mtime_text: str, keep_time: bool):
    """创建一个空文件（父目录一并创建）。返回 (rel, ok, 错误信息)。"""
    target = os.path.join(root, rel)
    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 已存在则不覆盖内容，仅确保是文件
        if not os.path.exists(target):
            with open(target, "wb"):
                pass
        if keep_time:
            ts = parse_mtime(mtime_text)
            if ts is not None:
                os.utime(target, (ts, ts))
        return rel, True, ""
    except OSError as e:
        return rel, False, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser(description="按 CSV 清单模拟创建空文件目录树")
    ap.add_argument("--csv", default=CSV_NAME, help=f"清单文件路径，默认 {CSV_NAME}")
    ap.add_argument("--root", default="网盘模拟数据", help="输出根目录，默认 ./网盘模拟数据")
    ap.add_argument("--workers", type=int, default=8, help="并发线程数，默认 8")
    ap.add_argument("--limit", type=int, default=0, help="只创建前 N 个文件（0=全部），用于试跑")
    ap.add_argument("--keep-time", action="store_true", help="按 CSV 修改时间设置文件 mtime")
    ap.add_argument("--dry-run", action="store_true", help="只统计目录/文件数量，不真正创建")
    args = ap.parse_args()

    csv_path = args.csv if os.path.isabs(args.csv) else os.path.join(os.path.dirname(os.path.abspath(__file__)), args.csv)
    root = os.path.abspath(args.root)
    if not os.path.exists(csv_path):
        sys.exit(f"找不到清单文件: {csv_path}")

    t0 = time.time()
    rows, skipped = load_rows(csv_path)
    if args.limit:
        rows = rows[: args.limit]
    dirs = {os.path.dirname(rel) for rel, _, _ in rows if os.path.dirname(rel)}
    print(f"清单解析完成: 文件 {len(rows):,} 个, 目录 {len(dirs):,} 个, 跳过无效行 {skipped:,} 个 (耗时 {time.time()-t0:.1f}s)")

    if args.dry_run:
        depth = max(rel.count("/") for rel, _, _ in rows)
        print(f"[dry-run] 最大层级深度 {depth + 1}，未创建任何文件")
        for rel, _, _ in rows[:5]:
            print("   ", rel)
        return

    os.makedirs(root, exist_ok=True)
    fail_log = os.path.join(os.path.dirname(csv_path), "create_mock_files_failed.log")

    total = len(rows)
    done = 0
    ok_cnt = 0
    fails = []
    start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        it = ex.map(lambda row: create_one(root, row[0], row[2], args.keep_time), rows)
        for rel, ok, err in it:
            done += 1
            if ok:
                ok_cnt += 1
            else:
                fails.append((rel, err))
            if done % 5000 == 0 or done == total:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                print(f"  进度 {done:,}/{total:,} ({done/total*100:5.1f}%)  "
                      f"速率 {rate:,.0f} 文件/秒  剩余 {eta:,.0f}s  失败 {len(fails):,}")

    with open(fail_log, "w", encoding="utf-8") as f:
        for rel, err in fails:
            f.write(f"{rel}\t{err}\n")

    print("-" * 60)
    print(f"完成: 成功创建 {ok_cnt:,} 个空文件, 失败 {len(fails):,} 个, 总耗时 {time.time()-start:.1f}s")
    print(f"输出根目录: {root}")
    if fails:
        print(f"失败明细: {fail_log}")


if __name__ == "__main__":
    main()
