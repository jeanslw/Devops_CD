"""快速验证 deploy_fluxcd mock 逻辑 — 无需 pytest，直接 python 运行"""
import sys
import os
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
from app.routers.k8s_deploy import deploy_fluxcd, K8sDeployRequest

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  {detail}")


def _make_req(tag="v2.3.2"):
    return K8sDeployRequest(project="devops-glue", tag=tag, cd_type="fluxcd", cluster_id=1)


def _make_mock_ssh(responses: dict):
    """构造 mock SSH，支持单关键字 (str) 或多关键字 AND 匹配 (tuple)"""
    mock = MagicMock()
    def side_effect(cmd):
        out_mock = MagicMock()
        err_mock = MagicMock()
        for keywords, (out, err) in responses.items():
            if isinstance(keywords, tuple):
                if all(k in cmd for k in keywords):
                    out_mock.read.return_value = out.encode() if isinstance(out, str) else out
                    err_mock.read.return_value = err.encode() if isinstance(err, str) else err
                    return MagicMock(), out_mock, err_mock
            elif keywords in cmd:
                out_mock.read.return_value = out.encode() if isinstance(out, str) else out
                err_mock.read.return_value = err.encode() if isinstance(err, str) else err
                return MagicMock(), out_mock, err_mock
        out_mock.read.return_value = b""
        err_mock.read.return_value = b""
        return MagicMock(), out_mock, err_mock
    mock.exec_command.side_effect = side_effect
    return mock


print("=" * 60)
print("deploy_fluxcd Mock 验证")
print("=" * 60)

# ── 1. 资源不存在 ──
print("\n1. 资源不存在 → 应秒级返回失败")
mock = _make_mock_ssh({"get helmrelease": ("", ""), "get kustomization": ("", "")})
with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock):
    result = deploy_fluxcd(_make_req(), "hub.abc.com/mycode/devops-glue:v2.3.2", "devops-glue", "10.0.0.1", "pass")
check("success=False", result["success"] is False, f"got {result['success']}")
check("包含'未找到引用镜像'", "未找到引用镜像" in result["output"], result["output"][:80])
check("close() 被调用", mock.close.called)

# ── 2. HelmRelease 找不到 chart ──
print("\n2. HelmRelease 找不到 chart → 轮询中检测 ArtifactFailed")
mock = _make_mock_ssh({
    ("get helmrelease", "-o name"): ("helmrelease.helm.toolkit.fluxcd.io/devops-glue", ""),
    "custom-columns=NAME": ("devops-glue-old   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>", ""),
    "patch helmrelease": ("helmrelease... patched", ""),
    "annotate helmrelease": ("helmrelease... annotated", ""),
    "Ready": ("False|ArtifactFailed|chart not found in repository", ""),
})
with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock):
    with patch("time.sleep"):
        result = deploy_fluxcd(_make_req(), "hub.abc.com/mycode/devops-glue:v2.3.2", "devops-glue", "10.0.0.1", "pass")
check("success=False", result["success"] is False, f"got {result['success']}")
check("包含 ArtifactFailed", "ArtifactFailed" in result["output"], result["output"][:120])
check("close() 被调用", mock.close.called)

# ── 3. Kustomization BuildFailed ──
print("\n3. Kustomization 找不到 kustomization.yaml → 检测 BuildFailed")
mock = _make_mock_ssh({
    ("get helmrelease", "-o name"): ("", ""),
    ("get kustomization", "-o name"): ("kustomization.kustomize.toolkit.fluxcd.io/devops-glue", ""),
    "custom-columns=NAME": ("devops-glue-old   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>", ""),
    "patch kustomization": ("kustomization... patched", ""),
    "annotate kustomization": ("kustomization... annotated", ""),
    "Ready": ("False|BuildFailed|kustomization.yaml not found", ""),
})
with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock):
    with patch("time.sleep"):
        result = deploy_fluxcd(_make_req(), "hub.abc.com/mycode/devops-glue:v2.3.2", "devops-glue", "10.0.0.1", "pass")
