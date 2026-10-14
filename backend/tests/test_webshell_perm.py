"""web 终端准入判据(_webshell_permitted)与独立鉴权路径(_ws_verify)单元测试.

锁定 backend/routers/terminal.py 两处口径:
  1. '*' 通配 / super_admin 隐含全权 / cd.webshell 三者的放行口径与 require_perm / enforce_deploy_perm 一致;
  2. _ws_verify 与 REST(verify_token)同口径:同一套"查 cd_sessions + Glue 复核",
     身份复核判为无 CD 权限 / 不可用时必须 401,不能因为走 WebSocket 就放行.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import HTTPException  # noqa: E402

# 纯函数测试不碰库,但导入 terminal 会传递导入 backend.database;沿用既有测试惯例固定 sqlite,避免读 .env 的 mysql
from backend.config import settings  # noqa: E402

settings.db_driver = "sqlite"

from backend.auth import _hash_token  # noqa: E402
from backend.database import Database  # noqa: E402
from backend.exceptions import AppException  # noqa: E402
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
    """WebShell 独立鉴权路径(_ws_verify)与 REST 同口径:同一套会话 + Glue 复核.

    身份复核拒绝(无 CD 权限 / 账号停用 / Glue 不可用)一律 401,握手不会放行.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "ws_admission.db")
        self._saved_db_path = settings.db_path
        settings.db_path = self.db_path
        enable_cd(self.db_path)  # 契约 enabled=1 + auto:让闸门放行、cd_sessions 自动建出
        Database._tables_ensured = False
        db = Database(self.db_path)
        with db.conn() as conn:
            self.token = "ws-test-token"
            conn.execute(
                "INSERT INTO cd_sessions (token_hash, username, expires_at) VALUES (?,?,?)",
                (_hash_token(self.token), "mallory", int(time.time()) + 3600),
            )

    def tearDown(self):
        settings.db_path = self._saved_db_path
        Database._tables_ensured = False
        self._tmp.cleanup()

    def test_verify_denies_user_without_cd_system(self):
        """Glue 复核判为无 CD 权限(AppException 401)→ 握手期同样 401,不按"未知即放行"处理."""
        denied = AppException(
            "Access denied: CD system not authorized", status_code=401, error_key="errors.no_cd_access"
        )
        with patch("backend.routers.terminal._oidc_identity", side_effect=denied):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(_ws_verify(self.token))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_verify_allows_user_with_cd_system(self):
        ident = {
            "username": "mallory",
            "role": "deployer",
            "systems": "cd",
            "status": 1,
            "permissions": ["cd.webshell"],
        }
        with patch("backend.routers.terminal._oidc_identity", return_value=ident):
            self.assertEqual(asyncio.run(_ws_verify(self.token)), "mallory")

    def test_verify_rejects_unknown_token(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(_ws_verify("no-such-token"))
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
