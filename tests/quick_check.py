"""最小验证"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
from app.routers.k8s_deploy import deploy_fluxcd, K8sDeployRequest

# 场景1: 资源不存在
print("1. 资源不存在...", end=" ")
m = MagicMock()
def resp(cmd):
    o = MagicMock(); e = MagicMock()
    if "get helmrelease" in cmd and "-o name" in cmd:
        o.read.return_value = b""
    elif "get kustomization" in cmd and "-o name" in cmd:
        o.read.return_value = b""
    e.read.return_value = b""
    return MagicMock(), o, e
m.exec_command.side_effect = resp

with patch("app.routers.k8s_deploy.ssh_connect", return_value=m):
    with patch("time.sleep"):
        r = deploy_fluxcd(
            K8sDeployRequest(project="test", tag="v1", cd_type="fluxcd", cluster_id=1),
            "img:v1", "test", "1.1.1.1", "pwd"
        )
assert r["success"] is False and "未找到引用镜像" in r["output"], f"FAIL: {r}"
print("OK")

# 场景2: HelmRelease 存在，正常走完流程不崩溃
print("2. HelmRelease 存在不崩溃...", end=" ")
m2 = MagicMock()
pod_count = [0]
def resp2(cmd):
    o = MagicMock(); e = MagicMock()
    if "get helmrelease" in cmd and "-o name" in cmd:
        o.read.return_value = b"helmrelease.helm.toolkit.fluxcd.io/test"
    elif "custom-columns=NAME" in cmd:
        pod_count[0] += 1
        if pod_count[0] >= 3:
            # 新 Pod 已 Running，旧 Pod 已消失（真实场景 rollout 完成后旧 Pod 已终止）
            o.read.return_value = b"pod2   img:v1   Running   <none>   <none>"
        else:
            o.read.return_value = b"pod1   img:v1   Running   <none>   <none>"
    elif "patch helmrelease" in cmd:
        o.read.return_value = b"patched"
    elif "annotate helmrelease" in cmd:
        o.read.return_value = b"annotated"
    elif "Ready" in cmd and "jsonpath" in cmd:
        o.read.return_value = b""
    elif "get deploy -o name" in cmd:
        o.read.return_value = b"deployment.apps/test"
    elif "rollout status" in cmd:
        o.read.return_value = b'successfully rolled out'
    elif "replicas" in cmd:
        o.read.return_value = b"1"
    e.read.return_value = b""
    return MagicMock(), o, e
m2.exec_command.side_effect = resp2

with patch("app.routers.k8s_deploy.ssh_connect", return_value=m2):
    with patch("time.sleep"):
        r = deploy_fluxcd(
            K8sDeployRequest(project="test", tag="v1", cd_type="fluxcd", cluster_id=1),
            "img:v1", "test", "1.1.1.1", "pwd"
        )
assert r["success"] is True, f"FAIL: success={r['success']}, output={r['output'][:200]}"
print("OK")

# 场景3: ArtifactFailed 提前退出
print("3. ArtifactFailed...", end=" ")
m3 = MagicMock()
def resp3(cmd):
    o = MagicMock(); e = MagicMock()
    if "get helmrelease" in cmd and "-o name" in cmd:
        o.read.return_value = b"helmrelease.helm.toolkit.fluxcd.io/test"
    elif "custom-columns=NAME" in cmd:
        o.read.return_value = b"pod1   img:v1   Running"
    elif "patch helmrelease" in cmd:
        o.read.return_value = b"patched"
    elif "annotate helmrelease" in cmd:
        o.read.return_value = b"annotated"
    elif "Ready" in cmd and "jsonpath" in cmd:
        o.read.return_value = b"False|ArtifactFailed|chart not found"
    e.read.return_value = b""
    return MagicMock(), o, e
m3.exec_command.side_effect = resp3

with patch("app.routers.k8s_deploy.ssh_connect", return_value=m3):
    with patch("time.sleep"):
        r = deploy_fluxcd(
            K8sDeployRequest(project="test", tag="v1", cd_type="fluxcd", cluster_id=1),
            "img:v1", "test", "1.1.1.1", "pwd"
        )
assert r["success"] is False and "ArtifactFailed" in r["output"], f"FAIL: {r['output'][:120]}"
print("OK")

print("\n全部通过!")
