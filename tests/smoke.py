"""冒烟测试 — 上线前快速检查核心链路是否可用。
用法: python tests/smoke.py
"""

import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 设置测试环境
os.environ.setdefault("DB_DRIVER", "sqlite")
os.environ.setdefault("DB_HOST", "")
os.environ.setdefault("DB_NAME", "")
os.environ.setdefault("DB_USER", "")
os.environ.setdefault("DB_PASS", "")
os.environ.setdefault("SECRET_KEY", "smoke-test-key")
os.environ.setdefault("HARBOR_REGISTRY", "test.local")
os.environ.setdefault("HARBOR_USER", "admin")
os.environ.setdefault("HARBOR_PASSWORD", "test")


def _setup_db():
    """创建最小测试数据库"""
    import tempfile
    import sqlite3
    import bcrypt

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="cd_smoke_")
    os.close(fd)
    os.environ["DB_PATH"] = db_path

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # admin_users
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username VARCHAR(255) UNIQUE,
        password_hash VARCHAR(255),
        role VARCHAR(32) DEFAULT 'admin',
        systems TEXT DEFAULT ''
    )""")
    pwd = bcrypt.hashpw("admin123".encode(), bcrypt.gensalt()).decode()
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role, systems) VALUES (?, ?, ?, ?)",
        ("admin", pwd, "admin", "cd"),
    )

    # ci_pipeline_tags
    conn.execute("""CREATE TABLE IF NOT EXISTS ci_pipeline_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project VARCHAR(255),
        tag VARCHAR(255),
        created_at TEXT
    )""")

    conn.commit()
    conn.close()

    from backend.config import settings
    from backend.database import Database
    import backend.auth as auth_mod

    settings.db_path = db_path
    settings.db_driver = "sqlite"
    Database._tables_ensured = False
    Database._pool = None
    auth_mod._systems_col_ok = True

    return db_path


def run_smoke():
    from fastapi.testclient import TestClient
    from main import app

    app.router.on_startup = []  # 跳过 startup
    client = TestClient(app)

    results = []
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        status = "PASS" if condition else "FAIL"
        if condition:
            passed += 1
        else:
            failed += 1
        results.append({"name": name, "status": status, "detail": detail})
        print(f"  [{status}] {name}")

    print("=" * 60)
    print("冒烟测试 — v1.2.2 架构优化")
    print("=" * 60)
    print()

    # ── 1. 核心模块导入 ──
    print("1. 核心模块导入")
    try:
        import backend.exceptions
        import backend.responses
        import backend.models
        import backend.database
        check("exceptions 模块导入", True)
        check("responses 模块导入", True)
        check("models 模块导入", True)
        check("database 模块导入", True)
    except Exception as e:
        check("核心模块导入", False, str(e))
        print(f"\n失败，无法继续: {e}")
        return 1

    # ── 2. 异常类 ──
    print("\n2. 异常类状态码")
    from backend.exceptions import (
        NotFoundError, ConflictError, ValidationError,
        ServiceUnavailableError, DatabaseError, AppException,
    )
    check("NotFoundError → 404", NotFoundError().status_code == 404)
    check("ConflictError → 409", ConflictError().status_code == 409)
    check("ValidationError → 400", ValidationError().status_code == 400)
    check("ServiceUnavailableError → 503", ServiceUnavailableError().status_code == 503)
    check("DatabaseError → 500", DatabaseError().status_code == 500)
    check("AppException 自定义", AppException("test", status_code=401).status_code == 401)

    # ── 3. 响应格式 ──
    print("\n3. 响应格式")
    from backend.responses import ok, items, error
    r = ok(message="success")
    check("ok() success=True", r.get("success") is True)
    check("ok() 包含 message", r.get("message") == "success")
    check("ok(data) 包含 data", ok(data={"x": 1}).get("data") == {"x": 1})
    check("items() 分页正确", items([{"a": 1}], total=10)["total_pages"] == 1)
    check("error() 格式", error("错了") == {"success": False, "error": "错了", "code": 400})

    # ── 4. Pydantic 模型 ──
    print("\n4. Pydantic 模型")
    from backend.models import LoginRequest, BotRequest, ServerRequest, BuildTriggerRequest
    check("LoginRequest", LoginRequest(user="a", password="b").user == "a")
    check("BotRequest 默认值", BotRequest(name="b", webhook_url="x").type == "custom")
    check("ServerRequest 默认值", ServerRequest(name="s", host="h").port == 22)
    check("BuildTriggerRequest 导出", BuildTriggerRequest(ref="main").ref == "main")
    check("BuildTriggerRequest 默认值", BuildTriggerRequest().variables == {})

    # ── 5. App 启动 ──
    print("\n5. App 启动")
    check("App 创建成功", app.title == "Devops-Glue CD")
    check("version 字段", app.version is not None)
    check("异常处理器注册", AppException in app.exception_handlers)

    # ── 6. 健康检查 ──
    print("\n6. 健康检查")
    r = client.get("/health")
    check("GET /health → 200", r.status_code == 200)
    check("status: ok", r.json().get("status") == "ok")

    # ── 7. 鉴权 ──
    print("\n7. 鉴权")
    r = client.get("/api/bots")
    check("无 Token → 401", r.status_code == 401)

    r = client.post("/api/login", json={"user": "admin", "password": "admin123"})
    check("登录成功 → 200", r.status_code == 200)
    token = r.json().get("token")
    check("拿到 Token", bool(token))

    if token:
        headers = {"Authorization": f"Bearer {token}"}
        r = client.get("/api/bots", headers=headers)
        check("有效 Token → 200", r.status_code == 200)
        check("返回数组", isinstance(r.json(), list))

    # ── 8. SPA catch-all ──
    print("\n8. SPA catch-all")
    r = client.get("/some/vue/route")
    check("Vue Route → 200 HTML", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""))

    # ── 9. 异常处理器 ──
    print("\n9. 异常处理器")
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    test_app = FastAPI()
    @test_app.exception_handler(AppException)
    async def h(request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "error": exc.message, "code": exc.status_code},
        )
    @test_app.get("/err")
    def err():
        raise NotFoundError("test 404")
    tc = TestClient(test_app)
    r = tc.get("/err")
    check("异常 → JSON 格式", "application/json" in r.headers.get("content-type", ""))
    check("异常 → success:false", r.json().get("success") is False)
    check("异常 → 404", r.status_code == 404)

    # ── 总结 ──
    print()
    print("=" * 60)
    total = passed + failed
    print(f"结果: {passed}/{total} 通过, {failed} 失败")
    print("=" * 60)

    # 写入 JSON 报告
    report = {
        "title": "v1.2.2 冒烟测试报告",
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": f"{passed / total * 100:.1f}%" if total > 0 else "N/A",
        "results": results,
    }
    report_path = Path(__file__).parent / "smoke_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {report_path}")

    # 清理
    try:
        os.remove(os.environ["DB_PATH"])
        os.remove(os.environ["DB_PATH"] + "-wal")
        os.remove(os.environ["DB_PATH"] + "-shm")
    except OSError:
        pass

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    _setup_db()
    sys.exit(run_smoke())
