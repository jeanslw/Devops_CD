"""Kubernetes 部署器

优先级：
1. kubectl apply -f {path}  — 远程 YAML 文件（生产推荐）
2. kubectl set image       — 直接改镜像版本（快速迭代）

namespace 从 YAML 中解析，前端不提供 namespace 输入框。
"""

import logging
import shlex

from backend.config import settings
from backend.deploy_log import S
from backend.deployers.k8s_utils import _kubectl_pods

from .base import Deployer, DeployResult, DeployTarget, _exec_on, ssh_session

logger = logging.getLogger(__name__)


def _get_yaml_metadata(ssh, yaml_path):
    """从 YAML 文件提取 Deployment 的 name + namespace。
    namespace 未声明时返回空串，调用方不传 -n，由 kubectl context 决定。"""
    _, stdout, _ = ssh.exec_command(
        f"kubectl get -f {shlex.quote(yaml_path)} "
        f'-o jsonpath=\'{{.items[?(@.kind=="Deployment")].metadata.name}} {{{{.items[?(@.kind=="Deployment")].metadata.namespace}}}}\' '
        f"2>/dev/null"
    )
    raw = stdout.read().decode().strip()
    if not raw:
        return "", ""
    parts = raw.split(None, 1)  # name namespace
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


class K8sDeployer(Deployer):
    """SSH 到 K8s 节点，优先 kubectl apply，兜底 kubectl set image"""

    def name(self) -> str:
        return "k8s"

    def deploy(
        self,
        target: DeployTarget,
        image: str,
        project: str,
        _tag: str,
        callback=None,
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="Missing K8s node host")

        deployment_name = target.options.get("deployment", project)
        container_name = target.options.get("container", project)
        filter_name = project.split("/")[-1]

        try:
            with ssh_session(target, settings.ssh_timeout) as ssh:
                # ── 从 YAML 提取 Deployment 名 + namespace ──
                if target.path:
                    deploy_name, namespace = _get_yaml_metadata(ssh, target.path)
                    if not deploy_name:
                        return DeployResult(
                            image=image,
                            status="failed",
                            output=f"YAML [{target.path}] 中未找到 Deployment 定义",
                        )
                    if filter_name != deploy_name:
                        # 前端预检弹窗已确认，这里只打警告不拦截
                        self._log(
                            callback, S("deploy_log.yaml_name_mismatch", yaml_name=deploy_name, project=filter_name)
                        )
                else:
                    deploy_name = deployment_name
                    namespace = ""  # 没 YAML，留空不传 -n
                    # 无 YAML → 必须先有 Deployment 才允许 set image
                    check_name = deploy_name
                    _, check_stdout, _ = ssh.exec_command(
                        f"kubectl get deployment/{shlex.quote(check_name)} {f'-n {namespace}' if namespace else ''} -o name 2>/dev/null".strip()
                    )
                    if not check_stdout.read().decode().strip():
                        return DeployResult(
                            image=image,
                            status="failed",
                            output=f"kubectl set image 需要集群中已存在 Deployment [{deploy_name}]，"
                            + "当前未找到。请先用 YAML 方式首次部署。",
                        )

                ns_flag = f"-n {namespace}" if namespace else ""

                # ── 部署前：当前运行版本 ──
                self._log(callback, S("deploy_log.current_version"))
                before = _kubectl_pods(ssh, deploy_name, namespace)
                before_text = f"当前运行版本:\n{before}" if before.strip() else "当前运行版本: (无)"
                before_pods = {b.split()[0] for b in before.split("\n") if b.strip()} if before else set()

                if before.strip():
                    self._log(callback, before)
                else:
                    self._log(callback, S("deploy_log.current_version_none"))

                self._log(callback, S("deploy_log.starting_deploy"))

                # ── 执行部署 ──
                if target.path:
                    deploy_cmds = [f"kubectl apply -f {shlex.quote(target.path)}"]
                else:
                    deploy_cmds = [
                        f"kubectl set image deployment/{shlex.quote(deploy_name)} {shlex.quote(container_name)}={shlex.quote(image)} {ns_flag}".strip()
                    ]

                deploy_log = []
                for i, c in enumerate(deploy_cmds):
                    self._log(callback, S("deploy_log.exec_cmd", n=i + 1, cmd=c))
                    o, e, _ = _exec_on(ssh, c)
                    if o:
                        deploy_log.append(o)
                        self._log(callback, o)
                    elif e:
                        deploy_log.append(e)
                        self._log(callback, e)

                # ── rollout status 等待部署完成 ──
                self._log(callback, S("deploy_log.waiting_pod"))
                rollout_cmd = (
                    f"kubectl rollout status deployment/{shlex.quote(deploy_name)} {ns_flag} --timeout=120s".strip()
                )
                rollout_out, rollout_err, _ = _exec_on(ssh, rollout_cmd)
                rollout_output = (rollout_out or rollout_err or "").strip()
                if rollout_out:
                    self._log(callback, rollout_out)
                elif rollout_err:
                    self._log(callback, rollout_err)

                # ── 部署后：查当前 Pod，排除旧 Pod，只看新的 ──
                self._log(callback, S("deploy_log.after_version"))
                all_after = _kubectl_pods(ssh, deploy_name, namespace)
                if all_after.strip():
                    after_pods = [
                        line for line in all_after.split("\n") if line.strip() and line.split()[0] not in before_pods
                    ]
                    after = "\n".join(after_pods) if after_pods else all_after
                else:
                    after = all_after
                self._log(callback, after)

                # ── 成败判断：rollout status 输出为准 ──
                is_ok = "successfully rolled out" in rollout_output
                if is_ok:
                    running_count = sum(1 for line in after.split("\n") if "Running" in line)
                    self._log(callback, S("deploy_log.verify_ok"))
                    output = (
                        f"{before_text}\n\n开始部署:\n"
                        + "\n".join(deploy_log)
                        + f"\n\n{rollout_output}\n\n部署后运行版本:\n{after}"
                        + f"\n\n已部署: {running_count} 个 Running Pod\n\n验证部署: ✅ 部署成功！"
                    )
                    return DeployResult(image=image, status="ok", output=output[: settings.log_truncate_chars])
                else:
                    self._log(callback, S("deploy_log.verify_fail_timeout"))
                    output = (
                        f"{before_text}\n\n开始部署:\n"
                        + "\n".join(deploy_log)
                        + f"\n\n{rollout_output}\n\n部署后运行版本:\n{after}"
                        + "\n\n验证部署: ❌ 部署失败！"
                    )
                    return DeployResult(image=image, status="failed", output=output[: settings.log_truncate_chars])
        except Exception as e:
            logger.error("K8s deploy failed", exc_info=e)
            self._log(callback, S("deploy_log.k8s_fail_error", error=str(e)))
            return DeployResult(image=image, status="failed", output=str(e))

    def stop(self, target: DeployTarget, project: str, **kwargs) -> dict:
        """停止服务：kubectl delete deployment"""
        namespace = kwargs.get("k8s_ns", "default")
        cmd = f"kubectl delete deployment/{shlex.quote(project)} -n {shlex.quote(namespace)}"
        try:
            with ssh_session(target, settings.ssh_timeout) as ssh:
                _, stdout, stderr = ssh.exec_command(cmd, timeout=settings.ssh_timeout)
                out = stdout.read().decode(errors="replace").strip()
                err = stderr.read().decode(errors="replace").strip()
                exit_code = stdout.channel.recv_exit_status()
                if exit_code != 0:
                    return {
                        "success": False,
                        "output": f"kubectl delete failed (exit {exit_code}): {(err or out)[: settings.log_truncate_chars]}",
                    }
                return {"success": True, "output": (err or out)[: settings.log_truncate_chars]}
        except Exception as ex:
            logger.error("K8s stop failed", exc_info=ex)
            return {"success": False, "output": "Stop service failed, please contact administrator"}

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "K8s node host is required"
        return None

    def supports(self, deploy_type: str) -> bool:
        return deploy_type in ("k8s", "kubernetes")
