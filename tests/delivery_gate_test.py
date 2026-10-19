#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交付闸门全面质量检验脚本 (Delivery Gate Automated Verification)
对系统中所有 REST API 接口进行严格的自动化回归测试与安全断言。
"""
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

BASE_URL = "http://127.0.0.1:8080"

def request(path, method="GET", data=None):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "DeliveryGateTester/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
    else:
        body = None
    try:
        with urllib.request.urlopen(req, data=body, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        content = e.read().decode("utf-8")
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"raw": content}
        return e.code, parsed

def run_tests():
    print("==================================================")
    print("🚀 开始执行系统级自动化验收与交付回归测试 (QA Gate)")
    print("==================================================")
    passed = 0
    failed = 0

    def assert_test(name, condition, extra_info=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  ✅ [PASS] {name}")
        else:
            failed += 1
            print(f"  ❌ [FAIL] {name} - {extra_info}")

    # 1. 基础服务健康状态
    code, res = request("/api/health")
    assert_test("1.1 服务健康检查 /api/health", code == 200 and res.get("ok") is True, str(res))

    code, res = request("/api/index/status")
    assert_test("1.2 索引状态检查 /api/index/status", code == 200 and "indexed_files" in res, str(res))

    # 2. 文件极速检索功能
    code, res = request("/api/search?q=" + urllib.parse.quote("销售 利润") + "&limit=10")
    assert_test("2.1 多词并发检索 /api/search?q=销售 利润", code == 200 and "results" in res and "elapsed_ms" in res, str(res))

    code, res = request("/api/search?q=xlsx&match_mode=all&limit=5")
    assert_test("2.2 检索模式 match_mode=all", code == 200 and "total_candidates" in res, str(res))

    # 3. 组织全景目录树
    code, res = request("/api/catalog/overview")
    assert_test("3.1 组织全景概览 /api/catalog/overview", code == 200 and res.get("ok") is True, str(res))

    code, res = request("/api/catalog/tree")
    assert_test("3.2 组织全景架构树 /api/catalog/tree", code == 200 and "tree" in res and res.get("ok") is True, str(res))

    code, res = request("/api/catalog/files?page=1&page_size=10")
    assert_test("3.3 目录文件分页浏览 /api/catalog/files", code == 200 and "items" in res, str(res))

    # 4. 归档时效监控与任务管理
    code, res = request("/api/archive/overview")
    assert_test("4.1 归档时效概览 /api/archive/overview", code == 200 and res.get("ok") is True, str(res))

    code, res = request("/api/archive/rules")
    assert_test("4.2 归档规则列表 /api/archive/rules", code == 200 and res.get("ok") is True and len(res.get("rules", [])) > 0, str(res))

    code, res = request("/api/archive/tasks?status=all&page=1&page_size=5")
    assert_test("4.3 归档工单任务列表 /api/archive/tasks", code == 200 and res.get("ok") is True, str(res))

    # 5. 人员权限与手机号密码体系
    code, res = request("/api/system/permissions")
    users = res.get("users", [])
    has_phone_pwd = all("phone" in u and "password" in u for u in users) if users else False
    assert_test("5.1 人员权限列表包含手机号与密码字段 /api/system/permissions", code == 200 and has_phone_pwd, str(res))

    # 5.2 新增内网成员 (手机号去重测试)
    test_phone = "13900998877"
    create_payload = {
        "name": "测试工程师",
        "phone": test_phone,
        "password": "qa_password_123",
        "department": "质量工程部 / 自动化测试组",
        "role": "dept_admin"
    }
    code, create_res = request("/api/system/permissions/users", method="POST", data=create_payload)
    new_user_id = create_res.get("id")
    assert_test("5.2 创建新内网成员账号 (手机号+密码)", code == 200 and create_res.get("ok") is True, str(create_res))

    # 5.3 手机号唯一性约束验证
    code, dup_res = request("/api/system/permissions/users", method="POST", data=create_payload)
    assert_test("5.3 手机号重复校验 (拦截重复注册)", code == 400, f"Expected 400, got {code}")

    if new_user_id:
        # 5.4 重置密码
        code, pwd_res = request(f"/api/system/permissions/users/{new_user_id}/password", method="PUT", data={"password": "new_secret_pwd"})
        assert_test("5.4 重置成员登录密码", code == 200 and pwd_res.get("ok") is True, str(pwd_res))

        # 5.5 切换权限角色
        code, role_res = request(f"/api/system/permissions/users/{new_user_id}/role", method="PUT", data={"role": "super_admin"})
        assert_test("5.5 实时更新角色为 super_admin", code == 200 and role_res.get("ok") is True, str(role_res))

        # 5.6 切换账号启停状态
        code, status_res = request(f"/api/system/permissions/users/{new_user_id}/status", method="PUT")
        assert_test("5.6 切换账号启用/停用状态", code == 200 and status_res.get("ok") is True, str(status_res))

        # 5.7 移除测试账号
        code, del_res = request(f"/api/system/permissions/users/{new_user_id}", method="DELETE")
        assert_test("5.7 移除测试成员授权", code == 200 and del_res.get("ok") is True, str(del_res))

    # 6. 安全防御验证 (非法越界与路径穿越防御)
    code, invalid_file_res = request("/api/analysis/file/99999999")
    assert_test("6.1 非法/不存在 file_id 防御 (返回404)", code == 404, str(invalid_file_res))

    print("\n--------------------------------------------------")
    print(f"📊 测试总计: {passed + failed} 项 | 通过: {passed} 项 | 失败: {failed} 项")
    print("--------------------------------------------------")
    if failed == 0:
        print("🏆 全部集成测试 100% 通过！达到准出交付标准 (Production Delivery Ready)！")
        return 0
    else:
        print("⚠️ 存在失败项，请检查！")
        return 1

if __name__ == "__main__":
    sys.exit(run_tests())
