# -*- coding: utf-8 -*-
"""测试各模型服务商及 DeepSeek-V4.1-Flash 等模型的可用性与时延。"""

import os
import time
import json
import urllib.request
import urllib.error

MODELS_TO_TEST = [
    # 1. 魔搭 ModelScope
    {
        "provider": "魔搭 ModelScope",
        "url": "https://api-inference.modelscope.cn/v1/chat/completions",
        "key": os.environ.get("MODELSCOPE_KEY_LIN", ""),
        "key_owner": "琳",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    },
    {
        "provider": "魔搭 ModelScope",
        "url": "https://api-inference.modelscope.cn/v1/chat/completions",
        "key": os.environ.get("MODELSCOPE_KEY_QI", ""),
        "key_owner": "琪",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    },
    {
        "provider": "魔搭 ModelScope",
        "url": "https://api-inference.modelscope.cn/v1/chat/completions",
        "key": os.environ.get("MODELSCOPE_KEY_QI_BACKUP", ""),
        "key_owner": "琪(备用)",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    },
    {
        "provider": "魔搭 ModelScope",
        "url": "https://api-inference.modelscope.cn/v1/chat/completions",
        "key": os.environ.get("MODELSCOPE_KEY_QI", ""),
        "key_owner": "琪",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash"
    },
    {
        "provider": "魔搭 ModelScope",
        "url": "https://api-inference.modelscope.cn/v1/chat/completions",
        "key": os.environ.get("MODELSCOPE_KEY_QI", ""),
        "key_owner": "琪",
        "model": "deepseek-ai/DeepSeek-V4-Pro"
    },
    # 2. 白山云 BaishanCloud
    {
        "provider": "白山云",
        "url": "https://api.edgefn.net/v1/chat/completions",
        "key": os.environ.get("BAISHAN_KEY_LIN", ""),
        "key_owner": "琳",
        "model": "DeepSeek-V3.2"
    },
    {
        "provider": "白山云",
        "url": "https://api.edgefn.net/v1/chat/completions",
        "key": os.environ.get("BAISHAN_KEY_QI", ""),
        "key_owner": "琪",
        "model": "DeepSeek-V3.2"
    },
    {
        "provider": "白山云",
        "url": "https://api.edgefn.net/v1/chat/completions",
        "key": os.environ.get("BAISHAN_KEY_QI", ""),
        "key_owner": "琪",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash"
    },
    {
        "provider": "白山云",
        "url": "https://api.edgefn.net/v1/chat/completions",
        "key": os.environ.get("BAISHAN_KEY_QI", ""),
        "key_owner": "琪",
        "model": "MiniMax-M2.5"
    },
    {
        "provider": "白山云",
        "url": "https://api.edgefn.net/v1/chat/completions",
        "key": os.environ.get("BAISHAN_KEY_QI", ""),
        "key_owner": "琪",
        "model": "GLM-5"
    },
    # 3. 硅基流动 SiliconFlow
    {
        "provider": "硅基流动",
        "url": "https://api.siliconflow.cn/v1/chat/completions",
        "key": os.environ.get("SILICONFLOW_KEY", ""),
        "key_owner": "琪",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash"
    },
    {
        "provider": "硅基流动",
        "url": "https://api.siliconflow.cn/v1/chat/completions",
        "key": os.environ.get("SILICONFLOW_KEY", ""),
        "key_owner": "琪",
        "model": "deepseek-ai/DeepSeek-V3"
    },
    {
        "provider": "硅基流动",
        "url": "https://api.siliconflow.cn/v1/chat/completions",
        "key": os.environ.get("SILICONFLOW_KEY", ""),
        "key_owner": "琪",
        "model": "Qwen/Qwen2.5-72B-Instruct"
    },
    # 4. 阿里百炼
    {
        "provider": "阿里百炼",
        "url": "https://ws-0q2src4w24p2a2bi.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
        "key": os.environ.get("BAILIAN_KEY", ""),
        "key_owner": "琪",
        "model": "qwen3.5-122b-a10b"
    }
]

def test_single_model(cfg):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['key']}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    }
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "你是一个智能企业网盘文件检索助手。"},
            {"role": "user", "content": "你好，请用一句话告诉我你的名字和你的核心优势。"}
        ],
        "max_tokens": 100,
        "temperature": 0.2
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=data, headers=headers)
    start_t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            elapsed_ms = (time.perf_counter() - start_t) * 1000
            status_code = resp.status
            body = resp.read().decode("utf-8")
            parsed = json.loads(body)
            reply = parsed.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            return {
                "ok": True,
                "status_code": status_code,
                "elapsed_ms": round(elapsed_ms, 1),
                "reply": reply,
                "error": None
            }
    except urllib.error.HTTPError as e:
        elapsed_ms = (time.perf_counter() - start_t) * 1000
        err_body = e.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status_code": e.code,
            "elapsed_ms": round(elapsed_ms, 1),
            "reply": None,
            "error": f"HTTP {e.code}: {err_body[:300]}"
        }
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start_t) * 1000
        return {
            "ok": False,
            "status_code": 0,
            "elapsed_ms": round(elapsed_ms, 1),
            "reply": None,
            "error": str(e)
        }

def main():
    print("=" * 80)
    print("开始对各类模型服务商进行连通性、响应时延及输出质量实测...")
    print("=" * 80)
    results = []
    for item in MODELS_TO_TEST:
        if not item["key"]:
            print(f"⏭️ 跳过 [{item['provider']}] {item['model']}（未配置 Key 环境变量）")
            continue
        tag = f"[{item['provider']}] {item['model']} (Key归属: {item['key_owner']})"
        print(f"\n👉 正在测试: {tag} ...")
        res = test_single_model(item)
        item_res = {**item, **res}
        results.append(item_res)
        if res["ok"]:
            print(f"   ✅ [成功] 状态码: {res['status_code']} | 耗时: {res['elapsed_ms']}ms")
            print(f"   💬 回复: {res['reply'][:150]}")
        else:
            print(f"   ❌ [失败] 状态码: {res['status_code']} | 耗时: {res['elapsed_ms']}ms")
            print(f"   ⚠️ 错误信息: {res['error']}")

    with open("tests/model_test_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 80)
    print("全部测试完毕，结果已持久化保存至 tests/model_test_results.json")

if __name__ == "__main__":
    main()
