"""登录路由回归：空用户名处理 + 锁定短路。

不连真实 DB：用 MagicMock 代替 Database，SimpleNamespace 模拟 Request；
两个用例都在 authenticate() 调用之前结束，因此不需要打桩认证逻辑。
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.exceptions import AppException
from backend.models import LoginRequest
from backend.routers import auth as auth_router


def _req(peer="127.0.0.1"):
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=peer),
        headers=types.SimpleNamespace(get=lambda key, default="": default),
    )


class TestLoginGuardRails(unittest.TestCase):
    def test_empty_username_rejected_without_lock_lookup_or_record(self):
        """空用户名：401 invalid_credentials，且不查锁/不计数（避免 md5(ip+':') 共享桶）。"""
        db = MagicMock()
        with patch.object(auth_router, "is_login_locked") as locked, \
             patch.object(auth_router, "record_login_failure") as record, \
             patch("backend.auth.authenticate") as authenticate:
            with self.assertRaises(AppException) as ctx:
                auth_router.login(LoginRequest(user="   ", password="x"), _req(), db)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.error_key, "errors.invalid_credentials")
        locked.assert_not_called()
        record.assert_not_called()
        authenticate.assert_not_called()

    def test_missing_username_field_same_path(self):
        db = MagicMock()
        with patch.object(auth_router, "record_login_failure") as record, \
             patch("backend.auth.authenticate") as authenticate:
            with self.assertRaises(AppException) as ctx:
                auth_router.login(LoginRequest(user="", password=""), _req("8.8.8.8"), db)
        self.assertEqual(ctx.exception.status_code, 401)
        record.assert_not_called()
        authenticate.assert_not_called()

    def test_locked_user_short_circuits_before_authenticate(self):
        """命中锁定：429 errors.login_locked，且不执行验密。"""
        db = MagicMock()
        with patch.object(auth_router, "is_login_locked", return_value=True) as locked, \
             patch.object(auth_router, "record_login_failure") as record, \
             patch("backend.auth.authenticate") as authenticate:
            with self.assertRaises(AppException) as ctx:
                auth_router.login(LoginRequest(user="Admin", password="x"), _req(), db)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.error_key, "errors.login_locked")
        locked.assert_called_once()
        record.assert_not_called()
        authenticate.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
