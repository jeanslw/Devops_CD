"""K8S kubectl 部署模式 - set image(默认) / YAML apply(高级) + 部署后镜像断言"""

import logging
import secrets
import shlex

import yaml

from backend.config import settings
from backend.deploy_log import S
from backend.deployers.base import DeployTarget, retry_idempotent, ssh_connect
from backend.deployers.k8s_base import K8sSubDeployer
from backend.deployers.k8s_utils import (
    _deployment_containers,
    _deployment_image,
    _exec_exit,
    _get_deployment_doc,
    _get_deployment_name,
    _kubectl_pods,
    _log,
    _render_k8s_yaml,
    check_cancelled,
)

logger = logging.getLogger(__name__)


class KubectlDeployer(K8sSubDeployer):
    """kubectl apply + rollout restart"""

    def cd_type(self) -> str:
        return "kubectl"

    def stop(
        self, req, project: str, host: str, port: int = 22, user: str = "root", pwd: str = "", ssh_key: str = ""
    ) -> dict:
        """停止:默认只删 Deployment;stop_mode=manifest 时才按 YAML 删除整份清单(高危).

        资源名与 deploy 同源:优先显式工作负载名,其次从 YAML 解析,最后回退项目短名.
        """
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        namespace = (getattr(req, "k8s_ns", "") or "").strip()
        ns_flag = f" -n {shlex.quote(namespace)}" if namespace else ""
        stop_mode = (getattr(req, "stop_mode", "") or "").strip().lower()
        yaml_path = (getattr(req, "target_path", "") or "").strip() or (getattr(req, "path", "") or "").strip()
        explicit_name = (getattr(req, "target_app", "") or "").strip() or (getattr(req, "k8s_deploy", "") or "").strip()

        ssh = None
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)

            # ── 解析同源资源名:显式值 > YAML 中的 Deployment 名 > 项目短名 ──
            name = explicit_name or project.split("/")[-1]
            yaml_content = ""
            if yaml_path:
                if yaml_path.startswith(("http://", "https://")):
                    try:
                        import requests

                        r = requests.get(yaml_path, timeout=10)
                        yaml_content = r.text if r.ok else ""
                    except Exception:
                        yaml_content = ""
                else:
                    yaml_content, _, _ = _exec_exit(ssh, f"cat {shlex.quote(yaml_path)} 2>/dev/null")
            if not explicit_name and yaml_content:
                parsed = _get_deployment_name(yaml_content)
                if parsed:
                    name = parsed

            warning = ""
            if stop_mode == "manifest":
                if not yaml_path or yaml_path.startswith(("http://", "https://")):
                    return {
                        "success": False,
                        "output": "manifest 模式需要在集群上可直接访问的本地 YAML 路径（不支持 URL）",
                    }
                warning = "⚠️ manifest 模式会删除该 YAML 中声明的全部资源（含 Service/ConfigMap 等）。\n"
                cmd = f"kubectl delete{ns_flag} -f {shlex.quote(yaml_path)}"
            else:
                cmd = f"kubectl delete deployment/{shlex.quote(name)}{ns_flag}"

            out, err, ec = _exec_exit(ssh, cmd, timeout=settings.ssh_timeout)
            success = ec == 0 or "not found" in (err or "").lower() or "not found" in (out or "").lower()
            return {
                "success": success,
                "output": (warning + (err or out))[: settings.log_truncate_chars],
            }
        except Exception as ex:
            logger.error("Kubectl stop failed", exc_info=ex)
            return {"success": False, "output": "Stop service failed, please contact administrator"}
        finally:
            if ssh:
                ssh.close()

    def rollback(self, req, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        """原生回滚:kubectl rollout undo deployment/<name>(回退到上一版本)."""
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        filter_name = project.split("/")[-1]
        namespace = (getattr(req, "k8s_ns", "") or "").strip()
        ns_flag = f" -n {shlex.quote(namespace)}" if namespace else ""
        # 与 deploy / stop 同一命名策略:显式工作负载名 > YAML 声明名 > 项目短名
        explicit_name = (getattr(req, "target_app", "") or "").strip() or (getattr(req, "k8s_deploy", "") or "").strip()

        yaml_content = ""
        if not explicit_name and req.path and req.path.startswith("http"):
            try:
                import requests

                r = requests.get(req.path, timeout=10)
                if r.status_code == 200:
                    yaml_content = r.text
            except Exception:
                yaml_content = ""

        ssh = None
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)

            if not explicit_name and req.path and not req.path.startswith("http"):
                try:
                    out, _, ec = _exec_exit(ssh, f"cat {shlex.quote(req.path)}")
                    if ec == 0:
                        yaml_content = out
                except Exception:
                    yaml_content = ""

            deploy_name = explicit_name or _get_deployment_name(yaml_content) or filter_name

            _log(callback, S("deploy_log.rollback_start", name=deploy_name))
            check_cancelled()
            cmd = f"kubectl rollout undo deployment/{shlex.quote(deploy_name)}{ns_flag}"
            _log(callback, S("deploy_log.exec_cmd", n=1, cmd=cmd))
            out, err, ec = _exec_exit(ssh, cmd, timeout=settings.ssh_timeout)

            # 等待回滚完成
            check_cancelled()
            rollout_out, rollout_err, rollout_ec = _exec_exit(
                ssh,
                f"kubectl rollout status deployment/{shlex.quote(deploy_name)}{ns_flag} --timeout={settings.k8s_rollout_timeout}s",
                timeout=settings.k8s_rollout_timeout + 30,
            )

            after = _kubectl_pods(ssh, deploy_name, namespace)
            ok = ec == 0 and rollout_ec == 0
            lines = [f"Rollback command: {cmd}"]
            if out:
                lines.append(out)
            if err:
                lines.append(err)
            if rollout_out:
                lines.append(rollout_out)
            if rollout_err:
                lines.append(rollout_err)
            lines.append(f"\nPod status after rollback:\n{after or '(none)'}")
            lines.append("\nRollback: ✅ success" if ok else "\nRollback: ❌ failed")
            return {"success": ok, "output": "\n".join(lines)[: settings.log_truncate_chars]}
        except Exception as e:
            logger.error("Kubectl rollback failed", exc_info=e)
            return {"success": False, "output": str(e)}
        finally:
            if ssh:
                ssh.close()

    def deploy(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        """按 image_mode 分流:setimage(默认) / apply(高级)."""
        image_mode = (getattr(req, "image_mode", "") or "").strip().lower()
        if image_mode not in ("apply", "setimage"):
            # 未显式指定:有 YAML 走 apply(兼容旧行为),无 YAML 走 set image
            image_mode = "apply" if getattr(req, "path", "") else "setimage"
        if image_mode == "setimage":
            return self._deploy_set_image(req, image, project, host, port, user, pwd, ssh_key, callback)
        return self._deploy_apply(req, image, project, host, port, user, pwd, ssh_key, callback)

    def _deploy_set_image(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        """触发器主路径:对已存在的 Deployment 发出一次 set image,并用镜像断言证明变更生效."""
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        namespace = (getattr(req, "k8s_ns", "") or "").strip()
        ns_flag = f" -n {shlex.quote(namespace)}" if namespace else ""
        deploy_name = (
            (getattr(req, "target_app", "") or "").strip()
            or (getattr(req, "k8s_deploy", "") or "").strip()
            or project.split("/")[-1]
        )
        container = (getattr(req, "container", "") or "").strip()

        if not container:
            _log(callback, S("deploy_log.setimage_no_container"))
            return {"success": False, "output": "set image requires a container name"}

        ssh = None
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)

            # ── 变更前:确认 Deployment 存在、容器名合法 ──
            doc, doc_err = _get_deployment_doc(ssh, deploy_name, namespace)
            if doc is None:
                _log(callback, S("deploy_log.setimage_deploy_not_found", name=deploy_name))
                return {
                    "success": False,
                    "output": f"Deployment [{deploy_name}] not found: {doc_err}. "
                    "First deployment requires YAML apply mode.",
                }

            containers = _deployment_containers(doc)
            names = [str(c.get("name") or "") for c in containers if c.get("name")]
            if container not in names:
                _log(
                    callback,
                    S(
                        "deploy_log.setimage_container_missing",
                        name=deploy_name,
                        container=container,
                        containers=", ".join(names) or "(none)",
                    ),
                )
                return {
                    "success": False,
                    "output": f"container [{container}] not found in deployment [{deploy_name}]; available: {names}",
                }

            before_image = _deployment_image(doc, container) or "(none)"
            _log(callback, S("deploy_log.current_version"))
            _log(callback, f"{container}: {before_image}")

            # ── 一次变更:kubectl set image ──
            check_cancelled()
            image_arg = f"{container}={image}"
            cmd = f"kubectl set image deployment/{shlex.quote(deploy_name)} {shlex.quote(image_arg)}{ns_flag}"
            _log(callback, S("deploy_log.exec_cmd", n=1, cmd=cmd))
            out, err, ec = _exec_exit(ssh, cmd, timeout=settings.ssh_timeout)
            if out:
                _log(callback, out)
            if err:
                _log(callback, err)
            if ec != 0:
                _log(callback, S("deploy_log.deploy_error", error=err or f"exit code {ec}"))
                return {"success": False, "output": f"kubectl set image failed (exit {ec}):\n{out or err}"}
            _log(callback, S("deploy_log.setimage_applied", container=container, image=image))

            # ── 原生完成信号:rollout status ──
            check_cancelled()
            _log(callback, S("deploy_log.waiting_pod"))
            rollout_out, rollout_err, rollout_ec = _exec_exit(
                ssh,
                f"kubectl rollout status deployment/{shlex.quote(deploy_name)}{ns_flag} "
                f"--timeout={settings.k8s_rollout_timeout}s",
                timeout=settings.k8s_rollout_timeout + 30,
            )
            rollout_text = (rollout_out or rollout_err or "").strip()
            if rollout_out:
                _log(callback, rollout_out)
            if rollout_err:
                _log(callback, rollout_err)

            # ── 变更后:断言实际镜像与目标一致 ──
            after_doc, after_err = _get_deployment_doc(ssh, deploy_name, namespace)
            after_image = _deployment_image(after_doc, container) if after_doc else ""
            if after_image:
                _log(callback, S("deploy_log.after_version"))
                _log(callback, f"{container}: {after_image}")

            is_ok = ec == 0 and rollout_ec == 0 and after_image == image
            if is_ok:
                _log(callback, S("deploy_log.image_assert_ok", image=image))
                _log(callback, S("deploy_log.verify_ok"))
            else:
                _log(callback, S("deploy_log.verify_fail"))
                if after_image != image:
                    _log(
                        callback,
                        S(
                            "deploy_log.image_assert_fail",
                            actual=after_image or after_err or "(none)",
                            expected=image,
                        ),
                    )

            status_text = "验证部署: ✅ 部署成功！" if is_ok else "验证部署: ❌ 部署失败！"
            result = (
                f"Workload: deployment/{deploy_name} container={container}\n"
                f"Image: {before_image} -> {after_image or image}\n\n"
                f"{rollout_text}\n\n{status_text}"
            )
            return {"success": is_ok, "output": result[: settings.log_truncate_chars]}
        except Exception as e:
            logger.error("Kubectl set image failed", exc_info=e)
            _log(callback, S("deploy_log.deploy_error", error=str(e)))
            return {"success": False, "output": str(e)}
        finally:
            if ssh:
                ssh.close()

    def _deploy_apply(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        """高级模式:整份 YAML apply,并在 rollout 后断言镜像确实变为目标镜像."""
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)

        tag = req.tag
        filter_name = project.split("/")[-1]
        namespace = (getattr(req, "k8s_ns", "") or "").strip()
        # ns_flag 延后构建:留空 k8s_ns 时先解析 YAML 声明的 namespace(见 effective_ns)

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

        # 单次 SSH 连接:读取(本地文件),上传,部署全部共用
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

            # ── 先渲染再校验 ──
            yaml_content = _render_k8s_yaml(yaml_content, image, tag)

            # kubectl 的 --namespace/-n 不覆盖 manifest 声明的 metadata.namespace;两者冲突时直接失败.
            try:
                docs = list(yaml.safe_load_all(yaml_content))
            except yaml.YAMLError as exc:
                return {"success": False, "output": f"Invalid Kubernetes YAML: {exc}"}
            manifest_ns = ""
            deploy_ns = ""
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                kind = str(doc.get("kind") or "")
                raw_metadata = doc.get("metadata")
                metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
                doc_ns = str(metadata.get("namespace") or "").strip()
                if doc_ns and not manifest_ns:
                    manifest_ns = doc_ns
                # rollout/pods 查询针对 Deployment,执行命名空间以 Deployment 声明为准
                if kind == "Deployment" and doc_ns and not deploy_ns:
                    deploy_ns = doc_ns
                if (
                    namespace
                    and doc_ns
                    and kind
                    not in {
                        "Namespace",
                        "ClusterRole",
                        "ClusterRoleBinding",
                        "CustomResourceDefinition",
                        "Node",
                        "PersistentVolume",
                        "StorageClass",
                        "MutatingWebhookConfiguration",
                        "ValidatingWebhookConfiguration",
                        "PriorityClass",
                        "PodSecurityPolicy",
                    }
                    and doc_ns != namespace
                ):
                    return {
                        "success": False,
                        "output": (
                            f"Kubernetes namespace mismatch: request namespace [{namespace}] "
                            f"but manifest resource [{kind or 'Unknown'}] declares [{doc_ns}]."
                        ),
                    }

            # 执行/验证统一使用的 namespace:请求填写优先;留空则以 YAML 声明为准.
            # apply 不带 -n 时按 manifest 落位,rollout/pods 查询须带同一 -n.
            effective_ns = namespace or deploy_ns or manifest_ns
            ns_flag = f" -n {shlex.quote(effective_ns)}" if effective_ns else ""

            yaml_deploy_name = _get_deployment_name(yaml_content)
            if yaml_deploy_name and yaml_deploy_name != filter_name:
                # 前端预检弹窗已确认,这里只打警告不拦截
                _log(callback, S("deploy_log.yaml_name_mismatch", yaml_name=yaml_deploy_name, project=filter_name))
            # 并发部署同一项目时固定路径会互相覆盖,故带随机后缀
            tmp = f"/tmp/k8s-{filter_name}-{secrets.token_hex(6)}.yaml"  # nosec

            _log(callback, S("deploy_log.uploading_yaml_k8s"))
            sftp = ssh.open_sftp()
            try:
                with sftp.file(tmp, "w") as f:
                    f.write(yaml_content)
            finally:
                sftp.close()
            _log(callback, S("deploy_log.yaml_upload_k8s_ok"))

            deploy_name = yaml_deploy_name or filter_name  # 以 YAML 声明的 Deployment 名为准
            name_mismatch = yaml_deploy_name and yaml_deploy_name != filter_name

            deploy_log = []

            _log(callback, S("deploy_log.verifying_app"))

            before = _kubectl_pods(ssh, deploy_name, effective_ns)
            before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
            before_pods = {b.split()[0] for b in before.split("\n") if b.strip()} if before else set()

            is_first_deploy = False
            if not before.strip():
                if name_mismatch:
                    # 名字不匹配时 YAML Deployment 是新的,视为首次部署
                    is_first_deploy = True
                    _log(callback, S("deploy_log.first_deploy_pod", deploy=deploy_name))
                else:
                    all_pods = _kubectl_pods(ssh, "", effective_ns)
                    running_pods = all_pods.strip()
                    if running_pods:
                        _log(callback, S("deploy_log.app_not_found", name=deploy_name, running=running_pods))
                        return {
                            "success": False,
                            "output": f"{before_text}\n\nDeploy failed: app [{deploy_name}] not found.\nRunning Pods:\n{running_pods}",
                        }
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
            cmds = [f"kubectl apply{ns_flag} -f {shlex.quote(tmp)}"]
            if not is_first_deploy:
                cmds.append(f"kubectl rollout restart deployment/{shlex.quote(deploy_name)}{ns_flag}")
            for i, c in enumerate(cmds):
                check_cancelled()
                _log(callback, S("deploy_log.exec_cmd", n=i + 1, cmd=c))
                # kubectl apply 幂等,可安全重试;rollout restart 失败按原逻辑直接判败
                if i == 0:
                    o, e, ec = retry_idempotent(
                        lambda: _exec_exit(ssh, c),
                        attempts=settings.deploy_retry_attempts,
                        delay=settings.deploy_retry_delay,
                        log_fn=lambda n: _log(
                            callback,
                            S("deploy_log.retry_attempt", attempt=n, max_attempts=settings.deploy_retry_attempts),
                        ),
                    )
                else:
                    o, e, ec = _exec_exit(ssh, c)
                if o:
                    deploy_log.append(o)
                    _log(callback, o)
                if e:
                    deploy_log.append(e)
                    _log(callback, e)
                if ec != 0:
                    _log(callback, S("deploy_log.deploy_error", error=e or f"exit code {ec}"))
                    return {"success": False, "output": f"{before_text}\n\nStep {i + 1} failed (exit {ec}):\n{o or e}"}

            # 远端临时 YAML 用完即删,避免并发随机名文件长期堆积
            try:
                ssh.exec_command(f"rm -f {shlex.quote(tmp)}")
            except Exception:
                pass

            # ── rollout status 等待部署完成 ──
            check_cancelled()
            _log(callback, S("deploy_log.waiting_pod"))
            rollout_cmd = f"kubectl rollout status deployment/{shlex.quote(deploy_name)}{ns_flag} --timeout={settings.k8s_rollout_timeout}s"
            rollout_out, rollout_err, rollout_ec = _exec_exit(
                ssh, rollout_cmd, timeout=settings.k8s_rollout_timeout + 30
            )
            rollout_output = (rollout_out or rollout_err or "").strip()
            if rollout_out:
                _log(callback, rollout_out)
            if rollout_err:
                _log(callback, rollout_err)

            # ── 部署后:查当前 Pod,排除旧 Pod ──
            _log(callback, S("deploy_log.after_version"))
            all_after = _kubectl_pods(ssh, deploy_name, effective_ns)
            if all_after.strip():
                after_pods = [
                    line for line in all_after.split("\n") if line.strip() and line.split()[0] not in before_pods
                ]
                after = "\n".join(after_pods) if after_pods else all_after
            else:
                after = all_after
            _log(callback, after)

            # ── 成败判断:rollout status + 实际镜像断言 ──
            # 只认 rollout 会放过"YAML 未引用 {IMAGE}:{TAG},实际没换镜像"的假成功.
            is_ok = rollout_ec == 0
            actual_images: list[str] = []
            assert_error = ""
            if is_ok:
                after_doc, after_doc_err = _get_deployment_doc(ssh, deploy_name, effective_ns)
                if after_doc is None:
                    is_ok = False
                    assert_error = after_doc_err or "deployment unreadable"
                else:
                    actual_images = [str(c.get("image") or "") for c in _deployment_containers(after_doc)]
                    if image not in actual_images:
                        is_ok = False
                        assert_error = ", ".join(i for i in actual_images if i) or "(none)"
            if is_ok:
                running_count = sum(1 for line in after.split("\n") if "Running" in line)
                _log(callback, S("deploy_log.image_assert_ok", image=image))
                _log(callback, S("deploy_log.verify_ok"))
                result = (
                    f"{before_text}\n\n开始部署:\n"
                    + "\n".join(deploy_log)
                    + f"\n\n{rollout_output}\n\n部署后运行版本:\n{after}"
                    + f"\n\n已部署: {running_count} 个 Running Pod\n\n验证部署: ✅ 部署成功！"
                )
            else:
                if assert_error:
                    _log(
                        callback,
                        S("deploy_log.image_assert_fail", actual=assert_error, expected=image),
                    )
                    _log(callback, S("deploy_log.verify_fail_version"))
                else:
                    _log(callback, S("deploy_log.verify_fail_timeout"))
                result = (
                    f"{before_text}\n\n开始部署:\n"
                    + "\n".join(deploy_log)
                    + f"\n\n{rollout_output}\n\n部署后运行版本:\n{after}"
                    + "\n\n验证部署: ❌ 部署失败！"
                    + (f"\n实际镜像: {assert_error}\n期望镜像: {image}" if assert_error else "")
                )

            return {"success": is_ok, "output": result[: settings.log_truncate_chars]}
        except Exception as e:
            logger.error("Kubectl deploy failed", exc_info=e)
            _log(callback, S("deploy_log.deploy_error", error=str(e)))
            return {"success": False, "output": str(e)}
        finally:
            if ssh:
                ssh.close()
