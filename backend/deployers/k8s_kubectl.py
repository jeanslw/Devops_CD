"""K8S kubectl 部署模式 — SSH 远程 kubectl apply + rollout restart"""

from backend.deployers.base import ssh_connect, DeployTarget
from backend.deployers.k8s_utils import (
    _log, _kubectl_pods, _render_k8s_yaml, _poll_k8s_pods, _get_deployment_name_from_yaml,
)
from backend.config import settings
from backend.deploy_log import S


def deploy_kubectl(req, image, project, host, port, user, pwd, ssh_key="", callback=None):
    target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)

    tag = req.tag
    filter_name = project.split("/")[-1]

    yaml_content = ""
    ssh = None
    if not req.path:
        _log(callback, S("deploy_log.path_required"))
        return {"success": False, "output": "kubectl mode requires YAML path or URL"}

    if req.path.startswith("http"):
        _log(callback, S("deploy_log.downloading_yaml"))
        import requests
        r = requests.get(req.path, timeout=10)
        if r.status_code != 200:
            _log(callback, S("deploy_log.yaml_fetch_fail", path=req.path))
            return {"success": False, "output": f"Failed to fetch remote YAML: {req.path}"}
        yaml_content = r.text
        _log(callback, S("deploy_log.yaml_download_ok"))

    # 单次 SSH 连接：读取（本地文件）、上传、部署全部共用
    ssh = ssh_connect(target, settings.ssh_timeout)
    try:
        if not req.path.startswith("http"):
            _log(callback, S("deploy_log.reading_yaml"))
            _, stdout, _ = ssh.exec_command(f"cat {req.path}")
            yaml_content = stdout.read().decode()
            if not yaml_content.strip():
                _log(callback, S("deploy_log.yaml_empty", path=req.path))
                return {"success": False, "output": f"Remote YAML is empty: {req.path}"}
            _log(callback, S("deploy_log.yaml_read_ok"))

        yaml_content = _render_k8s_yaml(yaml_content, image, tag)
        tmp = f"/tmp/k8s-{filter_name}.yaml"

        _log(callback, S("deploy_log.uploading_yaml_k8s"))
        sftp = ssh.open_sftp()
        with sftp.file(tmp, "w") as f:
            f.write(yaml_content)
        sftp.close()
        _log(callback, S("deploy_log.yaml_upload_k8s_ok"))

        deploy_log = []

        # ── 从 YAML 中提取实际 Deployment 名称 ──
        actual_deploy = _get_deployment_name_from_yaml(ssh, tmp, filter_name)
        deploy_name = actual_deploy or filter_name

        before = _kubectl_pods(ssh, deploy_name)
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
        before_pods = set(b.split()[0] for b in before.split("\n") if b.strip()) if before else set()

        _log(callback, S("deploy_log.verifying_app"))

        # ── 校验项目名与 YAML 部署名严格相等 ──
        if filter_name != deploy_name:
            _log(callback, S("deploy_log.project_yaml_mismatch", project=filter_name, deploy=deploy_name))
            _log(callback, S("deploy_log.check_yaml_path", project=filter_name))
            return {"success": False, "output": f"项目 [{filter_name}] 与 YAML 部署名 [{deploy_name}] 不匹配，请检查 YAML 路径。"}

        is_first_deploy = False
        if not before.strip():
            all_pods = _kubectl_pods(ssh, "")
            running_pods = all_pods.strip()
            if running_pods:
                _log(callback, S("deploy_log.app_not_found", name=deploy_name, running=running_pods))
                return {"success": False, "output": f"{before_text}\n\n部署失败：未找到应用 [{deploy_name}]，当前运行的 Pod：\n{running_pods}"}
            else:
                is_first_deploy = True
                _log(callback, S("deploy_log.first_deploy_pod", deploy=deploy_name))
        else:
            _log(callback, S("deploy_log.app_verified", name=deploy_name))
        _log(callback, before_text)

        _log(callback, S("deploy_log.starting_deploy"))
        cmds = [f"kubectl apply -f {tmp}"]
        if not is_first_deploy:
            cmds.append(f"kubectl rollout restart deployment/{deploy_name}")
        for i, c in enumerate(cmds):
            _log(callback, S("deploy_log.exec_cmd", n=i+1, cmd=c))
            _, stdout, stderr = ssh.exec_command(c)
            o = stdout.read().decode().strip()
            e = stderr.read().decode().strip()
            if o:
                deploy_log.append(o)
                _log(callback, o)
            elif e:
                deploy_log.append(e)
                _log(callback, e)

        _log(callback, S("deploy_log.waiting_pod"))
        _, stdout, _ = ssh.exec_command(f"kubectl get deployment/{deploy_name} -o jsonpath='{{.spec.replicas}}' 2>/dev/null || echo 1")
        expected_replicas = int(stdout.read().decode().strip() or "1")

        poll_result = _poll_k8s_pods(ssh, deploy_name, image, expected_replicas, before_pods=before_pods)
        after = poll_result["after"]

        wait_text = f"轮询耗时: {poll_result['elapsed']}s, 最大等待: {poll_result['max_wait_seconds']}s"
        status_text = f"已部署: {poll_result['correct_ready']}/{expected_replicas} 个正确版本 Pod"
        pod_summary = "\n".join(poll_result["pod_details"]) if poll_result["pod_details"] else after

        if poll_result["all_ready"]:
            _log(callback, S("deploy_log.pod_started"))
            result = (
                f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log)
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}\n\n验证部署: ✅ 部署成功！"
            )
            _log(callback, S("deploy_log.after_version"))
            _log(callback, after)
            _log(callback, S("deploy_log.verify_ok"))
        elif poll_result["has_failed"]:
            _log(callback, S("deploy_log.pod_failed"))
            result = (
                f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log)
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}\n\n错误 Pod:\n" + "\n".join(poll_result["pod_errors"])
                + f"\n\n验证部署: ❌ 部署失败！(Pod 状态异常)"
            )
            _log(callback, S("deploy_log.after_version"))
            _log(callback, after)
            _log(callback, S("deploy_log.verify_fail_pod"))
        else:
            _log(callback, S("deploy_log.pod_failed"))
            result = (
                f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log)
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}\n\nPod 状态:\n{pod_summary}"
                + f"\n\n验证部署: ❌ 部署失败！(超时未就绪)"
            )
            _log(callback, S("deploy_log.after_version"))
            _log(callback, after)
            _log(callback, S("deploy_log.verify_fail_timeout"))

        return {"success": poll_result["all_ready"], "output": result[:settings.log_truncate_chars]}
    except Exception as e:
        _log(callback, S("deploy_log.deploy_error", error=str(e)))
        return {"success": False, "output": str(e)}
    finally:
        if ssh:
            ssh.close()
