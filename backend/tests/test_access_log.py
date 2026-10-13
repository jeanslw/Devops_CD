"""访问日志脱敏(backend/access_log.py):一次性 code / state / token 不进日志(§10).

uvicorn 的访问日志会打印完整请求行;SSO 回调 URL 里带着一次性授权码与 CSRF state,
旧客户端的 WS 回退写法把会话 token 放在 query 里 -- 这些都属"凭据不落盘".
本用例锁住三件事:敏感值被替换,路径与其余参数保留(排障信息不丢),非 SSO 路由完全不受影响.

运行(项目根):
    python -m pytest backend/tests/test_access_log.py -q
"""

import io
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.access_log import (
    MASK,
    MaskCredentialQueryFilter,
    install_access_log_filter,
    mask_query,
)

# uvicorn.access 的记录格式:(client_addr, method, full_path, http_version, status_code)
_HTTP_ARGS = ("127.0.0.1:52344", "GET", "/api/auth/oidc/callback?code=SECRETCODE&state=SECRETSTATE", "1.1", 302)


def _record(args: object) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None  # type: ignore[arg-type]
    )


class TestMaskQuery(unittest.TestCase):
    """只替换敏感参数的值,路径与其余参数逐字符保留."""

    def test_callback_code_and_state_are_masked(self):
        masked = mask_query("/api/auth/oidc/callback?code=SECRETCODE&state=SECRETSTATE")
        self.assertEqual(masked, f"/api/auth/oidc/callback?code={MASK}&state={MASK}")

    def test_non_sensitive_params_survive(self):
        # retry 是流程调试信息("是否已静默重试过"),不该被抹掉
        self.assertEqual(mask_query("/api/auth/oidc/start?retry=1"), "/api/auth/oidc/start?retry=1")

    def test_ws_token_fallback_is_masked(self):
        self.assertEqual(mask_query("/ws/terminal/7?token=SESSIONTOKEN"), f"/ws/terminal/7?token={MASK}")

    def test_path_without_query_untouched(self):
        self.assertEqual(mask_query("/api/auth/oidc/start"), "/api/auth/oidc/start")

    def test_other_routes_are_not_rewritten(self):
        # 业务路由的 query 一律不动(避免误伤业务参数:分页,筛选,导入参数...)
        self.assertEqual(mask_query("/api/servers?page=2&state=whatever"), "/api/servers?page=2&state=whatever")


class TestFilterOnRecords(unittest.TestCase):
    def test_http_record_is_masked_and_keeps_other_fields(self):
        rec = _record(_HTTP_ARGS)
        self.assertTrue(MaskCredentialQueryFilter().filter(rec))
        self.assertEqual(rec.args[0], "127.0.0.1:52344")
        self.assertEqual(rec.args[2], f"/api/auth/oidc/callback?code={MASK}&state={MASK}")
        self.assertEqual(rec.args[4], 302)
        line = rec.getMessage()
        self.assertNotIn("SECRETCODE", line)
        self.assertNotIn("SECRETSTATE", line)
        self.assertIn("/api/auth/oidc/callback", line)  # 路径与状态码仍可排障

    def test_non_sso_record_untouched(self):
        args = ("127.0.0.1:52344", "GET", "/api/servers?page=2", "1.1", 200)
        rec = _record(args)
        MaskCredentialQueryFilter().filter(rec)
        self.assertIs(rec.args, args)

    def test_websocket_dict_args_masked_without_mutating_input(self):
        args = {"client_addr": "127.0.0.1:52344", "path": "/ws/terminal/7?token=SESSIONTOKEN", "status_code": 101}
        rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%(path)s", args, None)
        MaskCredentialQueryFilter().filter(rec)
        self.assertEqual(rec.args["path"], f"/ws/terminal/7?token={MASK}")
        self.assertEqual(rec.args["status_code"], 101)
        self.assertEqual(args["path"], "/ws/terminal/7?token=SESSIONTOKEN")  # 入参 dict 不被就地改写

    def test_unexpected_args_shape_is_passed_through(self):
        rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "startup complete", None, None)
        self.assertTrue(MaskCredentialQueryFilter().filter(rec))


class TestInstall(unittest.TestCase):
    """安装到 logger 上之后,真正打出来的一整行也不含凭据."""

    LOGGER = "cd.tests.access-log"

    def tearDown(self):
        logger = logging.getLogger(self.LOGGER)
        logger.filters = [f for f in logger.filters if not isinstance(f, MaskCredentialQueryFilter)]
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    def test_installed_filter_scrubs_formatted_output(self):
        logger = logging.getLogger(self.LOGGER)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)

        install_access_log_filter(self.LOGGER)
        install_access_log_filter(self.LOGGER)  # 幂等:不重复叠加(否则 record 被改写两次也无害,但要防止)
        self.assertEqual(len([f for f in logger.filters if isinstance(f, MaskCredentialQueryFilter)]), 1)

        logger.info('%s - "%s %s HTTP/%s" %d', *_HTTP_ARGS)
        out = stream.getvalue()
        self.assertIn("/api/auth/oidc/callback", out)
        self.assertIn("302", out)
        self.assertNotIn("SECRETCODE", out)
        self.assertNotIn("SECRETSTATE", out)


if __name__ == "__main__":
    unittest.main()
