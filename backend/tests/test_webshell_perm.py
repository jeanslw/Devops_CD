"""web 终端准入判据(_webshell_permitted)与独立鉴权路径(_ws_verify)单元测试.

锁定两处修复(见 backend/routers/terminal.py):
  1. '*' 通配 / super_admin 隐含全权 / cd.webshell 三者的放行口径与 require_perm / enforce_deploy_perm 一致
     (local 分支原先不认 '*');
  2. _ws_verify local 分支与 REST(verify_token)同口径:systems 不含 'cd' 的账号必须 401 --
     WebShell 是独立鉴权路径,不能漏掉 _check_cd_access(原先只查 "username, status").
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import HTTPException  # noqa: E402

# 纯函数测试不碰库,但导入 terminal 会传递导入 backend.database;沿用既有测试惯例固定 sqlite,避免读 .env 的 mysql
from backend.config import settings  # noqa: E402

settings.db_driver = "sqlite"

from backend.auth import _hash_token  # noqa: E402
from backend.database import Database  # noqa: E402
from backend.routers.terminal import _webshell_permitted, _ws_verify  # noqa: E402

from cd_contract import enable_cd  # noqa: E402


class TestWebshellPermitted(unittest.TestCase):
    def test_super_admin_role_always_permitted(self):
        # super_admin 隐含全权:即使权限清单为空也放行(与 require_perm 的 role 短路一致)
        self.assertTrue(_webshell_permitted(settings.super_admin_role, []))

    def test_wildcard_permitted_for_non_super_admin(self):
        # '*' 通配:OIDC 模式下 Glue 对超管代码授予,local 模式若出现在清单里同样放行
        self.assertTrue(_webshell_permitted("viewer", ["*"]))

    def test_webshell_perm_permitted(self):
        self.assertTrue(_webshell_permitted("viewer", ["cd.webshell"]))

    def test_no_perm_denied(self):
        self.assertFalse(_webshell_permitted("viewer", []))

    def test_unrelated_perm_denied(self):
        self.assertFalse(_webshell_permitted("viewer", ["cd.deploy.single"]))

    def test_wildcard_mixed_with_other_perms_permitted(self):
        self.assertTrue(_webshell_permitted("viewer", ["cd.deploy.single", "*"]))

    def test_empty_role_denied(self):
        # 用户行缺失 / 无角色 → 空 role + 空权限,fail-closed 拒绝(与 require_perm 的 deny-by-default 同口径)
        self.assertFalse(_webshell_permitted("", []))


class TestWebshellAdmission(unittest.TestCase):
    """WebShell 独立鉴权路径(_ws_verify)必须与 REST 同口径:systems 校验 + 停用踢下线.

    锁定 terminal.py 修复:local 分支原先只查 "username, status"、漏掉 _check_cd_access --
    一个 systems 不含 'cd' 的账号仍可打开 WebShell(而 REST 会被 verify_token 拦下).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "ws_admission.db")
        self._saved_auth_mode = settings.auth_mode
        self._saved_db_path = settings.db_path
        settings.auth_mode = "local"
        settings.db_path = self.db_path
        enable_cd(self.db_path)  # 契约 enabled=1 + auto:让闸门放行、cd_sessions 自动建出
        Database._tables_ensured = False
        db = Database(self.db_path)
        with db.conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(64) UNIQUE,
                    password_hash VARCHAR(255),
                    role VARCHAR(32) DEFAULT '',
                    systems VARCHAR(255) DEFAULT '',
                    status INTEGER DEFAULT 1
                )"""
            )
            # mallory:有会话但 systems 不含 'cd' -- REST 会 401,WebShell 也必须 401
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, role, systems, status) VALUES (?,?,?,?,?)",
                ("mallory", "", "deployer", "ci", 1),
            )
            self.token = "ws-test-token"
            conn.execute(
                "INSERT INTO cd_sessions (token_hash, username, expires_at) VALUES (?,?,?)",
                (_hash_token(self.token), "mallory", int(time.time()) + 3600),
            )

    def tearDown(self):
        settings.auth_mode = self._saved_auth_mode
        settings.db_path = self._saved_db_path
        Database._tables_ensured = False
        self._tmp.cleanup()

    def test_verify_denies_user_without_cd_system(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(_ws_verify(self.token))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_verify_allows_user_with_cd_system(self):
        with Database(self.db_path).conn() as conn:
            conn.execute("UPDATE admin_users SET systems='cd' WHERE username='mallory'")
        self.assertEqual(asyncio.run(_ws_verify(self.token)), "mallory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
