"""Mock 测试 deploy_fluxcd — 无需真实集群、Git 仓库、Helm Chart"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock

from app.routers.k8s_deploy import deploy_fluxcd, K8sDeployRequest


# ── 工具函数 ──────────────────────────────────────────────

def _make_req(tag="v2.3.2"):
    return K8sDeployRequest(
        project="devops-glue",
        tag=tag,
        cd_type="fluxcd",
        cluster_id=1,
    )


def _make_mock_ssh(responses: dict):
    """构造 mock SSH，支持单关键字 (str) 或多关键字 AND 匹配 (tuple)"""
    mock = MagicMock()

    def side_effect(cmd):
        stdout_mock = MagicMock()
        stderr_mock = MagicMock()
        for keywords, (out, err) in responses.items():
            if isinstance(keywords, tuple):
                if all(k in cmd for k in keywords):
                    stdout_mock.read.return_value = out.encode() if isinstance(out, str) else out
                    stderr_mock.read.return_value = err.encode() if isinstance(err, str) else err
                    return MagicMock(), stdout_mock, stderr_mock
            elif keywords in cmd:
                stdout_mock.read.return_value = out.encode() if isinstance(out, str) else out
                stderr_mock.read.return_value = err.encode() if isinstance(err, str) else err
                return MagicMock(), stdout_mock, stderr_mock
        # 默认返回空
        stdout_mock.read.return_value = b""
        stderr_mock.read.return_value = b""
        return MagicMock(), stdout_mock, stderr_mock

    mock.exec_command.side_effect = side_effect
    return mock


# ── 公共 responses ────────────────────────────────────────

BASE_RESPONSES = {
    # kubectl get pods (before) — 旧版本 v2.3.1
    "custom-columns=NAME": (
        "devops-glue-6c8dc8759f-dmnkj   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>",
        "",
    ),
    # kubectl get pods (after 新 Pod) — 新版本 v2.3.2
    "custom-columns=NAME": (  # 同一个关键字，由侧效应控制不同轮次
        "devops-glue-7f3b8c9d6-xyzab   hub.abc.com/mycode/devops-glue:v2.3.2   Running   <none>   <none>",
        "",
    ),
    # kubectl get deployment -o name
    "get deploy -o name": ("deployment.apps/devops-glue", ""),
    # kubectl rollout status
    "rollout status": ('deployment "devops-glue" successfully rolled out', ""),
    # kubectl get deployment replicas
    "replicas": ("1", ""),
}


# ── 测试用例 ──────────────────────────────────────────────

class TestFluxcdResourceNotFound:
    """资源不存在 → 秒级返回失败"""

    def test_both_missing(self):
        req = _make_req()
        mock_ssh = _make_mock_ssh({
            "get helmrelease": ("", ""),
            "get kustomization": ("", ""),
        })

        with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock_ssh):
            result = deploy_fluxcd(req, "hub.abc.com/mycode/devops-glue:v2.3.2",
                                   "devops-glue", "10.0.0.1", "pass")

        assert result["success"] is False
        assert "未找到引用镜像" in result["output"]
        mock_ssh.close.assert_called_once()  # 用完后关闭了连接


class TestFluxcdHelmReleaseChartNotFound:
    """HelmRelease 找不到 chart → 轮询中检测到错误，提前退出"""

    def test_artifact_failed(self):
        req = _make_req()
        mock_ssh = _make_mock_ssh({
            # 前置检查：资源存在（用 AND 匹配避免误匹配 _check_flux_error 的 jsonpath 命令）
            ("get helmrelease", "-o name"): ("helmrelease.helm.toolkit.fluxcd.io/devops-glue", ""),
            # 前置检查不再查 kustomization（已命中 helmrelease）
            # 部署前 pod 检查
            "custom-columns=NAME": (
                "devops-glue-6c8dc8759f-dmnkj   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>",
                "",
            ),
            # patch
            "patch helmrelease": ("helmrelease.helm.toolkit.fluxcd.io/devops-glue patched", ""),
            # annotate
            "annotate helmrelease": ("helmrelease.helm.toolkit.fluxcd.io/devops-glue annotated", ""),
            # Flux 状态检查 — 第 4 轮返回错误
            "Ready": ("False|ArtifactFailed|HelmChart 'flux-system/devops-glue' is not ready: no chart found", ""),
        })

        with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock_ssh):
            with patch("time.sleep"):
                result = deploy_fluxcd(req, "hub.abc.com/mycode/devops-glue:v2.3.2",
                                       "devops-glue", "10.0.0.1", "pass")

        assert result["success"] is False
        assert "ArtifactFailed" in result["output"]
        assert "no chart found" in result["output"].lower() or "chart" in result["output"].lower()
        mock_ssh.close.assert_called_once()


class TestFluxcdKustomizationBuildFailed:
    """Kustomization 找不到 kustomization.yaml → 提前退出"""

    def test_build_failed(self):
        req = _make_req()
        mock_ssh = _make_mock_ssh({
            ("get helmrelease", "-o name"): ("", ""),
            ("get kustomization", "-o name"): ("kustomization.kustomize.toolkit.fluxcd.io/devops-glue", ""),
            "custom-columns=NAME": (
                "devops-glue-6c8dc8759f-dmnkj   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>",
                "",
            ),
            "patch kustomization": ("kustomization.kustomize.toolkit.fluxcd.io/devops-glue patched", ""),
            "annotate kustomization": ("kustomization.kustomize.toolkit.fluxcd.io/devops-glue annotated", ""),
            # 第 4 轮后 Flux 报 BuildFailed
            "Ready": ("False|BuildFailed|kustomization.yaml not found in repository", ""),
        })

        with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock_ssh):
            with patch("time.sleep"):
                result = deploy_fluxcd(req, "hub.abc.com/mycode/devops-glue:v2.3.2",
                                       "devops-glue", "10.0.0.1", "pass")

        assert result["success"] is False
        assert "BuildFailed" in result["output"] or "kustomization.yaml" in result["output"]


class TestFluxcdSuccess:
    """正常部署成功：新 Pod 出现 → rollout 完成 → 验证通过"""

    def test_full_success(self):
        req = _make_req()
        callback_logs = []

        # 用 side_effect 控制不同轮次的产出
        call_count = {"pods": 0, "ready": 0}

        def exec_side(cmd):
            mock_out = MagicMock()
            mock_err = MagicMock()

            if "custom-columns=NAME" in cmd:
                call_count["pods"] += 1
                if call_count["pods"] == 1:
                    # 第 1 次：旧 Pod v2.3.1
                    mock_out.read.return_value = (
                        b"devops-glue-6c8dc8759f-dmnkj   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>"
                    )
                elif call_count["pods"] == 2:
                    # 第 2 次：新 Pod v2.3.2 出现（Flux 已反应）
                    mock_out.read.return_value = (
                        b"devops-glue-7f3b8c9d6-xyzab   hub.abc.com/mycode/devops-glue:v2.3.2   Running   <none>   <none>\n"
                        b"devops-glue-6c8dc8759f-dmnkj   hub.abc.com/mycode/devops-glue:v2.3.1   Terminating   <none>   <none>"
                    )
                elif call_count["pods"] >= 3:
                    # 之后的轮次：只有新 Pod Running
                    mock_out.read.return_value = (
                        b"devops-glue-7f3b8c9d6-xyzab   hub.abc.com/mycode/devops-glue:v2.3.2   Running   <none>   <none>"
                    )

            elif "get helmrelease" in cmd and "-o name" in cmd:
                mock_out.read.return_value = b"helmrelease.helm.toolkit.fluxcd.io/devops-glue"
            elif "get kustomization" in cmd and "-o name" in cmd:
                mock_out.read.return_value = b""
            elif "Ready" in cmd and "jsonpath" in cmd:
                call_count["ready"] += 1
                # 前几次不报错 / 协调中
                if call_count["ready"] < 3:
                    mock_out.read.return_value = b""
                else:
                    mock_out.read.return_value = b"True||"
            elif "patch helmrelease" in cmd:
                mock_out.read.return_value = b"helmrelease.helm.toolkit.fluxcd.io/devops-glue patched"
            elif "annotate helmrelease" in cmd:
                mock_out.read.return_value = b"helmrelease.helm.toolkit.fluxcd.io/devops-glue annotated"
            elif "get deploy -o name" in cmd:
                mock_out.read.return_value = b"deployment.apps/devops-glue"
            elif "rollout status" in cmd:
                mock_out.read.return_value = b'deployment "devops-glue" successfully rolled out'
            elif "replicas" in cmd:
                mock_out.read.return_value = b"1"

            mock_err.read.return_value = b""
            return MagicMock(), mock_out, mock_err

        mock_ssh = MagicMock()
        mock_ssh.exec_command.side_effect = exec_side

        with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock_ssh):
            with patch("time.sleep"):
                result = deploy_fluxcd(
                    req,
                    "hub.abc.com/mycode/devops-glue:v2.3.2",
                    "devops-glue",
                    "10.0.0.1",
                    "pass",
                    callback=lambda msg: callback_logs.append(msg),
                )

        assert result["success"] is True
        assert "部署成功" in result["output"]
        mock_ssh.close.assert_called_once()

        # 验证 callback 收到了日志
        assert any("检测到 Flux 资源" in m for m in callback_logs)
        assert any("开始部署" in m for m in callback_logs)


class TestFluxcdCallback:
    """验证 callback 流式日志正常工作"""

    def test_callback_receives_logs(self):
        req = _make_req()
        logs = []
        mock_ssh = _make_mock_ssh({
            ("get helmrelease", "-o name"): ("helmrelease.helm.toolkit.fluxcd.io/devops-glue", ""),
            "custom-columns=NAME": ("devops-glue-6c8dc8759f-dmnkj   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>", ""),
            "patch helmrelease": ("helmrelease... patched", ""),
            "annotate helmrelease": ("helmrelease... annotated", ""),
            "Ready": ("True||", ""),
            "get deploy -o name": ("deployment.apps/devops-glue", ""),
            "rollout status": ('deployment "devops-glue" successfully rolled out', ""),
            "replicas": ("1", ""),
        })

        with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock_ssh):
            with patch("time.sleep"):
                result = deploy_fluxcd(
                    req, "hub.abc.com/mycode/devops-glue:v2.3.2",
                    "devops-glue", "10.0.0.1", "pass",
                    callback=lambda msg: logs.append(msg),
                )

        # resource found → should have logged
        assert any("检测到 Flux 资源" in m for m in logs)

    def test_no_callback_does_not_crash(self):
        """不传 callback 也不应该崩溃"""
        req = _make_req()
        mock_ssh = _make_mock_ssh({
            ("get helmrelease", "-o name"): ("helmrelease.helm.toolkit.fluxcd.io/devops-glue", ""),
            "custom-columns=NAME": ("devops-glue-6c8dc8759f-dmnkj   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>", ""),
            "patch helmrelease": ("helmrelease... patched", ""),
            "annotate helmrelease": ("helmrelease... annotated", ""),
            "Ready": ("True||", ""),
            "get deploy -o name": ("deployment.apps/devops-glue", ""),
            "rollout status": ('deployment "devops-glue" successfully rolled out', ""),
            "replicas": ("1", ""),
        })

        with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock_ssh):
            with patch("time.sleep"):
                result = deploy_fluxcd(
                    req, "hub.abc.com/mycode/devops-glue:v2.3.2",
                    "devops-glue", "10.0.0.1", "pass",
                )

        assert "验证部署" in result["output"]
