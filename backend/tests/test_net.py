"""出站地址族优先级回归(backend/net.py):解析顺序、安装开关、配置校验、真实回环选族.

不连外网;真连用例只访问本机 ::1 / 127.0.0.1.安装类用例改写全局状态,tearDown 逐项还原.
运行(项目根):python -m pytest backend/tests/test_net.py -q
"""

import http.client
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import requests  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from backend import net  # noqa: E402
from backend.config import Settings, settings  # noqa: E402

AF_INET = socket.AF_INET
AF_INET6 = socket.AF_INET6
SA_V4 = ("127.0.0.1", 8080)
SA_V6 = ("::1", 8080, 0, 0)


def _ai(family, sa):
    """构造 getaddrinfo 条目(与真实结构同形:family, type, proto, canonname, sockaddr)."""
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sa)


class AddrInfoPreferenceTestCase(unittest.TestCase):
    def setUp(self):
        self.infos = [
            _ai(AF_INET6, SA_V6),
            _ai(AF_INET, SA_V4),
            _ai(AF_INET6, ("::2", 8080, 0, 0)),
            _ai(AF_INET, ("127.0.0.2", 8080)),
        ]

    def tearDown(self):
        socket.getaddrinfo = net._orig_getaddrinfo
        net._installed = False
        net._preferred_family = "ipv4"

    # ── 排序 ──
    def test_target_family_moves_first_keeping_system_order(self):
        with patch.object(net, "_orig_getaddrinfo", return_value=self.infos):
            out = net.getaddrinfo("localhost", 8080)
        # IPv4 全部前移,同族内保持系统返回顺序(127.0.0.1 仍在 127.0.0.2 前)
        self.assertEqual([o[4][0] for o in out], ["127.0.0.1", "127.0.0.2", "::1", "::2"])

    def test_ipv6_preference_reverses_order(self):
        with (
            patch.object(net, "_preferred_family", "ipv6"),
            patch.object(net, "_orig_getaddrinfo", return_value=self.infos),
        ):
            out = net.getaddrinfo("localhost", 8080)
        self.assertEqual([o[4][0] for o in out], ["::1", "::2", "127.0.0.1", "127.0.0.2"])

    def test_entries_are_neither_added_nor_dropped(self):
        with patch.object(net, "_orig_getaddrinfo", return_value=self.infos):
            out = net.getaddrinfo("localhost", 8080)
        self.assertEqual(sorted(map(repr, out)), sorted(map(repr, self.infos)))

    def test_auto_keeps_system_order(self):
        with (
            patch.object(net, "_preferred_family", "auto"),
            patch.object(net, "_orig_getaddrinfo", return_value=self.infos),
        ):
            self.assertIs(net.getaddrinfo("localhost", 8080), self.infos)

    def test_pinned_family_is_left_alone(self):
        for family in (AF_INET, AF_INET6):
            with self.subTest(family=family), patch.object(net, "_orig_getaddrinfo", return_value=self.infos):
                self.assertIs(net.getaddrinfo("localhost", 8080, family), self.infos)

    def test_single_entry_is_left_alone(self):
        one = [_ai(AF_INET, SA_V4)]
        with patch.object(net, "_orig_getaddrinfo", return_value=one):
            self.assertIs(net.getaddrinfo("localhost", 8080), one)

    def test_arguments_are_passed_through(self):
        spy = Mock(return_value=[])
        with patch.object(net, "_orig_getaddrinfo", spy):
            net.getaddrinfo("example.com", "https", 0, socket.SOCK_DGRAM, socket.IPPROTO_UDP, socket.AI_PASSIVE)
        spy.assert_called_once_with("example.com", "https", 0, socket.SOCK_DGRAM, socket.IPPROTO_UDP, socket.AI_PASSIVE)

    # ── 安装 ──
    def test_install_replaces_getaddrinfo_and_is_idempotent(self):
        sentinel = object()
        with patch.object(net, "getaddrinfo", sentinel), patch.object(settings, "http_ip_family", "ipv4"):
            self.assertTrue(net.install_ip_family())
            self.assertIs(socket.getaddrinfo, sentinel)
            self.assertEqual(net._preferred_family, "ipv4")
            # 幂等:第二次直接返回,不再改写
            socket.getaddrinfo = net._orig_getaddrinfo
            self.assertTrue(net.install_ip_family())
            self.assertIs(socket.getaddrinfo, net._orig_getaddrinfo)

    def test_install_ipv6_mode_installs_and_sets_preference(self):
        sentinel = object()
        with patch.object(net, "getaddrinfo", sentinel), patch.object(settings, "http_ip_family", "ipv6"):
            self.assertTrue(net.install_ip_family())
        self.assertIs(socket.getaddrinfo, sentinel)
        self.assertEqual(net._preferred_family, "ipv6")

    def test_install_explicit_argument_overrides_settings(self):
        sentinel = object()
        with patch.object(net, "getaddrinfo", sentinel), patch.object(settings, "http_ip_family", "auto"):
            self.assertTrue(net.install_ip_family(" IPv6 "))  # 入参宽松:大小写 / 空格可容忍
        self.assertIs(socket.getaddrinfo, sentinel)
        self.assertEqual(net._preferred_family, "ipv6")

    def test_install_skipped_when_auto(self):
        with patch.object(settings, "http_ip_family", "auto"):
            self.assertFalse(net.install_ip_family())
        self.assertIs(socket.getaddrinfo, net._orig_getaddrinfo)
        self.assertFalse(net._installed)
        self.assertEqual(net._preferred_family, "auto")

    def test_install_treats_unknown_value_as_auto(self):
        # 直接传值(绕过 config 白名单):按"不干预"处理
        with patch.object(settings, "http_ip_family", "ipv4only"):
            self.assertFalse(net.install_ip_family())
        self.assertIs(socket.getaddrinfo, net._orig_getaddrinfo)
        self.assertFalse(net._installed)
        self.assertEqual(net._preferred_family, "auto")

    def test_legacy_name_is_removed(self):
        self.assertFalse(hasattr(net, "install_ipv4_first"))


