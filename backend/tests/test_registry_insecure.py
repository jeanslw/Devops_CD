"""明文 HTTP 仓库放行守护(backend/deployers/registry_insecure.py)单元测试.

覆盖:
    - parse_daemon_configs():===CDDAEMON: 标记分段 → {路径: 内容};无标记 → {}
    - insecure_configured():dockerd 匹配语义(条目 host:port 逐字相等 / CIDR 覆盖字面 IP /
      裸 host 不覆盖其他端口)+ 非法 JSON 文件被跳过 + 缺少 insecure-registries 键
    - merge_insecure_entry():保留原有键与条目、幂等去重、非列表值归一、非 JSON 对象拒绝改写(None)
    - is_plain_http_registry_error():命中 server gave HTTP response to HTTPS client(大小写不敏感),
      不误伤认证失败 / 域名解析失败等其他故障
    - ensure_insecure_registry()(FakeSSH,不连真实主机):
        * 已放行 → ok,且一条写命令都不发(只读)
        * 未放行 + allow_auto=False(默认) → ok=False + 可复制指引,仍是只读
        * 未放行 + allow_auto=True → 备份 + 写入 + reload dockerd 后 ok=True / auto_fixed=True
        * daemon.json 非法 JSON + allow_auto=True → 拒绝改写(不覆盖人工配置)
        * 写入失败 / 重载失败 → ok=False,指引带真实原因

运行(项目根):
    python -m pytest backend/tests/test_registry_insecure.py -q
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.config import settings  # noqa: E402

settings.db_driver = "sqlite"

from backend.deployers.registry_insecure import (  # noqa: E402
    DAEMON_BACKUP,
    DAEMON_JSON,
    ensure_insecure_registry,
    guidance,
    insecure_configured,
    is_plain_http_registry_error,
    merge_insecure_entry,
    parse_daemon_configs,
)

REG = "192.168.137.5:5000"
DROPIN = "/etc/docker/daemon.json.d/10-registry.json"


class FakeChannel:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code

    def recv_exit_status(self):
        return self.exit_code


class FakeStream:
    def __init__(self, data="", exit_code=0):
        self._data = data.encode() if isinstance(data, str) else (data or b"")
        self.channel = FakeChannel(exit_code)

    def read(self):
        return self._data


class FakeSSH:
    """handler(cmd) -> (stdout, stderr, exit_code);commands 按序记录全部下发命令."""

    def __init__(self, handler=None):
        self._handler = handler
        self.commands = []

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        out, err, ec = self._handler(cmd) if self._handler else ("", "", 0)
        return (None, FakeStream(out, ec), FakeStream(err, ec))


def _dump(*files) -> str:
    """按 dump 命令的输出格式拼一段目标机配置文本."""
    return "\n".join(f"===CDDAEMON:{p}\n{c}" for p, c in files)


def _write_cmds(ssh) -> list:
    """只取会改动目标机的命令(写入 / 备份 / 重载),用于断言「只读」."""
    return [c for c in ssh.commands if "printf %s" in c or "systemctl" in c]


class TestParseDaemonConfigs(unittest.TestCase):
    def test_splits_marked_files(self):
        files = parse_daemon_configs(_dump((DAEMON_JSON, '{"log-driver": "json-file"}'), (DROPIN, "{}")))
        self.assertEqual(set(files), {DAEMON_JSON, DROPIN})
        self.assertEqual(json.loads(files[DAEMON_JSON])["log-driver"], "json-file")

    def test_no_marker_yields_empty(self):
        self.assertEqual(parse_daemon_configs(""), {})
        self.assertEqual(parse_daemon_configs("[ -f ] && cat ... 没有任何输出"), {})


class TestInsecureConfigured(unittest.TestCase):
    """对齐 dockerd 的 insecure-registries 匹配语义"""

    def test_exact_host_port_match(self):
        self.assertTrue(insecure_configured({DAEMON_JSON: json.dumps({"insecure-registries": [REG]})}, REG))

    def test_match_is_case_insensitive(self):
        files = {DAEMON_JSON: json.dumps({"insecure-registries": ["REG.EXAMPLE.COM:5000"]})}
        self.assertTrue(insecure_configured(files, "reg.example.com:5000"))

    def test_bare_host_does_not_cover_other_port(self):
        """条目 192.168.137.5 只放行默认端口,不覆盖 :5000(dockerd 亦然)"""
        files = {DAEMON_JSON: json.dumps({"insecure-registries": ["192.168.137.5"]})}
        self.assertFalse(insecure_configured(files, REG))

    def test_cidr_entry_covers_ip(self):
        files = {DAEMON_JSON: json.dumps({"insecure-registries": ["192.168.137.0/24"]})}
        self.assertTrue(insecure_configured(files, REG))
        files = {DAEMON_JSON: json.dumps({"insecure-registries": ["10.0.0.0/8"]})}
        self.assertFalse(insecure_configured(files, REG))

    def test_dropin_file_counts(self):
        files = {DAEMON_JSON: "{}", DROPIN: json.dumps({"insecure-registries": [REG]})}
        self.assertTrue(insecure_configured(files, REG))

    def test_broken_json_file_is_skipped(self):
        files = {DAEMON_JSON: "{ not json", DROPIN: json.dumps({"insecure-registries": [REG]})}
        self.assertTrue(insecure_configured(files, REG))
        self.assertFalse(insecure_configured({DAEMON_JSON: "{ not json"}, REG))

    def test_missing_or_mistyped_key(self):
        self.assertFalse(insecure_configured({DAEMON_JSON: "{}"}, REG))
        self.assertFalse(insecure_configured({DAEMON_JSON: json.dumps({"insecure-registries": REG})}, REG))


class TestMergeInsecureEntry(unittest.TestCase):
    """自动修复时的 daemon.json 合并(纯函数)"""

    def test_creates_entry_from_empty(self):
        merged = merge_insecure_entry("", REG)
        self.assertEqual(json.loads(merged), {"insecure-registries": [REG]})
        self.assertTrue(merged.endswith("\n"))

    def test_preserves_other_keys_and_entries(self):
        src = json.dumps({"log-driver": "json-file", "insecure-registries": ["old.example.com:5000"]})
        cfg = json.loads(merge_insecure_entry(src, REG))
        self.assertEqual(cfg["log-driver"], "json-file")
        self.assertEqual(cfg["insecure-registries"], ["old.example.com:5000", REG])

    def test_idempotent_when_already_present(self):
        src = json.dumps({"insecure-registries": [REG]})
        self.assertEqual(json.loads(merge_insecure_entry(src, REG))["insecure-registries"], [REG])

    def test_cidr_covered_entry_not_duplicated(self):
        src = json.dumps({"insecure-registries": ["192.168.137.0/24"]})
        self.assertEqual(json.loads(merge_insecure_entry(src, REG))["insecure-registries"], ["192.168.137.0/24"])

    def test_non_list_value_normalized(self):
        src = json.dumps({"insecure-registries": "oops"})
        self.assertEqual(json.loads(merge_insecure_entry(src, REG))["insecure-registries"], [REG])

    def test_refuses_broken_or_non_object_json(self):
        """不能安全改写时返回 None 并由上层拒绝写入,避免破坏人工维护的配置"""
        self.assertIsNone(merge_insecure_entry("{ not json", REG))
        self.assertIsNone(merge_insecure_entry("[1, 2]", REG))


class TestPlainHttpSignature(unittest.TestCase):
    def test_matches_docker_client_error(self):
        text = (
            'Error response from daemon: Get "https://192.168.137.5:5000/v2/": '
            "http: server gave HTTP response to HTTPS client"
        )
        self.assertTrue(is_plain_http_registry_error(text))
        self.assertTrue(is_plain_http_registry_error("SERVER GAVE HTTP RESPONSE TO HTTPS CLIENT"))

    def test_other_errors_not_matched(self):
        for text in ("unauthorized: authentication required", "dial tcp: no such host", "manifest unknown", ""):
            self.assertFalse(is_plain_http_registry_error(text), text)


class TestGuidance(unittest.TestCase):
    def test_contains_copyable_steps(self):
        text = guidance(REG)
        self.assertIn(f"docker pull {REG}/<repo>:<tag>", text)
        self.assertIn("systemctl reload docker", text)
        self.assertIn(DAEMON_BACKUP, text)
        self.assertIn("REGISTRY_INSECURE_AUTO=true", text)

    def test_reason_is_included(self):
        self.assertIn("原因 / reason: boom", guidance(REG, reason="boom"))


class TestEnsureInsecureRegistry(unittest.TestCase):
    """放行检测与自动修复的编排(FakeSSH,不连真实主机)"""

    def _ssh(self, dump_text, write_ec=0, reload_ec=0):
        def handler(cmd):
            if "===CDDAEMON:" in cmd:
                return (dump_text, "", 0)
            if cmd.startswith("mkdir -p"):  # 写入 daemon.json
                return ("", "permission denied" if write_ec else "", write_ec)
            if "systemctl" in cmd:  # reload || restart dockerd
                return ("", "Failed to reload docker.service" if reload_ec else "", reload_ec)
            return ("", "", 0)

        return FakeSSH(handler)

    def test_already_allowed_stays_read_only(self):
        ssh = self._ssh(_dump((DAEMON_JSON, json.dumps({"insecure-registries": [REG]}))))
        res = ensure_insecure_registry(ssh, REG)
        self.assertTrue(res.ok)
        self.assertFalse(res.auto_fixed)
        self.assertEqual(res.detail, "")
        self.assertEqual(_write_cmds(ssh), [])
        self.assertEqual(len(ssh.commands), 1)  # 只有 dump,一条写命令都没有

    def test_blocked_without_auto_is_read_only_and_guides(self):
        ssh = self._ssh(_dump((DAEMON_JSON, "{}")))
        res = ensure_insecure_registry(ssh, REG, allow_auto=False)
        self.assertFalse(res.ok)
        self.assertFalse(res.auto_fixed)
        self.assertIn(DAEMON_JSON, res.detail)
        self.assertIn("insecure-registries", res.detail)
        self.assertIn("REGISTRY_INSECURE_AUTO=true", res.detail)
        self.assertEqual(_write_cmds(ssh), [])
        self.assertEqual(len(ssh.commands), 1)

    def test_auto_fix_backs_up_writes_and_reloads(self):
        ssh = self._ssh(_dump((DAEMON_JSON, json.dumps({"log-driver": "json-file"}))))
        res = ensure_insecure_registry(ssh, REG, allow_auto=True)
        self.assertTrue(res.ok, res.detail)
        self.assertTrue(res.auto_fixed)
        writes = _write_cmds(ssh)
        self.assertEqual(len(writes), 2, writes)  # 写入 + 重载
        self.assertIn("cp -p", writes[0])  # 先备份
        self.assertIn(DAEMON_BACKUP, writes[0])
        self.assertIn(REG, writes[0])  # 新条目写入
        self.assertIn('"insecure-registries": [', writes[0])
        self.assertIn("json-file", writes[0])  # 原有键保留
        self.assertIn("systemctl reload docker", writes[1])
        self.assertEqual(len(ssh.commands), 3)  # dump + 写入 + 重载

    def test_auto_fix_refuses_broken_daemon_json(self):
        ssh = self._ssh(_dump((DAEMON_JSON, "{ not json")))
        res = ensure_insecure_registry(ssh, REG, allow_auto=True)
        self.assertFalse(res.ok)
        self.assertFalse(res.auto_fixed)
        self.assertIn("合法 JSON", res.detail)
        self.assertEqual(_write_cmds(ssh), [])
        self.assertEqual(len(ssh.commands), 1)

    def test_write_failure_reports_reason_and_skips_reload(self):
        ssh = self._ssh(_dump((DAEMON_JSON, "{}")), write_ec=1)
        res = ensure_insecure_registry(ssh, REG, allow_auto=True)
        self.assertFalse(res.ok)
        self.assertFalse(res.auto_fixed)
        self.assertIn("permission denied", res.detail)
        self.assertNotIn("systemctl", " ".join(ssh.commands))

    def test_reload_failure_reports_reason(self):
        ssh = self._ssh(_dump((DAEMON_JSON, "{}")), reload_ec=1)
        res = ensure_insecure_registry(ssh, REG, allow_auto=True)
        self.assertFalse(res.ok)
        self.assertTrue(res.auto_fixed)  # 配置已写入
        self.assertIn("Failed to reload docker.service", res.detail)
