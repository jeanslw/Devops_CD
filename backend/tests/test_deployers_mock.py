"""部署器 mock 测试 —— 不依赖真实 SSH / K8s / ArgoCD / Docker / Ansible。

用 unittest.mock 伪造 SSH 连接、paramiko channel 与 requests，验证四个部署功能
（argocd / fluxcd / ansible(ssh) / docker(compose)）的编排逻辑。

约定：
  - 已修复的 bug 用回归断言固化（修复后行为），防止回退。
  - 纯函数（YAML 渲染 / 参数解析 / 镜像切分）走正确性断言。

运行（纯标准库 unittest，无需 pytest）:
    .venv/Scripts/python.exe backend/tests/test_deployers_mock.py
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# 允许 `python backend/tests/test_deployers_mock.py` 直接运行时找到 backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.config import settings
from backend.deployers.base import DeployTarget, split_image_ref
from backend.deployers.compose import ComposeDeployer
from backend.deployers.k8s_argocd import ArgoCDDeployer
from backend.deployers.k8s_fluxcd import _discover_flux_resource
from backend.deployers.k8s_utils import _get_deployment_name, _render_k8s_yaml
from backend.deployers.ssh import SSHDeployer


# ─────────────────────────────────────────────────────────────
# Fake SSH / HTTP 对象
# ─────────────────────────────────────────────────────────────
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
    """handler(cmd) -> (stdout, stderr, exit_code)。"""

    def __init__(self, handler=None):
        self._handler = handler
        self.commands = []

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        out, err, ec = self._handler(cmd) if self._handler else ("", "", 0)
        return (None, FakeStream(out, ec), FakeStream(err, ec))

    def close(self):
        pass


class _Resp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


# ─────────────────────────────────────────────────────────────
# 纯函数正确性（回归保护）
# ─────────────────────────────────────────────────────────────
class TestPureHelpers(unittest.TestCase):
    def test_render_k8s_yaml_placeholders(self):
        settings.harbor_registry = "hub.example.com"
        yaml = "image: {IMAGE_NAME}:{TAG}\nfull: {IMAGE}:{TAG}\nplain: {IMAGE}\ntag: {TAG}"
        out = _render_k8s_yaml(yaml, "hub.example.com/repo/app:v1.0", "v2.0")
        self.assertIn("image: hub.example.com/repo/app:v2.0", out)
        self.assertIn("full: hub.example.com/repo/app:v2.0", out)
        self.assertNotIn("{IMAGE", out)
        self.assertNotIn("{TAG", out)

    def test_render_k8s_yaml_ported_registry_rsplit(self):
        settings.harbor_registry = "hub.example.com:5000"
        out = _render_k8s_yaml("i: {IMAGE}", "hub.example.com:5000/repo/app:v1.0", "v2.0")
        self.assertIn("hub.example.com:5000/repo/app:v2.0", out)

    def test_get_deployment_name(self):
        yaml = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: myapp\n"
        self.assertEqual(_get_deployment_name(yaml), "myapp")

    def test_split_image_ref_ported_registry(self):
        # 带端口 registry：从最后一个冒号切分，仓库/标签都不被端口破坏
        self.assertEqual(
            split_image_ref("hub.example.com:5000/repo/app:v1.0"), ("hub.example.com:5000/repo/app", "v1.0")
        )

    def test_split_image_ref_no_tag(self):
        self.assertEqual(split_image_ref("hub.example.com/repo/app"), ("hub.example.com/repo/app", ""))

    def test_split_image_ref_plain(self):
        self.assertEqual(split_image_ref("app:v1.0"), ("app", "v1.0"))

    def test_parse_command_options_inventory(self):
        from backend.services.deploy_service import _parse_command_options

        opts = _parse_command_options("ansible-playbook site.yml|INV|prod/hosts")
        self.assertEqual(opts["commands"], "ansible-playbook site.yml")
        self.assertEqual(opts["inventory"], "prod/hosts")

    def test_parse_server_ids(self):
        from backend.services.deploy_service import _parse_server_ids

        self.assertEqual(_parse_server_ids("1,2,abc,3"), [1, 2, 3])
        self.assertEqual(_parse_server_ids(""), [])


# ─────────────────────────────────────────────────────────────
# Ansible / SSH 部署器（ssh.py）
# ─────────────────────────────────────────────────────────────
class TestSSHDeployer(unittest.TestCase):
    def _deploy(self, mode, commands="", path="", exit_code=0):
        target = DeployTarget(
            host="1.2.3.4",
            user="root",
            path=path,
            mode=mode,
            options={"commands": commands, "inventory": "prod/hosts"},
        )
        fake = FakeSSH()
        session = MagicMock()
        session.__enter__.return_value = fake
        session.__exit__.return_value = False
        with (
            patch("backend.deployers.ssh.ssh_session", return_value=session),
            patch("backend.deployers.ssh.ssh_exec_stream", return_value=("command output", exit_code)),
        ):
            return SSHDeployer().deploy(target, "hub.example.com/app:v1.0", "group/app", "v1.0")

    def test_commands_mode_exit_code_failure(self):
        # 修复后：远程命令 exit!=0 判为 failed
        r = self._deploy("commands", commands="exit 1", exit_code=1)
        self.assertEqual(r.status, "failed")

    def test_commands_mode_exit_code_success(self):
        r = self._deploy("commands", commands="echo ok", exit_code=0)
        self.assertEqual(r.status, "ok")

    def test_ansible_mode_exit_code_failure(self):
        # 修复后：ansible-playbook 失败（exit!=0）判为 failed
        r = self._deploy("ansible", path="/opt/ansible/deploy.yml", exit_code=2)
        self.assertEqual(r.status, "failed")

    def test_stop_checks_exit_code(self):
        # 修复后：stop 检查命令 exit code，非 0 报失败
        target = DeployTarget(host="1.2.3.4", user="root")
        fake = FakeSSH(handler=lambda cmd: ("", "stop failed", 1))
        session = MagicMock()
        session.__enter__.return_value = fake
        session.__exit__.return_value = False
        with patch("backend.deployers.ssh.ssh_session", return_value=session):
            r = SSHDeployer().stop(target, "group/app", commands="systemctl stop app", tag="v1.0")
        self.assertFalse(r["success"])
        self.assertIn("stop failed", r["output"])


# ─────────────────────────────────────────────────────────────
# Docker Compose 部署器（compose.py）
# ─────────────────────────────────────────────────────────────
class TestComposeDeployer(unittest.TestCase):
    def test_commands_mode_exit_code_failure(self):
        # 修复后：commands 模式以命令 exit code 判定成败
        target = DeployTarget(host="1.2.3.4", user="root", mode="commands", options={"commands": "exit 1"})
        fake = FakeSSH()
        session = MagicMock()
        session.__enter__.return_value = fake
        session.__exit__.return_value = False
        with (
            patch("backend.deployers.compose.ssh_session", return_value=session),
            patch("backend.deployers.compose.ssh_exec_stream", return_value=("command failed", 1)),
        ):
            r = ComposeDeployer().deploy(target, "hub.example.com/app:v1.0", "group/app", "v1.0")
        self.assertEqual(r.status, "failed")

    def test_ssh_run_prefers_stdout(self):
        # 修复后：_ssh_run 返回 (out or err)，stdout 优先级高于 stderr。
        def handler(cmd):
            return ("STDOUT_REAL_RESULT", "STDERR_BENIGN_WARNING", 0)

        r = ComposeDeployer()._ssh_run(FakeSSH(handler), "cmd", "img")
        self.assertEqual(r.output, "STDOUT_REAL_RESULT")

    def test_stop_checks_exit_code(self):
        # 修复后：stop 检查 docker-compose down 的 exit code
        target = DeployTarget(host="1.2.3.4", user="root")
        fake = FakeSSH(handler=lambda cmd: ("", "compose file not found", 1))
        session = MagicMock()
        session.__enter__.return_value = fake
        session.__exit__.return_value = False
        with patch("backend.deployers.compose.ssh_session", return_value=session):
            r = ComposeDeployer().stop(target, "app", target_path="/bad/path")
        self.assertFalse(r["success"])
        self.assertIn("compose file not found", r["output"])


# ─────────────────────────────────────────────────────────────
# Argo CD 部署器（k8s_argocd.py）
# ─────────────────────────────────────────────────────────────
class TestArgoCD(unittest.TestCase):
    def _req(self):
        return types.SimpleNamespace(api_url="https://argocd:30443")

    def _deploy(self, image, app_spec):
        app = {"spec": app_spec, "status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
        mock_get = MagicMock(return_value=_Resp(200, app))
        mock_put = MagicMock(return_value=_Resp(200))
        mock_post = MagicMock(return_value=_Resp(200))
        with (
            patch("requests.get", mock_get),
            patch("requests.put", mock_put),
            patch("requests.post", mock_post),
            patch("time.sleep", return_value=None),
        ):
            result = ArgoCDDeployer().deploy(self._req(), image, "group/app", "argocd-host", pwd="token")
        return result, mock_put.call_args.kwargs.get("json")

    def test_helm_branch_uses_last_colon_for_tag(self):
        result, patch_body = self._deploy("hub.example.com/repo/app:v1.0", {"source": {"helm": {"parameters": []}}})
        params = patch_body["spec"]["source"]["helm"]["parameters"]
        self.assertEqual({p["name"]: p["value"] for p in params}["image.tag"], "v1.0")
        self.assertTrue(result["success"])

    def test_kustomize_ported_registry_rsplit(self):
        # 修复后：registry 带端口时 newName/newTag 用 rsplit 正确切分
        _, patch_body = self._deploy("hub.example.com:5000/repo/app:v1.0", {"source": {"kustomize": {"images": []}}})
        images = patch_body["spec"]["source"]["kustomize"]["images"][0]
        self.assertEqual(images["newName"], "hub.example.com:5000/repo/app")
        self.assertEqual(images["newTag"], "v1.0")

    def test_stop_without_api_url_falls_back_to_host(self):
        # 修复后：DeployRequest 无 api_url 不再抛 AttributeError，回退 https://{host}
        from backend.models.requests import DeployRequest

        req = DeployRequest(project="group/app", tag="v1")
        mock_delete = MagicMock(return_value=_Resp(200))
        with patch("requests.delete", mock_delete):
            r = ArgoCDDeployer().stop(req=req, project="group/app", host="argocd-host", pwd="token")
        self.assertTrue(r["success"])
        self.assertIn("https://argocd-host", mock_delete.call_args.args[0])

    def test_stop_uses_api_url_when_provided(self):
        req = types.SimpleNamespace(api_url="https://argocd-custom:1234")
        mock_delete = MagicMock(return_value=_Resp(200))
        with patch("requests.delete", mock_delete):
            ArgoCDDeployer().stop(req=req, project="group/app", host="argocd-host", pwd="token")
        self.assertIn("https://argocd-custom:1234", mock_delete.call_args.args[0])


# ─────────────────────────────────────────────────────────────
# Flux CD 部署器（k8s_fluxcd.py）
# ─────────────────────────────────────────────────────────────
class TestFluxCD(unittest.TestCase):
    def test_discover_flux_resource_exact_match(self):
        def handler(cmd):
            if "helmrelease myapp" in cmd and "-o name" in cmd:
                return ("helmrelease.apps.toolkit.fluxcd.io/myapp", "", 0)
            return ("", "", 0)

        name, kind = _discover_flux_resource(FakeSSH(handler), "myapp", "hub.example.com/app")
        self.assertEqual((name, kind), ("myapp", "helmrelease"))

    def test_discover_flux_resource_fallback_empty(self):
        _, kind = _discover_flux_resource(FakeSSH(lambda cmd: ("", "", 0)), "myapp", "hub.example.com/app")
        self.assertEqual(kind, "")


# ─────────────────────────────────────────────────────────────
# 取消信号生命周期（deploy_run.py）
# ─────────────────────────────────────────────────────────────
class TestDeployRun(unittest.TestCase):
    def test_cancel_signal_lifecycle(self):
        from backend.deploy_run import DeployRunManager

        mgr = DeployRunManager()
        self.assertFalse(mgr.register(1).is_set())
        mgr.cancel(1)
        self.assertTrue(mgr.is_cancelled(1))
        mgr.unregister(1)
        self.assertFalse(mgr.is_cancelled(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
