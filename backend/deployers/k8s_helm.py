"""K8S Helm 部署模式 — SSH helm upgrade --install + exit code 判断"""

import logging
import shlex

from backend.config import settings
from backend.deploy_log import S
from backend.deployers.base import DeployTarget, split_image_ref, ssh_connect
from backend.deployers.k8s_base import K8sSubDeployer
from backend.deployers.k8s_utils import _exec_exit, _kubectl_pods, _log, check_cancelled

logger = logging.getLogger(__name__)


class HelmDeployer(K8sSubDeployer):
    """Helm: helm upgrade --install，信任 --wait + exit code"""

    def cd_type(self) -> str:
        return "helm"

    def stop(
        self, req, project: str, host: str, port: int = 22, user: str = "root", pwd: str = "", ssh_key: str = ""
    ) -> dict:
        """停止：helm uninstall（先检查 release 是否存在）"""
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        ns = req.k8s_ns
        ns_flag = f" -n {ns}" if ns else ""
        release_name = project.split("/")[-1] if "/" in project else project
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)
            # 先查 release 是否存在
            _, list_err, list_ec = _exec_exit(ssh, f"helm list -q{ns_flag}", timeout=settings.ssh_timeout)
            if list_ec != 0:
                ssh.close()
                return {"success": False, "output": f"helm list failed: {list_err or 'unknown error'}"}

            # 执行 uninstall（始终执行，让 helm 告诉我们结果）
            cmd = f"helm uninstall {shlex.quote(release_name)}{ns_flag}"
            out, err, ec = _exec_exit(ssh, cmd, timeout=settings.ssh_timeout)
            ssh.close()
            success = ec == 0
            return {"success": success, "output": (out or err)[: settings.log_truncate_chars]}
        except Exception as ex:
            logger.error("Helm stop failed", exc_info=ex)
            return {"success": False, "output": "停止服务失败，请联系管理员"}

    def deploy(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        tag = req.tag if req.tag else ""
        if not req.path:
            return {"success": False, "output": "Helm deploy requires a chart path or repo reference"}
        chart = req.path
        image_repo, _ = split_image_ref(image)
        ns = req.k8s_ns
        ns_flag = f" -n {ns}" if ns else ""

        try:
            _log(callback, S("deploy_log.helm_connecting"))
            ssh = ssh_connect(target, settings.ssh_timeout)

            # ── Release 名：直接用项目短名 ──
            helm_release = project.split("/")[-1]

            # 检查 release 是否已存在（用 _exec_exit 防 helm 挂了静默失败）
            existing, list_err, list_ec = _exec_exit(ssh, f"helm list -q{ns_flag}", timeout=settings.ssh_timeout)
            if list_ec != 0:
                _log(
                    callback,
                    S("deploy_log.helm_fail", error=f"helm list failed (exit {list_ec}): {list_err or 'no output'}"),
                )
                ssh.close()
                return {"success": False, "output": f"helm list failed (exit {list_ec}):\n{list_err or 'no output'}"}

            if helm_release in existing.split("\n"):
                _log(callback, S("deploy_log.helm_upgrading", name=helm_release))
            else:
                _log(callback, S("deploy_log.helm_installing", name=helm_release))

            # ── 部署前快照 ──
            _log(callback, S("deploy_log.helm_getting_current"))
            before = _kubectl_pods(ssh, helm_release)
            if before.strip():
                _log(callback, S("deploy_log.current_version"))
                _log(callback, before)
            else:
                _log(callback, S("deploy_log.current_version_none"))

            # ── 执行 helm upgrade --install ──
            # 所有用户输入均通过 shlex.quote() 防 shell 注入
            _log(callback, S("deploy_log.helm_start"))
            helm_cmd = (
                f"helm upgrade --install {shlex.quote(helm_release)} {shlex.quote(chart)} "
                f"--set image.tag={shlex.quote(tag)} "
                f"--set image.repository={shlex.quote(image_repo)}"
                f"{ns_flag} --wait --timeout {settings.k8s_helm_timeout}s"
            )
            _log(callback, S("deploy_log.helm_cmd", cmd=helm_cmd))
            check_cancelled()
            helm_out, helm_err, exit_code = _exec_exit(ssh, helm_cmd, timeout=settings.k8s_helm_timeout + 10)
            if helm_out:
                _log(callback, helm_out)

            # ── 部署后状态 ──
            _log(callback, S("deploy_log.helm_getting_after"))
            after = _kubectl_pods(ssh, helm_release)
            if after.strip():
                _log(callback, after)

            # helm status 可能也挂（release 没创建成功），用 _exec_exit 兜底
            status_out, status_err, status_ec = _exec_exit(
                ssh, f"helm status {shlex.quote(helm_release)}{ns_flag}", timeout=settings.ssh_timeout
            )
            ssh.close()
            if status_ec != 0:
                status_out = f"(helm status unavailable: {status_err})" if status_err else "(helm status unavailable)"

            # ── 成败判断：helm --wait 的 exit code ──
            if exit_code != 0:
                _log(callback, S("deploy_log.helm_fail", error=helm_err or f"exit code {exit_code}"))
                return {
                    "success": False,
                    "output": (
                        f"Helm upgrade failed (exit {exit_code}):\n{helm_err or helm_out}"
                        f"\n\nPod status:\n{after or '(none)'}"
                        f"\n\n{status_out}"
                    )[: settings.log_truncate_chars],
                }

            _log(callback, S("deploy_log.verify_ok"))
            return {
                "success": True,
                "output": (f"{status_out}\n\nPod status:\n{after or '(none)'}")[: settings.log_truncate_chars],
            }
        except Exception as e:
            logger.error("Helm deploy failed", exc_info=e)
            _log(callback, S("deploy_log.helm_fail", error=str(e)))
            return {"success": False, "output": str(e)}
