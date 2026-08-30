"""K8S Argo CD 部署模式 — API patch image + sync + health polling"""

import logging

from backend.deploy_log import S
from backend.deployers.base import split_image_ref
from backend.deployers.k8s_base import K8sSubDeployer
from backend.deployers.k8s_utils import check_cancelled

logger = logging.getLogger(__name__)


class ArgoCDDeployer(K8sSubDeployer):
    """Argo CD: patch image + sync + health polling"""

    def cd_type(self) -> str:
        return "argocd"

    def stop(
        self, req, project: str, host: str, port: int = 22, user: str = "root", pwd: str = "", ssh_key: str = ""
    ) -> dict:
        """停止：删除 ArgoCD Application"""
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        token = pwd
        base = getattr(req, "api_url", "") or f"https://{host}"
        app_name = project.split("/")[-1]
        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            r = requests.delete(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
            if r.status_code in (200, 204):
                return {"success": True, "output": f"ArgoCD application {app_name} deleted"}
            else:
                return {"success": False, "output": f"Delete failed: {r.status_code} {r.text[:200]}"}
        except Exception as ex:
            logger.error("ArgoCD stop failed", exc_info=ex)
            return {"success": False, "output": "Stop service failed, please contact administrator"}

    def rollback(self, req, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        """原生回滚：ArgoCD POST /rollback 回到上一 sync 版本（history id）。"""
        import time

        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        token = pwd  # ArgoCD token 通过 password 字段传入
        base = getattr(req, "api_url", "") or f"https://{host}"
        output = []
        success = False

        def log(msg):
            if callable(callback):
                callback(msg)
            output.append(msg)

        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # ── 发现 Argo CD Application 名（与 deploy 同源，不盲猜等于项目名） ──
            app_name = project.split("/")[-1]
            r = requests.get(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
            if r.status_code != 200:
                log(S("deploy_log.argocd_searching"))
                r_list = requests.get(f"{base}/api/v1/applications", headers=headers, timeout=10, verify=False)
                found = None
                if r_list.status_code == 200:
                    for a in r_list.json().get("items", []):
                        name = a.get("metadata", {}).get("name", "")
                        spec_str = str(a.get("spec", {}))
                        if project in spec_str or app_name in spec_str:
                            found = name
                            break
                if found:
                    app_name = found
                    log(S("deploy_log.argocd_name_diff", name=app_name))
                else:
                    msg = S("deploy_log.argocd_get_fail", code=r.status_code, msg=r.text[:200])
                    log(msg)
                    return {"success": False, "output": msg}
                r = requests.get(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
                if r.status_code != 200:
                    msg = S("deploy_log.argocd_get_fail", code=r.status_code, msg=r.text[:200])
                    log(msg)
                    return {"success": False, "output": msg}

            app = r.json()

            # ── 读 history 取上一版 revision id ──
            history = app.get("status", {}).get("history", []) or []
            sorted_history = sorted(history, key=lambda h: h.get("id", 0))
            if len(sorted_history) < 2:
                msg = S("deploy_log.argocd_no_prev_version")
                log(msg)
                return {"success": False, "output": msg}
            prev = sorted_history[-2]
            prev_id = prev.get("id")
            prev_rev = prev.get("revision", "")
            log(S("deploy_log.argocd_rollback_to", id=prev_id, revision=prev_rev))

            check_cancelled()
            r = requests.post(
                f"{base}/api/v1/applications/{app_name}/rollback",
                json={"id": prev_id},
                headers=headers,
                timeout=10,
                verify=False,
            )
            if r.status_code != 200:
                log(S("deploy_log.argocd_rollback_failed", code=r.status_code, msg=r.text[:200]))
                return {"success": False, "output": "\n".join(output)}

            # ── 复用 health 轮询等待回滚完成 ──
            health = ""
            sync = ""
            for i in range(30):
                check_cancelled()
                time.sleep(2)
                r = requests.get(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
                if r.status_code != 200:
                    log(S("deploy_log.argocd_poll_fail", code=r.status_code, msg=r.text[:200]))
                    continue
                a = r.json()
                health = a.get("status", {}).get("health", {}).get("status", "")
                sync = a.get("status", {}).get("sync", {}).get("status", "")
                log(S("deploy_log.argocd_wait", n=i + 1, total=30, health=health or "Unknown", sync=sync or "Unknown"))
                if health == "Healthy":
                    log(S("deploy_log.argocd_healthy", sync=sync))
                    success = True
                    break
            else:
                log(S("deploy_log.argocd_timeout"))

            return {"success": success, "output": "\n".join(output)}
        except Exception as e:
            logger.error("ArgoCD rollback failed", exc_info=e)
            msg = S("deploy_log.argocd_rollback_error")
            log(msg)
            return {"success": False, "output": msg}

    def deploy(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        """Argo CD: patch image + sync"""
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        import time

        token = pwd  # ArgoCD token 通过 password 字段传入
        base = getattr(req, "api_url", "") or f"https://{host}"
        image_repo, image_tag = split_image_ref(image)
        output = []
        success = False

        def log(msg):
            if callable(callback):
                callback(msg)
            output.append(msg)

        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            log(S("deploy_log.argocd_connecting"))

            # ── 发现 Argo CD Application 名，不盲猜等于项目名 ──
            app_name = project.split("/")[-1]
            r = requests.get(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
            if r.status_code != 200:
                # 精确名不存在，搜索所有 App 按镜像名匹配
                log(S("deploy_log.argocd_searching"))
                r_list = requests.get(f"{base}/api/v1/applications", headers=headers, timeout=10, verify=False)
                if r_list.status_code == 200:
                    apps = r_list.json().get("items", [])
                    found = None
                    for a in apps:
                        name = a.get("metadata", {}).get("name", "")
                        spec_str = str(a.get("spec", {}))
                        if image_repo in spec_str or project in spec_str:
                            found = name
                            break
                    if found:
                        app_name = found
                        log(S("deploy_log.argocd_name_diff", name=app_name))
                    else:
                        msg = S("deploy_log.argocd_get_fail", code=r.status_code, msg=r.text[:200])
                        log(msg)
                        return {"success": False, "output": msg}
                else:
                    msg = S("deploy_log.argocd_get_fail", code=r.status_code, msg=r.text[:200])
                    log(msg)
                    return {"success": False, "output": msg}
                # 用发现的 app_name 重新获取
                r = requests.get(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
                if r.status_code != 200:
                    msg = S("deploy_log.argocd_get_fail", code=r.status_code, msg=r.text[:200])
                    log(msg)
                    return {"success": False, "output": msg}

            app = r.json()

            log(S("deploy_log.argocd_prepare"))
            params = app.get("spec", {}).get("source", {}).get("helm", {}).get("parameters", [])
            kustomize = app.get("spec", {}).get("source", {}).get("kustomize", {})
            if kustomize:
                # 用 Application 名作为 Kustomize image name（常见约定）
                new_images = [
                    {
                        "name": app_name,
                        "newName": image_repo,
                        "newTag": image_tag or "latest",
                    }
                ]
                patch = {"spec": {"source": {"kustomize": {"images": new_images}}}}
            else:
                # Helm: set image tag parameter
                found = False
                for p in params:
                    if p.get("name") == "image.tag":
                        p["value"] = image_tag or "latest"
                        found = True
                        break
                if not found:
                    params.append({"name": "image.tag", "value": image_tag or "latest"})
                patch = {"spec": {"source": {"helm": {"parameters": params}}}}

            log(S("deploy_log.argocd_update"))
            r = requests.put(
                f"{base}/api/v1/applications/{app_name}", json=patch, headers=headers, timeout=10, verify=False
            )
            if r.status_code != 200:
                log(S("deploy_log.argocd_update_fail", code=r.status_code, msg=r.text[:200]))
                return {"success": False, "output": "\n".join(output)}
            log(S("deploy_log.argocd_update_ok"))

            log(S("deploy_log.argocd_sync"))
            r = requests.post(
                f"{base}/api/v1/applications/{app_name}/sync", json={}, headers=headers, timeout=10, verify=False
            )
            if r.status_code != 200:
                log(S("deploy_log.argocd_sync_fail", code=r.status_code, msg=r.text[:200]))
                return {"success": False, "output": "\n".join(output)}
            log(S("deploy_log.argocd_sync_ok"))

            health = ""
            sync = ""
            for i in range(30):
                check_cancelled()
                time.sleep(2)
                r = requests.get(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
                if r.status_code != 200:
                    log(S("deploy_log.argocd_poll_fail", code=r.status_code, msg=r.text[:200]))
                    continue
                a = r.json()
                health = a.get("status", {}).get("health", {}).get("status", "")
                sync = a.get("status", {}).get("sync", {}).get("status", "")
                log(S("deploy_log.argocd_wait", n=i + 1, total=30, health=health or "Unknown", sync=sync or "Unknown"))
                if health == "Healthy":
                    log(S("deploy_log.argocd_healthy", sync=sync))
                    success = True
                    break
            else:
                log(S("deploy_log.argocd_timeout"))

            return {"success": success, "output": "\n".join(output)}
        except Exception as e:
            logger.error("ArgoCD deploy failed", exc_info=e)
            msg = S("deploy_log.argocd_deploy_error")
            log(msg)
            return {"success": False, "output": msg}
