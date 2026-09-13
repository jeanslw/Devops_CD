"""login_guard 回归测试 —— 与 Devops-Glue 共享登录锁的键格式、窗口与 IP 解析。

不连真实 MySQL/SQLite 文件：用单连接内存 sqlite 模拟 db.conn() 上下文，
用 SimpleNamespace 模拟 FastAPI Request。

运行：
    .venv/Scripts/python.exe -m unittest backend.tests.test_login_guard
"""

import hashlib
import os
import sqlite3
import sys
import types
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend import login_guard
from backend.config import settings


def _req(peer, xff=None):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=peer),
        headers=types.SimpleNamespace(get=lambda key, default="": headers.get(key, default)),
    )


class _FakeDB:
    """只实现 login_guard 用到的 with db.conn() as conn: conn.execute(...)"""

    def __init__(self):
        self.raw = sqlite3.connect(":memory:")
        self.raw.row_factory = sqlite3.Row  # login_guard 以 row["value"] 取值
        self.raw.execute("CREATE TABLE cache (cache_key TEXT PRIMARY KEY, value TEXT, expires_at INT)")
        self.raw.commit()

    @contextmanager
    def conn(self):
        try:
            yield self.raw
            self.raw.commit()
        except Exception:
            self.raw.rollback()
            raise


class _BoomDB:
    @contextmanager
    def conn(self):
        raise RuntimeError("db down")
        yield  # pragma: no cover


class TestClientIp(unittest.TestCase):
    def setUp(self):
        self._orig = settings.trusted_proxy_hops
        settings.trusted_proxy_hops = 0

    def tearDown(self):
        settings.trusted_proxy_hops = self._orig

    def test_hops_zero_ignores_xff(self):
        settings.trusted_proxy_hops = 0
        self.assertEqual(login_guard.client_ip(_req("127.0.0.1", "1.2.3.4")), "127.0.0.1")

    def test_public_peer_xff_not_trusted_even_with_hops(self):
        settings.trusted_proxy_hops = 1
        # 用真实公网地址（203.0.113.x 属 TEST-NET-3，Python ipaddress 将文档保留段也归为
        # 非全局可达，与 PHP filter 的保留段定义本就不同；真实公网客户端两边判定一致）
        self.assertEqual(login_guard.client_ip(_req("8.8.8.8", "10.0.0.1")), "8.8.8.8")

    def test_loopback_one_hop_takes_rightmost(self):
        settings.trusted_proxy_hops = 1
        req = _req("127.0.0.1", "1.2.3.4, 198.51.100.9")
        self.assertEqual(login_guard.client_ip(req), "198.51.100.9")

    def test_private_peer_one_hop_single_entry(self):
        settings.trusted_proxy_hops = 1
        self.assertEqual(login_guard.client_ip(_req("10.0.0.254", "198.51.100.9")), "198.51.100.9")

    def test_two_hops_second_from_right(self):
        settings.trusted_proxy_hops = 2
        req = _req("172.16.0.1", "198.51.100.9, 10.0.0.2, 10.0.0.1")
        self.assertEqual(login_guard.client_ip(req), "10.0.0.2")

    def test_hops_exceed_chain_falls_back(self):
        settings.trusted_proxy_hops = 2
        self.assertEqual(login_guard.client_ip(_req("127.0.0.1", "1.2.3.4")), "127.0.0.1")

    def test_invalid_xff_candidate_falls_back(self):
        settings.trusted_proxy_hops = 1
        self.assertEqual(login_guard.client_ip(_req("192.168.1.1", "not-an-ip")), "192.168.1.1")

    def test_ipv6_loopback_peer(self):
        settings.trusted_proxy_hops = 1
        self.assertEqual(login_guard.client_ip(_req("::1", "2001:db8::1")), "2001:db8::1")


class TestLockKey(unittest.TestCase):
    def test_key_matches_glue_format(self):
        # 必须与 Glue AdminAuthService::loginFailKey 完全一致：
        # 'login_fail_' + md5(ip + ':' + lower(username))
        expected = "login_fail_" + hashlib.md5(b"1.2.3.4:admin").hexdigest()
        self.assertEqual(login_guard._key("1.2.3.4", "Admin"), expected)
        self.assertEqual(login_guard._key("1.2.3.4", "ADMIN"), expected)


class TestLockFlow(unittest.TestCase):
    def setUp(self):
        # _upsert_sql 按 settings.db_driver 选方言；内存库是 sqlite
        self._orig_driver = settings.db_driver
        settings.db_driver = "sqlite"
        self.db = _FakeDB()

    def tearDown(self):
        settings.db_driver = self._orig_driver

    def test_five_failures_lock_then_clear(self):
        for _ in range(4):
            login_guard.record_login_failure(self.db, "9.9.9.9", "bob")
            self.assertFalse(login_guard.is_login_locked(self.db, "9.9.9.9", "bob"))
        login_guard.record_login_failure(self.db, "9.9.9.9", "bob")
        self.assertTrue(login_guard.is_login_locked(self.db, "9.9.9.9", "bob"))
        # 另一用户不受影响
        self.assertFalse(login_guard.is_login_locked(self.db, "9.9.9.9", "carol"))

        login_guard.clear_login_failure(self.db, "9.9.9.9", "bob")
        self.assertFalse(login_guard.is_login_locked(self.db, "9.9.9.9", "bob"))

    def test_expired_window_unlocks(self):
        for _ in range(5):
            login_guard.record_login_failure(self.db, "9.9.9.9", "dan")
        self.assertTrue(login_guard.is_login_locked(self.db, "9.9.9.9", "dan"))
        # 直接把过期时间改成过去：is_login_locked 的 expires_at > now 判定应放行
        key = login_guard._key("9.9.9.9", "dan")
        self.db.raw.execute("UPDATE cache SET expires_at = 1 WHERE cache_key = ?", (key,))
        self.db.raw.commit()
        self.assertFalse(login_guard.is_login_locked(self.db, "9.9.9.9", "dan"))

    def test_db_failure_fails_open(self):
        boom = _BoomDB()
        self.assertFalse(login_guard.is_login_locked(boom, "1.1.1.1", "x"))
        # 以下两个调用不得抛异常
        login_guard.record_login_failure(boom, "1.1.1.1", "x")
        login_guard.clear_login_failure(boom, "1.1.1.1", "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
