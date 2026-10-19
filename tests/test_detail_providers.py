# -*- coding: utf-8 -*-
"""深度测试魔搭 ModelScope (去 ms- 前缀与不去) 和白山云的调用。"""

import time
import json
import urllib.request
import urllib.error

def test_modelscope_tokens():
    keys = [
        "ms-e0157e57-a690-48a0-a07b-603b2c3a43cb",
        "e0157e57-a690-48a0-a07b-603b2c3a43cb",
        "ms-6e1945bb-484c-4be1-8002-a4b7e34f4f22",
        "6e1945bb-484c-4be1-8002-a4b7e34f4f22",
        "ms-28537548-91b5-4ccc-81b3-04734aed2bd4",
        "28537548-91b5-4ccc-81b3-04734aed2bd4",
    ]
    models = [
        "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        "deepseek-ai/DeepSeek-V4.1-Flash",
        "deepseek-ai/DeepSeek-V3",
        "Qwen/Qwen2.5-72B-Instruct"
    ]
    url = "https://api-inference.modelscope.cn/v1/chat/completions"

    print("--- 开始测试 ModelScope ---")
    for key in keys:
        for model in models:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}"
            }
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    res_body = resp.read().decode("utf-8")
                    print(f"✅ ModelScope 成功! key={key[:10]}... model={model}")
                    print(f"   返回: {res_body[:200]}")
                    return key, model
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", errors="replace")
                print(f"❌ key={key[:10]}... model={model} HTTP {e.code}: {err[:150]}")
            except Exception as e:
                print(f"❌ key={key[:10]}... model={model} 异常: {e}")
    return None, None

def test_baishan_headers():
    url = "https://api.edgefn.net/v1/chat/completions"
    keys = [
        "sk-VBa1db6zidFzfstwC275Ea5909F342F5B41a0603DdAd7553",
        "sk-zcQLM6VEmMzYjwtMC61673D62d87441b9aEe28F00fDf3c54"
    ]
    models = ["DeepSeek-V3.2", "deepseek-ai/DeepSeek-V4.1-Flash", "MiniMax-M2.5", "GLM-5"]
    print("\n--- 开始测试白山云 ---")
    for key in keys:
        for model in models:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "curl/7.88.1"
            }
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    res_body = resp.read().decode("utf-8")
                    print(f"✅ 白山云成功! key={key[:10]}... model={model}")
                    print(f"   返回: {res_body[:200]}")
                    return key, model
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", errors="replace")
                print(f"❌ 白山云 key={key[:10]}... model={model} HTTP {e.code}: {err[:150]}")
            except Exception as e:
                print(f"❌ 白山云 key={key[:10]}... model={model} 异常: {e}")

if __name__ == "__main__":
    test_modelscope_tokens()
    test_baishan_headers()
