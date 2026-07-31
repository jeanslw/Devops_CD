"""K8S Argo CD 部署模式 — API patch image + sync + health polling"""

from backend.deployers.k8s_base import K8sSubDeployer
from backend.deploy_log import S


class ArgoCDDeployer(K8sSubDeployer):
    """Argo CD: patch image + sync + health polling"""

    def cd_type(self) -> str:
        return "argocd"

    def stop(self, req, project: str, host: str, port: int = 22,
             user: str = "root", pwd: str = "", ssh_key: str = "") -> dict:
        """停止：删除 ArgoCD Application"""
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        token = pwd
        base = req.api_url or f"https://{host}"
        app_name = project.split("/")[-1]
        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            r = requests.delete(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
            if r.status_code in (200, 204):
                return {"success": True, "output": f"ArgoCD application {app_name} deleted"}
            else:
                return {"success": False, "output": f"Delete failed: {r.status_code} {r.text[:200]}"}
        except Exception as ex:
            return {"success": False, "output": str(ex)}

    def deploy(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        """Argo CD: patch image + sync"""
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        import time

        token = pwd  # ArgoCD token 通过 password 字段传入
        base = req.api_url or f"https://{host}"
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
                        if image.split(":")[0] in spec_str or project in spec_str:
                            found = name
                            break
                    if found:
                        app_name = found
                        log(S("deploy_log.argocd_name_diff", name=app_name))
                    else:
                        msg = f"Argo CD 获取应用失败: {r.status_code} {r.text[:200]}"
                        log(msg)
                        return {"success": False, "output": msg}
                else:
                    msg = f"Argo CD 获取应用失败: {r.status_code} {r.text[:200]}"
                    log(msg)
                    return {"success": False, "output": msg}
                # 用发现的 app_name 重新获取
                r = requests.get(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
                if r.status_code != 200:
                    msg = f"Argo CD 获取应用失败: {r.status_code} {r.text[:200]}"
                    log(msg)
                    return {"success": False, "output": msg}

            app = r.json()

            log(S("deploy_log.argocd_prepare"))
            params = app.get("spec", {}).get("source", {}).get("helm", {}).get("parameters", [])
            kustomize = app.get("spec", {}).get("source", {}).get("kustomize", {})
            if kustomize:
                # 用 Application 名作为 Kustomize image name（常见约定）
                new_images = [{"name": app_name, "newName": image.split(":")[0], "newTag": image.split(":")[1] if ":" in image else "latest"}]
                patch = {"spec": {"source": {"kustomize": {"images": new_images}}}}
            else:
                # Helm: set image tag parameter
                found = False
                for p in params:
                    if p.get("name") == "image.tag":
                        p["value"] = image.split(":")[-1] if ":" in image else "latest"
                        found = True
                        break
                if not found:
                    params.append({"name": "image.tag", "value": image.split(":")[-1] if ":" in image else "latest"})
                patch = {"spec": {"source": {"helm": {"parameters": params}}}}

            log(S("deploy_log.argocd_update"))
            r = requests.put(f"{base}/api/v1/applications/{app_name}", json=patch, headers=headers, timeout=10, verify=False)
            if r.status_code != 200:
                log(S("deploy_log.argocd_update_fail", code=r.status_code, msg=r.text[:200]))
                return {"success": False, "output": "\n".join(output)}
            log(S("deploy_log.argocd_update_ok"))

            log(S("deploy_log.argocd_sync"))
            r = requests.post(f"{base}/api/v1/applications/{app_name}/sync", json={}, headers=headers, timeout=10, verify=False)
            if r.status_code != 200:
                log(S("deploy_log.argocd_sync_fail", code=r.status_code, msg=r.text[:200]))
                return {"success": False, "output": "\n".join(output)}
            log(S("deploy_log.argocd_sync_ok"))

            health = ""
            sync = ""
            for i in range(30):
                time.sleep(2)
                r = requests.get(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=False)
                a = r.json()
                health = a.get("status", {}).get("health", {}).get("status", "")
                sync = a.get("status", {}).get("sync", {}).get("status", "")
                log(S("deploy_log.argocd_wait", n=i+1, total=30, health=health or 'Unknown', sync=sync or 'Unknown'))
                if health == "Healthy":
                    log(S("deploy_log.argocd_healthy", sync=sync))
                    success = True
                    break
            else:
                log(S("deploy_log.argocd_timeout"))

            return {"success": success, "output": "\n".join(output)}
        except Exception as e:
            msg = str(e)
            log(msg)
            return {"success": False, "output": msg}
