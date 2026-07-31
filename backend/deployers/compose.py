"""docker-compose 部署器 — IMAGE/TAG 双变量，支持 --env-file"""

import shlex

from .base import Deployer, DeployTarget, DeployResult, ssh_session, _exec_on, ssh_exec_stream
from backend.config import settings
from backend.deploy_log import S


class ComposeDeployer(Deployer):
    """docker-compose 部署：.env 写入 IMAGE + TAG，与 K8s 变量对齐"""

    def name(self) -> str:
        return "compose"

    def deploy(
        self, target: DeployTarget, image: str, project: str, tag: str,
        callback=None,
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="Missing target host")

        mode = target.mode or "remote"
        image_name = image.split(":")[0]  # hub.abc.com/project/app

        # ── commands 模式：纯透传，不做任何 compose/docker 假设 ──
        if mode == "commands":
            template = target.options.get("commands", "")
            if not template:
                return DeployResult(image=image, status="failed", output="Missing custom commands")
            cmd = template.replace("{image}", image).replace("{image_name}", image_name).replace("{tag}", tag).replace("{project}", project)
            try:
                with ssh_session(target, settings.ssh_timeout) as ssh:
                    self._log(callback, S("deploy_log.starting_deploy"))
                    output = self._ssh_exec_stream(ssh, cmd, callback)
                    return DeployResult(image=image, status="ok", output=output)
            except Exception as e:
                self._log(callback, S("deploy_log.deploy_error", error=str(e)))
                return DeployResult(image=image, status="failed", output=str(e))

        # ── remote 模式：标准 docker-compose 流程 ──
        if not target.path:
            return DeployResult(image=image, status="failed", output="Missing compose path")

        yaml_content = target.options.get("yaml_content", "")
        env_file = target.options.get("env_file", "")

        try:
            project_short = project.split("/")[-1]
            with ssh_session(target, settings.ssh_timeout) as ssh:
                # 1. 校验路径
                self._log(callback, S("deploy_log.verifying_path"))
                path_check = self._ssh_run(ssh, f"test -d {target.path} && echo 'OK' || echo 'NOT_FOUND'", image)
                if path_check.output.strip() != "OK":
                    self._log(callback, S("deploy_log.path_not_found", path=target.path))
                    return DeployResult(image=image, status="failed", output=f"Deploy failed: path not found - {target.path}")
                self._log(callback, S("deploy_log.path_ok", path=target.path))

                # 2. 上传 YAML（如果有）
                if yaml_content:
                    self._log(callback, S("deploy_log.uploading_yaml"))
                    content = yaml_content.replace("{image}", image).replace("{image_name}", image_name).replace("{tag}", tag).replace("{project}", project)
                    err = self._upload_file(ssh, target, content)
                    if err:
                        self._log(callback, S("deploy_log.yaml_upload_fail", error=err))
                        return DeployResult(image=image, status="failed", output=f"YAML upload failed: {err}")
                    self._log(callback, S("deploy_log.yaml_upload_ok"))

                # 3. 写入 IMAGE / TAG 到 env 文件
                self._log(callback, S("deploy_log.writing_env", image=image_name, tag=tag))
                if env_file:
                    env_flag = f"--env-file {env_file}"
                else:
                    env_file = ".env"
                    env_flag = ""

                # sed 只替换 IMAGE/TAG 行（# 分隔符避免镜像名 / 冲突），不存在则追加
                env_update = (
                    f"cd {target.path} && "
                    f"sed -i \"s#^IMAGE=.*#IMAGE={image_name}#\" {env_file} && "
                    f"grep -q '^IMAGE=' {env_file} || echo \"IMAGE={image_name}\" >> {env_file} && "
                    f"sed -i \"s#^TAG=.*#TAG={tag}#\" {env_file} && "
                    f"grep -q '^TAG=' {env_file} || echo \"TAG={tag}\" >> {env_file}"
                )
                self._ssh_run(ssh, env_update, image)

                # 验证 .env 写入成功（直接看 stdout，忽略 stderr）
                out, _, _ = _exec_on(ssh, f"cd {target.path} && cat {env_file}")
                if image_name not in out or tag not in out:
                    self._log(callback, S("deploy_log.env_write_fail"))
                    return DeployResult(image=image, status="failed",
                        output=f".env write failed:\n{out}")

                # 4. 仓库登录 & 镜像拉取
                registry = settings.harbor_registry.rstrip("/") if settings.harbor_registry else ""
                has_creds = bool(settings.harbor_user and settings.harbor_password)

                # 判断镜像是否来自已配置的私有仓库
                is_private = bool(registry) and image_name.startswith(registry + "/")
                # Docker Hub 回退镜像名（剥离仓库前缀）
                dh_image = image_name[len(registry) + 1:] if is_private else image_name

                def _image_exists(img):
                    r = self._ssh_run(ssh,
                        f"docker image inspect {img}:{tag} > /dev/null 2>&1 && echo 'EXISTS' || echo 'NOT_FOUND'", img)
                    return r.output.strip() == "EXISTS"

                def _remote_manifest_digest(img):
                    """获取远程 registry 的 manifest digest（仅拉 manifest，不下载层）"""
                    # 不过滤 stderr，方便排查 manifest inspect 失败原因
                    r = self._ssh_run(ssh,
                        f"timeout 10 docker manifest inspect {img}:{tag} 2>&1 | grep -oE 'sha256:[a-f0-9]{{64}}' | head -1",
                        img)
                    d = r.output.strip()
                    return d if d and d.startswith("sha256:") and len(d) == 71 else ""

                def _local_has_digest(img, digest):
                    """本地镜像是否包含指定的 registry digest"""
                    r = self._ssh_run(ssh,
                        f"docker image inspect {img}:{tag} --format '{{{{.RepoDigests}}}}' 2>/dev/null",
                        img)
                    return f"@{digest}" in r.output

                def _needs_pull(img):
                    """决定是否需要 pull：
                    - 本地没镜像 → 拉
                    - 能拿到 remote digest 且与本地一致 → 跳过
                    - 其他情况（拿不到 digest / digest 不一致）→ 拉（无法比对不能假设一致）
                    """
                    if not _image_exists(img):
                        self._log(callback, S("deploy_log.image_not_local_will_pull"))
                        return True
                    remote = _remote_manifest_digest(img)
                    if not remote:
                        self._log(callback, S("deploy_log.no_remote_digest_will_pull"))
                        return True
                    if _local_has_digest(img, remote):
                        self._log(callback, S("deploy_log.image_up_to_date"))
                        return False
                    return True

                # 4a. 登录私有仓库
                if is_private and has_creds:
                    self._log(callback, S("deploy_log.registry_login", registry=registry))
                    login_out = self._ssh_run(ssh,
                        f"echo {shlex.quote(settings.harbor_password)} | docker login {registry} -u {shlex.quote(settings.harbor_user or '')} --password-stdin 2>&1", image)
                    if "Login Succeeded" in login_out.output:
                        self._log(callback, S("deploy_log.registry_login_ok"))
                    else:
                        self._log(callback, S("deploy_log.registry_login_fail"))
                elif is_private and not has_creds:
                    self._log(callback, S("deploy_log.registry_no_creds"))

                # 4b. 拉取目标 service 镜像（只拉目标 service，不动其他如 mysql）
                if _needs_pull(image_name):
                    self._log(callback, S("deploy_log.pulling_image"))
                    pull_text = self._ssh_exec_stream(ssh,
                        f"cd {target.path} && COLUMNS=512 timeout 600 docker-compose pull {project_short} 2>&1", callback)
                else:
                    pull_text = ""
                pull_ok = _image_exists(image_name)

                # 4c. 私有仓库失败 → 回退 Docker Hub
                if not pull_ok and is_private:
                    self._log(callback, S("deploy_log.fallback_dockerhub", image=dh_image))
                    self._ssh_run(ssh,
                        f"cd {target.path} && sed -i \"s#^IMAGE=.*#IMAGE={dh_image}#\" {env_file} && grep -q '^IMAGE=' {env_file} || echo \"IMAGE={dh_image}\" >> {env_file}", image)
                    if _needs_pull(dh_image):
                        self._log(callback, S("deploy_log.pulling_image"))
                        dh_pull = self._ssh_exec_stream(ssh,
                            f"cd {target.path} && COLUMNS=512 timeout 600 docker-compose pull {project_short} 2>&1", callback)
                        pull_text += "\n" + dh_pull
                    pull_ok = _image_exists(dh_image)
                    if not pull_ok:
                        self._log(callback, S("deploy_log.pull_failed_all"))
                        return DeployResult(image=image, status="failed",
                            output=f"Image pull failed (both private registry and Docker Hub):\n\n{pull_text}")

                if not pull_ok:
                    self._log(callback, S("deploy_log.pull_failed"))
                    return DeployResult(image=image, status="failed",
                        output=f"Image pull failed:\n\n{pull_text}")

                # 4d. 用 docker inspect 补全完整 Digest 和镜像名（不受终端宽度截断）
                #     同时清理 docker-compose 截断的 digest:/status: 行，避免存到部署记录
                pull_lines = [l for l in pull_text.split('\n') if l.strip()]
                pull_clean = []
                img_digest = ""
                img_status = ""
                try:
                    inspect_image = dh_image if _image_exists(dh_image) else image_name
                    dig, _, _ = _exec_on(ssh, f"docker image inspect --format '{{{{index .RepoDigests 0}}}}' {inspect_image} 2>/dev/null")
                    if dig.strip():
                        img_digest = dig.strip()
                    tag_full, _, _ = _exec_on(ssh, f"docker image inspect --format '{{{{index .RepoTags 0}}}}' {inspect_image} 2>/dev/null")
                    if tag_full.strip():
                        img_status = tag_full.strip()
                except Exception:
                    pass
                # 过滤 docker-compose 截断的 digest/status 行，替换为完整 inspect 输出
                if img_digest:
                    self._log(callback, S("deploy_log.image_digest", digest=img_digest))
                    pull_clean.append(f"Digest: {img_digest}")
                if img_status:
                    self._log(callback, S("deploy_log.image_status", status=img_status))
                    pull_clean.append(f"Status: Downloaded newer image for {img_status}")
                for line in pull_lines:
                    low = line.lower()
                    if 'digest:' in low or 'status:' in low:
                        continue  # docker-compose 截断版，丢弃
                    pull_clean.append(line)
                pull_summary = "\n".join(pull_clean)

                # 5. 部署前版本检查
                self._log(callback, S("deploy_log.checking_version"))
                before = self._ssh_run(ssh,
                    f"cd {target.path} && docker-compose ps -q 2>/dev/null | xargs docker inspect --format '{{{{.Name}}}} {{{{.Config.Image}}}}' 2>/dev/null | grep -F '{project_short}'",
                    image)
                self._log(callback, S("deploy_log.current_version"))
                self._log(callback, before.output or S("deploy_log.no_output"))

                # 6. 执行部署
                self._log(callback, S("deploy_log.starting_deploy"))
                deploy_text = self._ssh_exec_stream(ssh,
                    f"cd {target.path} && docker-compose {env_flag} up -d --force-recreate {project_short} 2>&1", callback)

                # 7. 部署后验证
                self._log(callback, S("deploy_log.waiting_container"))
                self._ssh_run(ssh, "sleep 3", image)

                running = self._ssh_run(ssh,
                    f"cd {target.path} && docker-compose ps -q 2>/dev/null | xargs docker inspect --format '{{{{.Name}}}} {{{{.Config.Image}}}}' 2>/dev/null | grep -F '{project_short}'",
                    image)
                if not running.output.strip():
                    running = self._ssh_run(ssh,
                        f"docker ps --format '{{{{.Names}}}} {{{{.Image}}}}' 2>/dev/null | grep -F '{project_short}'", image)

                if running.output and tag in running.output:
                    self._log(callback, S("deploy_log.container_started"))
                    result = DeployResult(image=image, status="ok",
                        output=f"Pull output:\n{pull_summary}\n---\nBefore version:\n{before.output or '(none)'}\n\nDeploy output:\n{deploy_text}\n\nAfter version:\n{running.output}\n\nVerification: ✅ Deploy succeeded!")
                    self._log(callback, S("deploy_log.after_version"))
                    self._log(callback, running.output)
                    self._log(callback, S("deploy_log.verify_ok"))
                elif not running.output.strip():
                    self._log(callback, S("deploy_log.container_failed"))
                    result = DeployResult(image=image, status="failed",
                        output=f"Before version:\n{before.output or '(none)'}\n\nDeploy output:\n{deploy_text}\n\nAfter version: (no running container)\n\nVerification: ❌ Deploy failed! (container not started)")
                    self._log(callback, S("deploy_log.after_version_none"))
                    self._log(callback, S("deploy_log.verify_fail_container"))
                elif all(s not in deploy_text.lower() for s in ["up", "starting", "started", "created"]):
                    self._log(callback, S("deploy_log.container_failed"))
                    result = DeployResult(image=image, status="failed",
                        output=f"Before version:\n{before.output or '(none)'}\n\nDeploy output:\n{deploy_text}\n\nAfter version:\n{running.output or '(none)'}\n\nVerification: ❌ Deploy failed!")
                    self._log(callback, S("deploy_log.after_version"))
                    self._log(callback, running.output or S("deploy_log.no_output"))
                    self._log(callback, S("deploy_log.verify_fail"))
                else:
                    self._log(callback, S("deploy_log.container_failed"))
                    result = DeployResult(image=image, status="failed",
                        output=f"Before version:\n{before.output or '(none)'}\n\nDeploy output:\n{deploy_text}\n\nAfter version:\n{running.output}\n\nVerification: ❌ Deploy failed! (version mismatch)")
                    self._log(callback, S("deploy_log.after_version"))
                    self._log(callback, running.output)
                    self._log(callback, S("deploy_log.verify_fail_version"))
        except Exception as e:
            self._log(callback, S("deploy_log.deploy_error", error=str(e)))
            return DeployResult(image=image, status="failed", output=str(e))
        return result

    def _upload_file(self, ssh, target: DeployTarget, content: str) -> str | None:
        """SFTP 写文件。返回 None=成功，返回 str=错误信息"""
        try:
            ssh.exec_command(f"mkdir -p {target.path}")
            sftp = ssh.open_sftp()
            with sftp.file(f"{target.path}/docker-compose.yml", "w") as f:
                f.write(content)
            sftp.close()
            return None
        except Exception as e:
            return str(e)

    def _ssh_run(self, ssh, cmd: str, image: str) -> DeployResult:
        try:
            out, err, exit_code = _exec_on(ssh, cmd)
            return DeployResult(
                image=image,
                status="ok",
                output=(err or out)[:settings.log_truncate_chars],
            )
        except Exception as e:
            return DeployResult(image=image, status="failed", output=str(e))

    def _ssh_exec_stream(self, ssh, cmd: str, callback) -> str:
        """实时流式执行命令（委托给共享实现）"""
        return ssh_exec_stream(ssh, cmd, lambda msg: self._log(callback, msg))

    def stop(self, target: DeployTarget, project: str, **kwargs) -> dict:
        """停止服务：docker-compose down"""
        target_path = kwargs.get("target_path", "")
        if not target_path:
            return {"success": False, "output": "Compose stop: missing target_path"}
        cmd = f"cd {target_path} && docker-compose down"
        try:
            with ssh_session(target, settings.ssh_timeout) as ssh:
                _, stdout, stderr = ssh.exec_command(cmd, timeout=settings.ssh_timeout)
                out = stdout.read().decode(errors="replace").strip()
                err = stderr.read().decode(errors="replace").strip()
                return {"success": True, "output": (err or out)[:settings.log_truncate_chars]}
        except Exception as ex:
            return {"success": False, "output": str(ex)}

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "SSH target host is required"
        if target.mode == "commands":
            if not target.options.get("commands"):
                return "Custom commands are required"
        else:
            if not target.path:
                return "Compose path is required"
        return None
