"""会话令牌治理回归（v1.5.x：不透明 token + cd_sessions 会话表 + logout 即时吊销）。

用真实临时 SQLite 库验证 token 生命周期，覆盖：
  - authenticate 签发的是不透明 token（不携带用户名/口令哈希，与旧 base64(username:hash:expires) 区分）
  - 会话入库只存 SHA-256 摘要（库泄露不暴露可用 token）
  - verify_token / get_current_user 通过会话校验并返回身份
  - logout（revoke_session）删行后 token 立即失效
  - 过期会话被拒绝并惰性清理
  - 无效 token 被拒绝

运行（项目根）:
    python -m pytest backend/tests/test_session_auth.py -q
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bcrypt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

# 测试一律用临时 SQLite，忽略 .env 中可能配置的 MySQL（必须在导入 Database 前设置）
from backend.config import settings

settings.db_driver = "sqlite"

from backend.auth import (  # noqa: E402
    _hash_token,
    authenticate,
    get_current_user,
    revoke_session,
    verify_token,
)
from backend.database import Database  # noqa: E402


def _create_marker(path):
    """Database 启动校验要求与 Devops-Glue 共享库（ci_pipeline_artifacts 为标记表）。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ci_pipeline_artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, project_key VARCHAR(255))")
    conn.commit()
    conn.close()


class SessionAuthTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_session.db")
        _create_marker(self.db_path)
        # 每个临时库都是"全新库"，强制重新建表（类变量在测试间共享）
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(64) UNIQUE,
                password_hash VARCHAR(255),
                role VARCHAR(32) DEFAULT '',
                systems VARCHAR(255) DEFAULT '',
                status INTEGER DEFAULT 1
            )""")
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, role, systems, status) VALUES (?,?,?,?,?)",
                ("alice", bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode(), "deployer", "cd", 1),
            )

    def tearDown(self):
        self._tmp.cleanup()

    def _creds(self, token):
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    def test_authenticate_issues_opaque_token(self):
        token = authenticate("alice", "secret123", self.db)
        self.assertIsNotNone(token)
        # 旧格式是 base64(username:hash:expires)，必然包含用户名与 ':'；新 token 不携带任何用户信息
        self.assertNotIn("alice", token)
        self.assertNotIn(":", token)

    def test_session_stored_as_hash_not_plaintext(self):
        token = authenticate("alice", "secret123", self.db)
        with self.db.conn() as conn:
            row = conn.execute("SELECT token_hash FROM cd_sessions WHERE username='alice'").fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(row["token_hash"], _hash_token(token))

    def test_verify_token_returns_username(self):
        token = authenticate("alice", "secret123", self.db)
        self.assertEqual(verify_token(self._creds(token), self.db), "alice")

    def test_get_current_user_returns_identity(self):
        token = authenticate("alice", "secret123", self.db)
        user = get_current_user(self._creds(token), self.db)
        self.assertEqual(user["username"], "alice")
        self.assertEqual(user["role"], "deployer")
        self.assertEqual(user["systems"], "cd")

    def test_logout_revokes_session(self):
        token = authenticate("alice", "secret123", self.db)
        revoke_session(self.db, token)
        with self.assertRaises(HTTPException) as ctx:
            verify_token(self._creds(token), self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_logout_idempotent(self):
        token = authenticate("alice", "secret123", self.db)
        revoke_session(self.db, token)
        revoke_session(self.db, token)  # 重复吊销不报错
        with self.db.conn() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM cd_sessions").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_expired_session_rejected(self):
        token = authenticate("alice", "secret123", self.db)
        with self.db.conn() as conn:
            conn.execute("UPDATE cd_sessions SET expires_at=1")  # 强制过期
        with self.assertRaises(HTTPException) as ctx:
            verify_token(self._creds(token), self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_invalid_token_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            verify_token(self._creds("does-not-exist"), self.db)
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
