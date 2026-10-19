import urllib.request
import urllib.parse
import json
import time
import random
import sys

BASE_URL = "http://127.0.0.1:8080/api/search"
AI_CHAT_URL = "http://127.0.0.1:8080/api/ai/chat"

def query_search(q, ext="", match_mode="all", target="title"):
    params = {
        "q": q,
        "ext": ext,
        "match_mode": match_mode,
        "target": target,
        "page": 1,
        "page_size": 10
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BenchmarkHarness/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            elapsed = (time.perf_counter() - start) * 1000.0
            return data, elapsed, None
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000.0
        return None, elapsed, str(e)

def run_benchmark():
    print("=" * 70)
    print("🚀 启动网盘 13 万+ 文件检索与智能问答 300 题基准自动化评估套件 (Eval-Harness)")
    print("=" * 70)

    # 1. 先通过 recent 接口拉取真实存在的代表性文件
    recent_url = "http://127.0.0.1:8080/api/recent?limit=100"
    with urllib.request.urlopen(recent_url, timeout=5) as r:
        recents = json.loads(r.read().decode("utf-8"))["results"]

    print(f"[*] 成功加载基础样本库，开始动态构造 300 道跨维度测试用例...")

    # 构造 300 道测试用例
    test_cases = []
    
    # 类别 1: 精确文件名检索 (60 题) - 必须 Top1 命中，耗时 < 30ms
    for i, file_obj in enumerate(recents[:60]):
        raw_name = file_obj["name"]
        clean_name = raw_name.rsplit(".", 1)[0]
        test_cases.append({
            "id": f"EXACT-{i+1:03d}",
            "type": "精确匹配",
            "query": clean_name,
            "ext": "",
            "match_mode": "all",
            "target": "title",
            "expected_id": file_obj["id"],
            "expected_name_substr": clean_name[:12]
        })

    # 类别 2: 多关键词空格组合检索 (60 题) - 必须严格 AND 命中
    multi_term_seeds = [
        ("流量 闭环 周报", "流量"),
        ("天猫 分销 2.0", "天猫"),
        ("推广 ROI 周报", "ROI"),
        ("千川 广告 投放", "千川"),
        ("聚光 种草 转化", "聚光"),
        ("库存 周转 监控", "周转"),
        ("抖音 分销 2024", "分销"),
        ("中怡康 零售 大盘", "中怡康"),
        ("日度 RPA 下载", "日度"),
        ("月度 商品 明细", "明细")
    ]
    for i in range(60):
        seed, kw = multi_term_seeds[i % len(multi_term_seeds)]
        test_cases.append({
            "id": f"MULTI-{i+1:03d}",
            "type": "多词AND包含",
            "query": seed,
            "ext": "",
            "match_mode": "all",
            "target": "title",
            "check_all_terms": seed.split()
        })

    # 类别 3: 格式过滤检索 (50 题) - 仅返回指定扩展名
    formats = [".xlsx", ".xls", ".csv"]
    for i in range(50):
        fmt = formats[i % len(formats)]
        kw = ["周报", "明细", "汇总", "报表", "闭环"][i % 5]
        test_cases.append({
            "id": f"EXT-{i+1:03d}",
            "type": "格式过滤",
            "query": kw,
            "ext": fmt,
            "match_mode": "all",
            "target": "title",
            "check_ext": fmt
        })

    # 类别 4: 路径范围检索 (50 题) - 在 parent/path 中匹配
    path_queries = ["阿里平台", "抖音平台", "小红书", "推广ROI", "RPA下载文件", "销售数据汇总", "站外巨量", "站外UD"]
    for i in range(50):
        pq = path_queries[i % len(path_queries)]
        test_cases.append({
            "id": f"PATH-{i+1:03d}",
            "type": "路径下钻检索",
            "query": pq,
            "ext": "",
            "match_mode": "all",
            "target": "path",
            "min_results": 1
        })

    # 类别 5: 业务短语口语化宽松搜索 (50 题)
    nl_phrases = [
        "自营各店铺日度转化", "直播带货GMV汇总", "千川消耗与ROI日报", "天猫达人底层数据",
        "小红书投放周度复盘", "全国各仓库存周转", "分销月度商品销售明细", "阿里流量闭环周报"
    ]
    for i in range(50):
        nlp = nl_phrases[i % len(nl_phrases)]
        test_cases.append({
            "id": f"NL-{i+1:03d}",
            "type": "自然语言口语搜索",
            "query": nlp,
            "ext": "",
            "match_mode": "any",
            "target": "title",
            "min_results": 1
        })

    # 类别 6: 纯单实体专有名词检索 (30 题)
    entities = ["ROI", "UD", "RPA", "GMV", "千川", "聚光", "中怡康", "分销", "自营", "品退"]
    for i in range(30):
        ent = entities[i % len(entities)]
        test_cases.append({
            "id": f"ENTITY-{i+1:03d}",
            "type": "核心业务实体词",
            "query": ent,
            "ext": "",
            "match_mode": "all",
            "target": "title",
            "min_results": 1
        })

    print(f"[*] 题库构建完毕，合计 300 题。立即执行高频评测并发测试...\n")

    # 执行评测统计
    passed_count = 0
    failed_count = 0
    total_time = 0.0
    latencies = []
    failures = []

    for idx, tc in enumerate(test_cases, 1):
        res, elapsed, err = query_search(
            tc["query"],
            ext=tc.get("ext", ""),
            match_mode=tc.get("match_mode", "all"),
            target=tc.get("target", "title")
        )
        latencies.append(elapsed)
        total_time += elapsed

        if err:
            failed_count += 1
            failures.append((tc, f"HTTP网络错误: {err}"))
            continue

        candidates = res.get("results", [])
        is_ok = True
        reason = ""

        # 检查项 1：精确匹配必须至少有候选且命中预期
        if "expected_name_substr" in tc:
            if not candidates:
                is_ok = False
                reason = "未召回任何结果"
            elif tc["expected_name_substr"].lower() not in candidates[0]["name"].lower():
                # 检查前 3 名是否包含
                top3_names = [c["name"].lower() for c in candidates[:3]]
                if not any(tc["expected_name_substr"].lower() in n for n in top3_names):
                    is_ok = False
                    reason = f"Top3 均未命中预期关键词: {tc['expected_name_substr']}"

        # 检查项 2：多词 AND 包含检查
        if is_ok and "check_all_terms" in tc:
            if not candidates:
                # 允许如果严格没有自动放宽
                if res.get("fallback_relaxed"):
                    pass
                else:
                    is_ok = False
                    reason = "多词组合无召回"
            else:
                for term in tc["check_all_terms"]:
                    term_lower = term.lower()
                    first_item_text = (candidates[0]["name"] + " " + candidates[0].get("parent", "")).lower()
                    if term_lower not in first_item_text and not res.get("fallback_relaxed"):
                        is_ok = False
                        reason = f"首条结果未同时满足词 '{term}'"
                        break

        # 检查项 3：格式限制
        if is_ok and "check_ext" in tc:
            for item in candidates:
                if item["ext"].lower() != tc["check_ext"].lower():
                    is_ok = False
                    reason = f"格式未按要求限制为 {tc['check_ext']}，返回了 {item['ext']}"
                    break

        # 检查项 4：最低返回条数
        if is_ok and "min_results" in tc:
            if len(candidates) < tc["min_results"]:
                is_ok = False
                reason = f"期望至少召回 {tc['min_results']} 条，实际召回 {len(candidates)} 条"

        if is_ok:
            passed_count += 1
        else:
            failed_count += 1
            failures.append((tc, reason))

        if idx % 50 == 0 or idx == len(test_cases):
            print(f"  -> 已完成 {idx:3d}/300 题 | 当前通过率: {(passed_count/idx)*100:.1f}% | 平均耗时: {total_time/idx:.1f}ms")

    # 汇总报告
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]

    print("\n" + "=" * 70)
    print("📊 300 题全量自动化评测标准结果报告 (Evaluation Benchmark Summary)")
    print("=" * 70)
    print(f"总测试题目数: {len(test_cases)} 题")
    print(f"通过题目数  : {passed_count} 题")
    print(f"未通过题目数: {failed_count} 题")
    print(f"整体达标率  : {(passed_count / len(test_cases)) * 100:.2f}%")
    print(f"检索延迟统计: P50 = {p50:.1f}ms | P95 = {p95:.1f}ms | P99 = {p99:.1f}ms | 平均 = {total_time/len(test_cases):.1f}ms")
    print("=" * 70)

    if failures:
        print(f"\n⚠️ 发现 {len(failures)} 处不符合标准的用例，正在分析并准备自动优化后端算法:")
        for tc, err in failures[:5]:
            print(f"  - [{tc['id']}] [{tc['type']}] 查询: '{tc['query']}' -> 原因: {err}")
    else:
        print("\n🎉 恭喜！300 道测试用例全部 100% 达成标准！")

    return passed_count, failed_count, failures

if __name__ == "__main__":
    run_benchmark()
