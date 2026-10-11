"""登录路由回归：空用户名处理 + 部署闸门 + 锁定短路。

不连真实 DB：用 MagicMock 代替 Database（`cd_missing_tables()` 默认桩为返回空 = 表建全 = 已部署），
SimpleNamespace 模拟 Request / Response；用例都在 authenticate() 调用之前结束，
因此不需要打桩认证逻辑。
"""

import os
import sys
import types
import unittest
from typing import cast
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import Request, Response

from backend.exceptions import AppException
from backend.models import LoginRequest
from backend.routers import auth as auth_router


def _req(peer="127.0.0.1") -> Request:
    # 最小鸭子类型 mock（这些用例在 authenticate() 之前结束，不会真正用到 Request 其余部分）
    return cast(
        Request,
        types.SimpleNamespace(
            client=types.SimpleNamespace(host=peer),
            headers=types.SimpleNamespace(get=lambda key, default="": default),
        ),
    )


def _resp() -> Response:
    # 登录成功分支才会写 cookie；当前用例都在此之前结束，给个最小 mock 即可
    return cast(
        Response,
        types.SimpleNamespace(
            set_cookie=lambda *a, **k: None,
            delete_cookie=lambda *a, **k: None,
        ),
    )


def _db(deployed: bool = True) -> MagicMock:
    """MagicMock 代替 Database：部署闸门只看 cd_missing_tables()（空列表 = 表建全 = 已部署），
    驱动显式给 "mysql"，让闸门消息走 MySQL 分支（补表动作按驱动变化，见 cd_deploy_hint）。"""
    db = MagicMock()
    db.driver = "mysql"
    db.cd_missing_tables.return_value = [] if deployed else ["cd_approvals", "cd_sessions"]
    return db


class TestLoginGuardRails(unittest.TestCase):
    def test_empty_username_rejected_without_lock_lookup_or_record(self):
        """空用户名：401 invalid_credentials，且不查锁/不计数（避免 md5(ip+':') 共享桶）。"""
        db = _db()
        with (
            patch.object(auth_router, "is_login_locked") as locked,
            patch.object(auth_router, "record_login_failure") as record,
            patch("backend.auth.authenticate") as authenticate,
        ):
            with self.assertRaises(AppException) as ctx:
                auth_router.login(LoginRequest(user="   ", password="x"), _req(), _resp(), db=db)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.error_key, "errors.invalid_credentials")
        locked.assert_not_called()
        record.assert_not_called()
        authenticate.assert_not_called()

    def test_missing_username_field_same_path(self):
        db = _db()
        with (
            patch.object(auth_router, "record_login_failure") as record,
            patch("backend.auth.authenticate") as authenticate,
        ):
            with self.assertRaises(AppException) as ctx:
                auth_router.login(LoginRequest(user="", password=""), _req("8.8.8.8"), _resp(), db=db)
        self.assertEqual(ctx.exception.status_code, 401)
        record.assert_not_called()
        authenticate.assert_not_called()

    def test_locked_user_short_circuits_before_authenticate(self):
        """命中锁定：429 errors.login_locked，且不执行验密。"""
        db = _db()
        with (
            patch.object(auth_router, "is_login_locked", return_value=True) as locked,
            patch.object(auth_router, "record_login_failure") as record,
            patch("backend.auth.authenticate") as authenticate,
        ):
            with self.assertRaises(AppException) as ctx:
                auth_router.login(LoginRequest(user="Admin", password="x"), _req(), _resp(), db=db)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.error_key, "errors.login_locked")
        locked.assert_called_once()
        record.assert_not_called()
        authenticate.assert_not_called()

    def test_not_deployed_blocks_login_before_lock_and_failure_count(self):
        """部署闸门：CD 表未建全（cd_missing_tables 非空）→ 503 errors.cd_not_deployed，
        且不查锁、不计数、不验密 —— 未启用的部署不得污染共享 cache 表的失败锁。"""
        db = _db(deployed=False)
        with (
            patch.object(auth_router, "is_login_locked") as locked,
            patch.object(auth_router, "record_login_failure") as record,
            patch("backend.auth.authenticate") as authenticate,
        ):
            with self.assertRaises(AppException) as ctx:
                auth_router.login(LoginRequest(user="Admin", password="x"), _req(), _resp(), db=db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_deployed")
        self.assertIn("init_mysql.sql", ctx.exception.message)
        # 缺失表明细随 error_params 下发，前端可插值展示（errors.cd_not_deployed）
        self.assertIn("cd_approvals", ctx.exception.error_params["tables"])
        locked.assert_not_called()
        record.assert_not_called()
        authenticate.assert_not_called()

    def test_deployed_login_proceeds_to_authenticate(self):
        """已部署（表建全）时闸门放行：正常走到验密并下发 token。"""
        db = _db()
        with (
            patch.object(auth_router, "is_login_locked", return_value=False),
            patch.object(auth_router, "record_login_failure") as record,
            patch.object(auth_router, "clear_login_failure") as clear,
            patch("backend.auth.authenticate", return_value="tok-123") as authenticate,
        ):
            body = auth_router.login(LoginRequest(user="Admin", password="x"), _req(), _resp(), db=db)
        self.assertEqual(body, {"token": "tok-123"})
        authenticate.assert_called_once()
        clear.assert_called_once()
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
