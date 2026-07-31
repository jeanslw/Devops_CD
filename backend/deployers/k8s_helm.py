"""K8S Helm 部署模式 — SSH helm upgrade --install + exit code 判断"""

from backend.deployers.base import ssh_connect, DeployTarget
from backend.deployers.k8s_base import K8sSubDeployer
from backend.deployers.k8s_utils import _log, _ssh_cmd, _kubectl_pods
from backend.config import settings
from backend.deploy_log import S


def _exec_exit(ssh, cmd: str, timeout: int = 130) -> tuple:
    """执行 SSH 命令并返回 (stdout, stderr, exit_code)"""
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    ec = stdout.channel.recv_exit_status()
    return out, err, ec


class HelmDeployer(K8sSubDeployer):
    """Helm: helm upgrade --install，信任 --wait + exit code"""

    def cd_type(self) -> str:
        return "helm"

    def stop(self, req, project: str, host: str, port: int = 22,
             user: str = "root", pwd: str = "", ssh_key: str = "") -> dict:
        """停止：helm uninstall"""
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        ns = req.k8s_ns
        ns_flag = f" -n {ns}" if ns else ""
        cmd = f"helm uninstall {project}{ns_flag}"
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)
            out, err, ec = _exec_exit(ssh, cmd, timeout=settings.ssh_timeout)
            ssh.close()
            success = ec == 0 or "release: not found" in (err or "")
            return {"success": success, "output": (out or err)[:settings.log_truncate_chars]}
        except Exception as ex:
            return {"success": False, "output": str(ex)}

    def deploy(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        tag = req.tag
        if not req.path:
            return {"success": False, "output": "Helm deploy requires a chart path or repo reference"}
        chart = req.path
        ns = req.k8s_ns
        ns_flag = f" -n {ns}" if ns else ""

        try:
            _log(callback, S("deploy_log.helm_connecting"))
            ssh = ssh_connect(target, settings.ssh_timeout)

            # ── Release 名：直接用项目短名 ──
            helm_release = project.split("/")[-1]
            existing = _ssh_cmd(ssh, f"helm list -q{ns_flag} 2>/dev/null") or ""
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
            _log(callback, S("deploy_log.helm_start"))
            helm_cmd = (
                f"helm upgrade --install {helm_release} {chart} "
                f"--set image.tag={tag} --set image.repository={image.split(':')[0]}"
                f"{ns_flag} --wait --timeout 120s"
            )
            _log(callback, S("deploy_log.helm_cmd", cmd=helm_cmd))
            helm_out, helm_err, exit_code = _exec_exit(ssh, helm_cmd)
            if helm_out:
                _log(callback, helm_out)
            if helm_err:
                _log(callback, helm_err)

            # ── 部署后状态 ──
            _log(callback, S("deploy_log.helm_getting_after"))
            after = _kubectl_pods(ssh, helm_release)
            if after.strip():
                _log(callback, after)
            status_out = _ssh_cmd(ssh, f"helm status {helm_release}{ns_flag} 2>/dev/null") or ""
            ssh.close()

            # ── 成败判断：helm --wait 的 exit code ──
            if exit_code != 0:
                _log(callback, S("deploy_log.helm_fail", error=helm_err or f"exit code {exit_code}"))
                return {
                    "success": False,
                    "output": (
                        f"Helm upgrade failed (exit {exit_code}):\n{helm_err or helm_out}"
                        f"\n\nPod status:\n{after or '(none)'}"
                        f"\n\n{status_out}"
                    )[:settings.log_truncate_chars],
                }

            _log(callback, S("deploy_log.verify_ok"))
            return {
                "success": True,
                "output": (
                    f"{status_out}\n\nPod status:\n{after or '(none)'}"
                )[:settings.log_truncate_chars],
            }
        except Exception as e:
            _log(callback, S("deploy_log.helm_fail", error=str(e)))
            return {"success": False, "output": str(e)}