class IpFamilySettingTestCase(unittest.TestCase):
    """HTTP_IP_FAMILY 取值解析(backend/config.py)."""

    def test_values_are_normalised(self):
        for raw, want in (("ipv4", "ipv4"), (" IPv6 ", "ipv6"), ("AUTO", "auto"), ("", "ipv4")):
            self.assertEqual(Settings(_env_file=None, http_ip_family=raw).http_ip_family, want)

    def test_unknown_value_is_rejected(self):
        # 拼错立刻报错(不做静默退化)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, http_ip_family="ipv4only")

    def test_legacy_key_is_gone(self):
        self.assertNotIn("http_ipv4_first", Settings.model_fields)
        self.assertEqual(Settings(_env_file=None, http_ipv4_first=False).http_ip_family, "ipv4")


class _FamilyHandler(BaseHTTPRequestHandler):
    """回一个标识自己所在地址族的响应体(v4 / v6)."""

    def do_GET(self):
        body = getattr(self.server, "family_label", "?").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 测试服务不打访问日志


def _localhost_is_dual_stack() -> bool:
    """localhost 是否同时解析出 A 与 AAAA."""
    try:
        infos = socket.getaddrinfo("localhost", 80, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return {info[0] for info in infos} >= {AF_INET, AF_INET6}


class RealClientFamilyCoverageTestCase(unittest.TestCase):
    """真实回环链路:requests(urllib3)、http.client、socket.create_connection 都按偏好选族.

    同一端口只在 127.0.0.1 与 ::1 上各起一个单族 HTTP 服务,响应体标识被连上的那一侧.
    """

    def setUp(self):
        if not _localhost_is_dual_stack():
            raise unittest.SkipTest("localhost 未同时解析出 A / AAAA")
        self.servers = []
        self.port = self._listen_both()
        net._installed = False

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        socket.getaddrinfo = net._orig_getaddrinfo
        net._installed = False
        net._preferred_family = "ipv4"

    def _serve(self, host, family, label, port):
        class _Server(HTTPServer):
            address_family = family
            family_label = label

        srv = _Server((host, port), _FamilyHandler, bind_and_activate=False)
        if family == AF_INET6:
            srv.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)  # 只收 IPv6
        srv.server_bind()
        srv.server_activate()
        self.servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def _listen_both(self) -> int:
        """在 ::1 与 127.0.0.1 的同一端口上各起一个单族 HTTP 服务,返回端口."""
        for _ in range(10):
            v6 = self._serve("::1", AF_INET6, "v6", 0)
            port = v6.server_address[1]
            try:
                self._serve("127.0.0.1", AF_INET, "v4", port)
            except OSError:
                self.servers.remove(v6)
                v6.shutdown()
                v6.server_close()
                continue
            return port
        raise unittest.SkipTest("无法在 ::1 与 127.0.0.1 上绑同一端口")

    def _install(self, family: str):
        net._installed = False  # 同一用例内换族,允许重新安装
        net.install_ip_family(family)

    def test_requests_follows_preferred_family(self):
        for family, want in (("ipv4", "v4"), ("ipv6", "v6")):
            with self.subTest(family=family):
                self._install(family)
                session = requests.Session()
                session.trust_env = False  # 忽略 HTTP_PROXY 等环境变量
                try:
                    body = session.get(f"http://localhost:{self.port}/", timeout=5).text
                finally:
                    session.close()
                self.assertEqual(body, want)

    def test_http_client_follows_preferred_family(self):
        for family, want in (("ipv4", "v4"), ("ipv6", "v6")):
            with self.subTest(family=family):
                self._install(family)
                conn = http.client.HTTPConnection("localhost", self.port, timeout=5)
                try:
                    conn.request("GET", "/")
                    self.assertEqual(conn.getresponse().read().decode(), want)
                finally:
                    conn.close()

    def test_stdlib_socket_connect_and_paramiko_lookup_follow_preference(self):
        # paramiko 与 stdlib 建连都先经 socket.getaddrinfo 拿候选,再逐条尝试
        for family, want in (("ipv4", AF_INET), ("ipv6", AF_INET6)):
            with self.subTest(family=family):
                self._install(family)
                first = socket.getaddrinfo("localhost", self.port, socket.AF_UNSPEC, socket.SOCK_STREAM)[0]
                self.assertEqual(first[0], want)
                sock = socket.create_connection(("localhost", self.port), timeout=5)
                try:
                    self.assertEqual(sock.family, want)  # 首个候选即成功,没走另一族
                finally:
                    sock.close()

    def test_auto_keeps_system_order(self):
        with patch.object(settings, "http_ip_family", "auto"):
            self.assertFalse(net.install_ip_family())
        first = socket.getaddrinfo("localhost", self.port, socket.AF_UNSPEC, socket.SOCK_STREAM)[0]
        conn = http.client.HTTPConnection("localhost", self.port, timeout=5)
        try:
            conn.request("GET", "/")
            self.assertEqual(conn.getresponse().read().decode(), "v6" if first[0] == AF_INET6 else "v4")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
