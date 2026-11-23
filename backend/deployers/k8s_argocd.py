"""K8S Argo CD 部署模式 - API patch image + sync + health polling"""

import logging
import re
from datetime import datetime

import requests
import urllib3

from backend.config import settings
from backend.deploy_log import S
from backend.deployers.base import split_image_ref
from backend.deployers.k8s_base import K8sSubDeployer
from backend.deployers.k8s_utils import check_cancelled

logger = logging.getLogger(__name__)

_VERIFY = settings.argocd_verify_tls


def _get(url: str, **kw):
    headers = kw.pop("headers", {})
    return requests.get(url, headers=headers, timeout=10, verify=_VERIFY, **kw)


def _parse_rfc3339(value: str):
    """解析 Argo 返回的 RFC3339 时间;兼容带/不带小数秒,无法解析时返回 None."""
    text = (value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _normalize_image_ref(ref: str) -> tuple[str, str]:
    """归一为(仓库末段, tag/digest),用于 Argo summary 短名与目标全名的比较."""
    text = (ref or "").strip()
    if not text:
        return "", ""
    if "@" in text:
        repo, digest = text.split("@", 1)
        return repo.rsplit("/", 1)[-1], f"@{digest}"
    name, tag = split_image_ref(text)
    return name.rsplit("/", 1)[-1], tag


def _image_applied(candidate: str, target: str) -> bool:
    """判断 summary.images 中的条目是否就是目标镜像(允许省略 registry 前缀)."""
    cand_name, cand_ver = _normalize_image_ref(candidate)
    tgt_name, tgt_ver = _normalize_image_ref(target)
    if not cand_name or not tgt_name:
        return False
    return cand_name == tgt_name and cand_ver == tgt_ver


class ArgoCDDeployer(K8sSubDeployer):
    """Argo CD: patch image + sync + health polling"""

    def cd_type(self) -> str:
        return "argocd"

    def stop(
        self, req, project: str, host: str, port: int = 22, user: str = "root", pwd: str = "", ssh_key: str = ""
    ) -> dict:
        """停止:删除 ArgoCD Application"""
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        token = pwd
        base = getattr(req, "api_url", "") or f"https://{host}"
        app_name = project.split("/")[-1]
        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            r = requests.delete(f"{base}/api/v1/applications/{app_name}", headers=headers, timeout=10, verify=_VERIFY)
            if r.status_code in (200, 204):
                return {"success": True, "output": f"ArgoCD application {app_name} deleted"}
            else:
                return {"success": False, "output": f"Delete failed: {r.status_code} {r.text[:200]}"}
        except Exception as ex:
            logger.error("ArgoCD stop failed", exc_info=ex)
            return {"success": False, "output": "Stop service failed, please contact administrator"}

    def rollback(self, req, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        """原生回滚:ArgoCD POST /rollback 回到上一 sync 版本(history id)."""
        import time

        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        token = pwd
        base = getattr(req, "api_url", "") or f"https://{host}"
        output = []
        success = False

        def log(msg):
            if callable(callback):
                callback(msg)
            output.append(msg)

        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            app_name = project.split("/")[-1]
            r = _get(f"{base}/api/v1/applications/{app_name}", headers=headers)
            if r.status_code != 200:
                log(S("deploy_log.argocd_searching"))
                r_list = _get(f"{base}/api/v1/applications", headers=headers)
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
                r = _get(f"{base}/api/v1/applications/{app_name}", headers=headers)
                if r.status_code != 200:
                    msg = S("deploy_log.argocd_get_fail", code=r.status_code, msg=r.text[:200])
                    log(msg)
                    return {"success": False, "output": msg}

            app = r.json()

            history = app.get("status", {}).get("history", []) or []
            sorted_history = sorted(history, key=lambda h: h.get("id", 0))
            if len(sorted_history) < 2:
                msg = S("deploy_log.argocd_no_prev_version")
                log(msg)
                return {"success": False, "output": msg}
            prev = sorted_history[-2]
            prev_id = prev.get("id")
            prev_rev = prev.get("revision", "")
            rollback_summary = S("deploy_log.argocd_rollback_to", id=prev_id, revision=prev_rev)
            if callable(callback):
                callback(rollback_summary)

            check_cancelled()
            r = requests.post(
                f"{base}/api/v1/applications/{app_name}/rollback",
                json={"id": prev_id},
                headers=headers,
                timeout=10,
                verify=_VERIFY,
            )
            if r.status_code != 200:
                log(S("deploy_log.argocd_rollback_failed", code=r.status_code, msg=r.text[:200]))
                return {"success": False, "output": "\n".join(output)}

            health = ""
            sync = ""
            for i in range(30):
                check_cancelled()
                time.sleep(2)
                r = _get(f"{base}/api/v1/applications/{app_name}", headers=headers)
                if r.status_code != 200:
                    log(S("deploy_log.argocd_poll_fail", code=r.status_code, msg=r.text[:200]))
                    continue
                a = r.json()
                health = a.get("status", {}).get("health", {}).get("status", "")
                sync = a.get("status", {}).get("sync", {}).get("status", "")
                revision = (
                    a.get("status", {}).get("sync", {}).get("revision")
                    or a.get("status", {})
                    .get("operationState", {})
                    .get("operation", {})
                    .get("sync", {})
                    .get("revision")
                    or ""
                )
                log(S("deploy_log.argocd_wait", n=i + 1, total=30, health=health or "Unknown", sync=sync or "Unknown"))
                if health == "Healthy" and sync == "Synced":
                    if prev_rev and revision and revision == prev_rev:
                        log(
                            f"[argocd] Rollback is Healthy/Synced but still on previous revision {prev_rev}; "
                            f"current revision is {revision}; waiting for the app to move away"
                        )
                        continue
                    log(S("deploy_log.argocd_healthy", sync=sync))
                    success = True
                    break
            else:
                log(S("deploy_log.argocd_timeout"))

            result = {"success": success, "output": "\n".join(output)}
            if success:
                result["rollback_summary"] = rollback_summary
            return result
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

        token = pwd
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

            app_name = project.split("/")[-1]
            r = _get(f"{base}/api/v1/applications/{app_name}", headers=headers)
            if r.status_code != 200:
                log(S("deploy_log.argocd_searching"))
                r_list = _get(f"{base}/api/v1/applications", headers=headers)
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
                r = _get(f"{base}/api/v1/applications/{app_name}", headers=headers)
                if r.status_code != 200:
                    msg = S("deploy_log.argocd_get_fail", code=r.status_code, msg=r.text[:200])
                    log(msg)
                    return {"success": False, "output": msg}

            app = r.json()
            before_revision = (
                app.get("status", {}).get("sync", {}).get("revision")
                or app.get("status", {}).get("operationState", {}).get("operation", {}).get("sync", {}).get("revision")
                or ""
            )

            log(S("deploy_log.argocd_prepare"))
            params = app.get("spec", {}).get("source", {}).get("helm", {}).get("parameters", [])
            kustomize = app.get("spec", {}).get("source", {}).get("kustomize", {})
            if kustomize:
                new_images = [
                    {
                        "name": app_name,
                        "newName": image_repo,
                        "newTag": image_tag or "latest",
                    }
                ]
                patch = {"spec": {"source": {"kustomize": {"images": new_images}}}}
            else:
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
                f"{base}/api/v1/applications/{app_name}", json=patch, headers=headers, timeout=10, verify=_VERIFY
            )
            if r.status_code != 200:
                log(S("deploy_log.argocd_update_fail", code=r.status_code, msg=r.text[:200]))
                return {"success": False, "output": "\n".join(output)}
            log(S("deploy_log.argocd_update_ok"))

            log(S("deploy_log.argocd_sync"))
            sync_requested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            sync_requested_dt = _parse_rfc3339(sync_requested_at)
            r = requests.post(
                f"{base}/api/v1/applications/{app_name}/sync", json={}, headers=headers, timeout=10, verify=_VERIFY
            )
            if r.status_code != 200:
                log(S("deploy_log.argocd_sync_fail", code=r.status_code, msg=r.text[:200]))
                return {"success": False, "output": "\n".join(output)}
            log(S("deploy_log.argocd_sync_ok"))

            health = ""
            sync = ""
            revision = ""
            phase = ""
            for i in range(30):
                check_cancelled()
                time.sleep(2)
                r = _get(f"{base}/api/v1/applications/{app_name}", headers=headers)
                if r.status_code != 200:
                    log(S("deploy_log.argocd_poll_fail", code=r.status_code, msg=r.text[:200]))
                    continue
                a = r.json()
                health = a.get("status", {}).get("health", {}).get("status", "")
                sync = a.get("status", {}).get("sync", {}).get("status", "")
                revision = (
                    a.get("status", {}).get("sync", {}).get("revision")
                    or a.get("status", {})
                    .get("operationState", {})
                    .get("operation", {})
                    .get("sync", {})
                    .get("revision")
                    or ""
                )
                op_state = a.get("status", {}).get("operationState", {}) or {}
                phase = op_state.get("phase", "") or ""
                finished_at = op_state.get("finishedAt", "") or ""
                images = a.get("status", {}).get("summary", {}).get("images", []) or []
                log(S("deploy_log.argocd_wait", n=i + 1, total=30, health=health or "Unknown", sync=sync or "Unknown"))

                if phase in ("Failed", "Error"):
                    log(S("deploy_log.argocd_sync_phase_failed", phase=phase))
                    break

                if health == "Healthy" and sync == "Synced":
                    revision_advanced = bool(before_revision and revision and revision != before_revision)
                    image_applied = any(_image_applied(img, image) for img in images)
                    finished_dt = _parse_rfc3339(finished_at)
                    operation_done = bool(
                        phase == "Succeeded"
                        and finished_dt is not None
                        and sync_requested_dt is not None
                        and finished_dt >= sync_requested_dt
                    )
                    if revision_advanced or image_applied or not before_revision or operation_done:
                        log(S("deploy_log.argocd_healthy", sync=sync))
                        success = True
                        break
                    log(
                        f"[argocd] Healthy/Synced but still on revision {revision or 'Unknown'} "
                        f"and target image {image} not in summary.images; waiting for the change to take effect"
                    )
            else:
                log(S("deploy_log.argocd_timeout"))

            if not success:
                log(
                    f"[argocd] Sync status={sync or 'Unknown'}; health={health or 'Unknown'}; "
                    f"revision={revision or 'Unknown'}; phase={phase or 'Unknown'}"
                )

            return {"success": success, "output": "\n".join(output)}
        except Exception as e:
            logger.error("ArgoCD deploy failed", exc_info=e)
            msg = S("deploy_log.argocd_deploy_error")
            log(msg)
            return {"success": False, "output": msg}
