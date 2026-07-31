"""K8S kubectl 部署模式 — SSH 远程 kubectl apply + rollout restart"""

import shlex

from backend.deployers.base import ssh_connect, DeployTarget
from backend.deployers.k8s_base import K8sSubDeployer
from backend.deployers.k8s_utils import (
    _log, _kubectl_pods, _render_k8s_yaml, _get_deployment_name, _exec_exit,
)
from backend.config import settings
from backend.deploy_log import S


class KubectlDeployer(K8sSubDeployer):
    """kubectl apply + rollout restart"""

    def cd_type(self) -> str:
        return "kubectl"

    def stop(self, req, project: str, host: str, port: int = 22,
             user: str = "root", pwd: str = "", ssh_key: str = "") -> dict:
        """停止：kubectl delete -f <yaml> 或 kubectl delete deployment"""
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        if req.target_path:
            cmd = f"kubectl delete -f {req.target_path}"
        else:
            cmd = f"kubectl delete deployment/{project}"
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)
            out, err, ec = _exec_exit(ssh, cmd, timeout=settings.ssh_timeout)
            ssh.close()
            success = ec == 0 or "not found" in (err or "").lower() or "not found" in (out or "").lower()
            return {"success": success, "output": (err or out)[:settings.log_truncate_chars]}
        except Exception as ex:
            return {"success": False, "output": str(ex)}

    def deploy(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
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
                out, err, ec = _exec_exit(ssh, f"cat {shlex.quote(req.path)}")
                yaml_content = out
                if ec != 0 or not yaml_content.strip():
                    _log(callback, S("deploy_log.yaml_empty", path=req.path))
                    return {"success": False, "output": f"Remote YAML is empty or unreadable: {req.path}\n{err}"}
                _log(callback, S("deploy_log.yaml_read_ok"))

            # ── 先渲染再校验：渲染后 {IMAGE}:{TAG} 已替换，YAML 可被 safe_load 解析 ──
            yaml_content = _render_k8s_yaml(yaml_content, image, tag)
            yaml_deploy_name = _get_deployment_name(yaml_content)
            if yaml_deploy_name and yaml_deploy_name != filter_name:
                # 前端预检弹窗已确认，这里只打警告不拦截
                _log(callback, S("deploy_log.yaml_name_mismatch", yaml_name=yaml_deploy_name, project=filter_name))
            tmp = f"/tmp/k8s-{filter_name}.yaml"

            _log(callback, S("deploy_log.uploading_yaml_k8s"))
            sftp = ssh.open_sftp()
            with sftp.file(tmp, "w") as f:
                f.write(yaml_content)
            sftp.close()
            _log(callback, S("deploy_log.yaml_upload_k8s_ok"))

            deploy_name = yaml_deploy_name or filter_name  # 以 YAML 声明的 Deployment 名为准
            name_mismatch = yaml_deploy_name and yaml_deploy_name != filter_name

            deploy_log = []

            _log(callback, S("deploy_log.verifying_app"))

            before = _kubectl_pods(ssh, deploy_name)
            before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
            before_pods = set(b.split()[0] for b in before.split("\n") if b.strip()) if before else set()

            is_first_deploy = False
            if not before.strip():
                if name_mismatch:
                    # 名字不匹配时 YAML Deployment 是新的，视为首次部署
                    is_first_deploy = True
                    _log(callback, S("deploy_log.first_deploy_pod", deploy=deploy_name))
                else:
                    all_pods = _kubectl_pods(ssh, "")
                    running_pods = all_pods.strip()
                    if running_pods:
                        _log(callback, S("deploy_log.app_not_found", name=deploy_name, running=running_pods))
                        return {"success": False, "output": f"{before_text}\n\nDeploy failed: app [{deploy_name}] not found.\nRunning Pods:\n{running_pods}"}
                    else:
                        is_first_deploy = True
                        _log(callback, S("deploy_log.first_deploy_pod", deploy=deploy_name))
            else:
                _log(callback, S("deploy_log.app_verified", name=deploy_name))
            if before.strip():
                _log(callback, S("deploy_log.current_version"))
                _log(callback, before)
            else:
                _log(callback, S("deploy_log.current_version_none"))

            _log(callback, S("deploy_log.starting_deploy"))
            cmds = [f"kubectl apply -f {shlex.quote(tmp)}"]
            if not is_first_deploy:
                cmds.append(f"kubectl rollout restart deployment/{shlex.quote(deploy_name)}")
            for i, c in enumerate(cmds):
                _log(callback, S("deploy_log.exec_cmd", n=i+1, cmd=c))
                o, e, ec = _exec_exit(ssh, c)
                if o:
                    deploy_log.append(o)
                    _log(callback, o)
                if e:
                    deploy_log.append(e)
                    _log(callback, e)
                if ec != 0:
                    _log(callback, S("deploy_log.deploy_error", error=e or f"exit code {ec}"))
                    return {"success": False, "output": f"{before_text}\n\nStep {i+1} failed (exit {ec}):\n{o or e}"}

            # ── rollout status 等待部署完成 ──
            _log(callback, S("deploy_log.waiting_pod"))
            rollout_cmd = f"kubectl rollout status deployment/{shlex.quote(deploy_name)} --timeout={settings.k8s_rollout_timeout}s"
            rollout_out, rollout_err, rollout_ec = _exec_exit(ssh, rollout_cmd, timeout=settings.k8s_rollout_timeout + 30)
            rollout_output = (rollout_out or rollout_err or "").strip()
            if rollout_out:
                _log(callback, rollout_out)
            if rollout_err:
                _log(callback, rollout_err)

            # ── 部署后：查当前 Pod，排除旧 Pod ──
            _log(callback, S("deploy_log.after_version"))
            all_after = _kubectl_pods(ssh, deploy_name)
            if all_after.strip():
                after_pods = [l for l in all_after.split("\n") if l.strip() and l.split()[0] not in before_pods]
                after = "\n".join(after_pods) if after_pods else all_after
            else:
                after = all_after
            _log(callback, after)

            # ── 成败判断：rollout status exit code ──
            is_ok = rollout_ec == 0
            if is_ok:
                running_count = sum(1 for l in after.split("\n") if "Running" in l)
                _log(callback, S("deploy_log.verify_ok"))
                result = (
                    f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log)
                    + f"\n\n{rollout_output}\n\n部署后运行版本:\n{after}"
                    + f"\n\n已部署: {running_count} 个 Running Pod\n\n验证部署: ✅ 部署成功！"
                )
            else:
                _log(callback, S("deploy_log.verify_fail_timeout"))
                result = (
                    f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log)
                    + f"\n\n{rollout_output}\n\n部署后运行版本:\n{after}"
                    + f"\n\n验证部署: ❌ 部署失败！"
                )

            return {"success": is_ok, "output": result[:settings.log_truncate_chars]}
        except Exception as e:
            _log(callback, S("deploy_log.deploy_error", error=str(e)))
            return {"success": False, "output": str(e)}
        finally:
            if ssh:
                ssh.close()
