"""K8S 部署共享工具函数 — SSH 命令执行 + Pod 轮询 + YAML 渲染"""

from backend.config import settings
from backend.deploy_log import S


def _ssh_cmd(ssh, cmd):
    """执行 SSH 命令，返回 stdout+stderr 合并字符串"""
    _, stdout, stderr = ssh.exec_command(cmd)
    o = stdout.read().decode().strip()
    e = stderr.read().decode().strip()
    return o or e


def _kubectl_pods(ssh, deploy_name=""):
    """获取 K8S pod 列表，按 Deployment 名前缀匹配，不盲猜子串"""
    cmd = (
        "kubectl get pods -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[*].image,"
        "STATUS:.status.phase,REASON:.status.reason,DELETING:.metadata.deletionTimestamp --no-headers 2>/dev/null"
    )
    if deploy_name:
        # 前缀匹配：K8s Pod 命名规则 = {deploy}-{rs-hash}-{pod-hash}
        # grep "^{name}-" 避免 app 误匹配 app-backend
        cmd += f" | grep -E '^{deploy_name}-[a-f0-9]'"
    return _ssh_cmd(ssh, cmd)


def _parse_pod_line(line: str) -> dict | None:
    parts = line.split()
    if not parts:
        return None

    name = parts[0]
    if len(parts) < 4:
        return None

    deleting = parts[-1] if len(parts) > 4 else ""
    reason = parts[-2] if len(parts) > 3 else ""
    status = parts[-3] if len(parts) > 2 else ""
    image = " ".join(parts[1:-3]) if len(parts) > 4 else ""

    if deleting and deleting not in {"<none>", "None", "null", ""}:
        status = "Terminating"
        reason = deleting
    elif reason in {"<none>", "None", "null", ""}:
        reason = ""

    return {
        "name": name,
        "image": image,
        "status": status,
        "reason": reason,
    }


def _render_k8s_yaml(yaml_content: str, image: str, tag: str) -> str:
    image_parts = image.rsplit(":", 1)
    full_image_name = image_parts[0]
    reg = settings.harbor_registry
    # 剥离原始 registry，只保留镜像路径（如 mycode/diagnosis-runtime）
    if reg and full_image_name.startswith(reg + "/"):
        image_name = full_image_name[len(reg) + 1:]
    else:
        image_name = full_image_name
    # 统一以 .env 的 registry 拼接最终镜像地址
    final_image = f"{reg}/{image_name}:{tag}" if reg else f"{image_name}:{tag}"
    final_image_name = f"{reg}/{image_name}" if reg else image_name

    if "{IMAGE}:{TAG}" in yaml_content:
        yaml_content = yaml_content.replace("{IMAGE}:{TAG}", final_image)
    # 注意：{IMAGE} 顺序必须在 {IMAGE_NAME} 之前，避免误替换
    return yaml_content.replace("{IMAGE}", final_image).replace("{IMAGE_NAME}", final_image_name).replace("{TAG}", tag)


def _poll_k8s_pods(ssh, filter_name: str, desired_image: str, expected_replicas: int,
                   before_pods: set = None, max_wait: int = 20, interval: int = 3) -> dict:
    """轮询等待 Pod 就绪。

    before_pods 不为 None 时：以 Pod 名变更判断部署成功（新 Pod Running = 成功），
    不再依赖镜像名匹配（部署名/Pod名/镜像名三者独立）。
    before_pods 为 None 时（如 FluxCD）：回退到旧的镜像名匹配逻辑。
    """
    import time

    start_ts = time.monotonic()
    all_ready = False
    has_failed = False
    pod_details = []
    pod_errors = []
    correct_ready = 0
    after = ""
    failed_states = [
        "InvalidImageName", "ErrImagePull", "ImagePullBackOff", "CrashLoopBackOff",
        "RunContainerError", "CreateContainerError", "CreateContainerConfigError",
    ]
    use_pod_name = before_pods is not None

    for _ in range(max_wait):
        time.sleep(interval)
        after = _kubectl_pods(ssh, filter_name)
        pods = []
        for line in after.split("\n"):
            if not line.strip():
                continue
            parsed = _parse_pod_line(line)
            if parsed:
                pods.append(parsed)

        if not pods:
            continue

        correct_ready = 0
        pod_details = []
        pod_errors = []

        for pod in pods:
            image_text = pod["image"]
            status = pod["status"]
            reason = pod["reason"]
            is_new = (not use_pod_name) or (pod["name"] not in before_pods)

            # 判断正确版本：Pod 名模式只看新 Pod 是否 Running；镜像模式对比镜像名
            if use_pod_name:
                if is_new and status == "Running":
                    correct_ready += 1
            else:
                if desired_image in image_text and status == "Running":
                    correct_ready += 1

            detail = f"{pod['name']}: {image_text} | {status}" + (f" ({reason})" if reason else "")
            pod_details.append(detail)

            # 仅对新 Pod（或使用镜像模式时对所有 Pod）检测错误
            if not is_new:
                continue
            error_reason = (reason or "").strip().lower()
            normalized_status = (status or "").strip().lower()
            is_true_failure = normalized_status in {"failed", "unknown", "terminating"}
            is_image_failure = any(fs.lower() in error_reason for fs in failed_states)
            if is_true_failure or is_image_failure:
                pod_errors.append(detail)

        if pod_errors:
            has_failed = True
            break
        if correct_ready >= expected_replicas:
            all_ready = True
            break

    elapsed = int(time.monotonic() - start_ts)
    return {
        "all_ready": all_ready,
        "has_failed": has_failed,
        "correct_ready": correct_ready,
        "pod_details": pod_details,
        "pod_errors": pod_errors,
        "after": after,
        "elapsed": elapsed,
        "max_wait_seconds": max_wait * interval,
    }


def _get_deployment_name_from_yaml(ssh, yaml_path, fallback=""):
    """从 YAML 文件中提取第一个 Deployment 名称，不盲猜等于项目名"""
    _, stdout, _ = ssh.exec_command(
        f"kubectl get -f {yaml_path} -o jsonpath='{{.items[?(@.kind==\"Deployment\")].metadata.name}}' 2>/dev/null"
    )
    raw = stdout.read().decode().strip()
    names = [n for n in raw.split() if n]
    return names[0] if names else fallback


def _log(callback, message):
    if callable(callback):
        callback(message)