check("success=False", result["success"] is False, f"got {result['success']}")
check("包含 BuildFailed", "BuildFailed" in result["output"], result["output"][:120])

# ── 4. 正常部署成功 ──
print("\n4. 正常部署成功 → 新 Pod 出现 → rollout → 验证通过")
call_count = {"pods": 0, "ready": 0}

def exec_side(cmd):
    out_mock = MagicMock()
    err_mock = MagicMock()
    err_mock.read.return_value = b""

    if "custom-columns=NAME" in cmd:
        call_count["pods"] += 1
        if call_count["pods"] == 1:
            out_mock.read.return_value = b"devops-glue-old   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>"
        elif call_count["pods"] == 2:
            out_mock.read.return_value = (
                b"devops-glue-new   hub.abc.com/mycode/devops-glue:v2.3.2   Running   <none>   <none>\n"
                b"devops-glue-old   hub.abc.com/mycode/devops-glue:v2.3.1   Terminating   <none>   <none>"
            )
        else:
            out_mock.read.return_value = b"devops-glue-new   hub.abc.com/mycode/devops-glue:v2.3.2   Running   <none>   <none>"

    elif "get helmrelease" in cmd and "-o name" in cmd:
        out_mock.read.return_value = b"helmrelease.helm.toolkit.fluxcd.io/devops-glue"
    elif "get kustomization" in cmd and "-o name" in cmd:
        out_mock.read.return_value = b""
    elif "Ready" in cmd and "jsonpath" in cmd:
        call_count["ready"] += 1
        out_mock.read.return_value = b"True||" if call_count["ready"] >= 3 else b""
    elif "patch helmrelease" in cmd:
        out_mock.read.return_value = b"helmrelease... patched"
    elif "annotate helmrelease" in cmd:
        out_mock.read.return_value = b"helmrelease... annotated"
    elif "get deploy -o name" in cmd:
        out_mock.read.return_value = b"deployment.apps/devops-glue"
    elif "rollout status" in cmd:
        out_mock.read.return_value = b'deployment "devops-glue" successfully rolled out'
    elif "replicas" in cmd:
        out_mock.read.return_value = b"1"
    else:
        out_mock.read.return_value = b""

    return MagicMock(), out_mock, err_mock

mock = MagicMock()
mock.exec_command.side_effect = exec_side

logs = []
with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock):
    with patch("time.sleep"):
        result = deploy_fluxcd(
            _make_req(), "hub.abc.com/mycode/devops-glue:v2.3.2",
            "devops-glue", "10.0.0.1", "pass",
            callback=lambda msg: logs.append(msg),
        )
check("success=True", result["success"] is True, f"got {result['success']}")
check("包含'部署成功'", "部署成功" in result["output"], result["output"][:120])
check("callback 收到日志", any("检测到 Flux 资源" in m for m in logs))
check("close() 被调用", mock.close.called)

# ── 5. callback=None 不崩溃 ──
print("\n5. 不传 callback → 不应崩溃")
mock = _make_mock_ssh({
    ("get helmrelease", "-o name"): ("helmrelease.helm.toolkit.fluxcd.io/devops-glue", ""),
    "custom-columns=NAME": ("devops-glue-old   hub.abc.com/mycode/devops-glue:v2.3.1   Running   <none>   <none>", ""),
    "patch helmrelease": ("helmrelease... patched", ""),
    "annotate helmrelease": ("helmrelease... annotated", ""),
    "Ready": ("True||", ""),
    "get deploy -o name": ("deployment.apps/devops-glue", ""),
    "rollout status": ('deployment "devops-glue" successfully rolled out', ""),
    "replicas": ("1", ""),
})
with patch("app.routers.k8s_deploy.ssh_connect", return_value=mock):
    with patch("time.sleep"):
        result = deploy_fluxcd(_make_req(), "hub.abc.com/mycode/devops-glue:v2.3.2", "devops-glue", "10.0.0.1", "pass")
check("不传 callback 正常返回", "验证部署" in result["output"], result["output"][:120])

# ── 结果 ──
print("\n" + "=" * 60)
print(f"结果: {passed} 通过 / {failed} 失败 / {passed + failed} 总计")
