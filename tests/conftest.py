"""全局 Fixtures — 数据库、环境、App 客户端"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 确保项目根在 path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── SQLite 测试环境 ──
os.environ.setdefault("DB_DRIVER", "sqlite")
os.environ.setdefault("DB_HOST", "")
os.environ.setdefault("DB_NAME", "")
os.environ.setdefault("DB_USER", "")
os.environ.setdefault("DB_PASS", "")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests")
os.environ.setdefault("HARBOR_REGISTRY", "test.local")
os.environ.setdefault("HARBOR_USER", "admin")
os.environ.setdefault("HARBOR_PASSWORD", "test")
os.environ.setdefault("MONITORING_ENABLED", "true")
os.environ.setdefault("CI_API_URL", "")


@pytest.fixture
def tmp_db_path():
    """临时 SQLite 数据库路径（测试后自动清理）"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="cd_test_")
    os.close(fd)
    os.environ["DB_PATH"] = path
    yield path
    try:
        os.remove(path)
        os.remove(path + "-wal")  # WAL 文件
    except OSError:
        pass
    try:
        os.remove(path + "-shm")  # WAL 文件
    except OSError:
        pass


@pytest.fixture
def seed_db(tmp_db_path):
    """创建带 admin_users 表和测试用户的数据库，绕过 _validate_shared_db"""
    import sqlite3

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # admin_users 表（CI 共享）
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username VARCHAR(255) UNIQUE,
        password_hash VARCHAR(255),
        role VARCHAR(32) DEFAULT 'admin',
        systems TEXT DEFAULT ''
    )""")

    # 插入测试用户（密码: "admin123"）
    import bcrypt
    pwd_hash = bcrypt.hashpw("admin123".encode(), bcrypt.gensalt()).decode()
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role, systems) VALUES (?, ?, ?, ?)",
        ("admin", pwd_hash, "admin", "cd"),
    )

    # ci_pipeline_tags 表（绕过校验）
    conn.execute("""CREATE TABLE IF NOT EXISTS ci_pipeline_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project VARCHAR(255),
        tag VARCHAR(255),
        created_at TEXT
    )""")

    conn.commit()
    conn.close()

    # 重新加载 settings（因为 DB_PATH 已变）
    from backend.config import settings
    # 强制刷新 db_path
    settings.db_path = tmp_db_path
    settings.db_driver = "sqlite"

    # 重置 Database 类状态
    from backend.database import Database
    Database._tables_ensured = False
    Database._pool = None

    # 重置 auth 模块的 _systems_col_ok
    import backend.auth as auth_mod
    auth_mod._systems_col_ok = True

    return tmp_db_path


@pytest.fixture
def app(seed_db):
    """创建 FastAPI TestClient（绕开 startup 事件）"""
    # 在 seed_db 之后执行，确保数据库就绪
    from main import app
    # 移除 startup 事件以避免 Harbor/CI 连接
    app.router.on_startup = []
    return app


@pytest.fixture
def client(app):
    """FastAPI TestClient"""
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def auth_token(client):
    """获取 admin 用户的 token"""
    r = client.post("/api/login", json={"user": "admin", "password": "admin123"})
    assert r.status_code == 200, f"Login failed: {r.json()}"
    return r.json()["token"]


@pytest.fixture
def auth_headers(auth_token):
    """带 token 的请求头"""
    return {"Authorization": f"Bearer {auth_token}"}
