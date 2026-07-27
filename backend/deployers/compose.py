"""docker-compose 部署器 — IMAGE/TAG 双变量，支持 --env-file"""

from .base import Deployer, DeployTarget, DeployResult, ssh_session, _exec_on
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

                # 直接 printf 覆盖写入，避免 sed 分隔符/退出码等各种边界问题
                env_update = f"cd {target.path} && printf 'IMAGE={image_name}\\nTAG={tag}\\n' > {env_file}"
                self._ssh_run(ssh, env_update, image)

                # 验证 .env 写入成功（直接看 stdout，忽略 stderr）
                out, _ = _exec_on(ssh, f"cd {target.path} && cat {env_file}")
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

                # 4a. 登录私有仓库
                if is_private and has_creds:
                    self._log(callback, S("deploy_log.registry_login", registry=registry))
                    login_out = self._ssh_run(ssh,
                        f"echo {settings.harbor_password} | docker login {registry} -u {settings.harbor_user} --password-stdin 2>&1", image)
                    if "Login Succeeded" in login_out.output:
                        self._log(callback, S("deploy_log.registry_login_ok"))
                    else:
                        self._log(callback, S("deploy_log.registry_login_fail"))
                elif is_private and not has_creds:
                    self._log(callback, S("deploy_log.registry_no_creds"))

                # 4b. docker compose pull（私有仓库）
                self._log(callback, S("deploy_log.pulling_image"))
                pull_text = self._ssh_exec_stream(ssh, f"cd {target.path} && docker compose {env_flag} pull 2>&1", callback)
                pull_ok = _image_exists(image_name)

                # 4c. 私有仓库失败 → 回退 Docker Hub
                if not pull_ok and is_private:
                    self._log(callback, S("deploy_log.fallback_dockerhub", image=dh_image))
                    self._ssh_run(ssh,
                        f"cd {target.path} && printf 'IMAGE={dh_image}\\nTAG={tag}\\n' > {env_file}", image)
                    self._log(callback, S("deploy_log.pulling_image"))
                    dh_pull = self._ssh_exec_stream(ssh, f"cd {target.path} && docker compose {env_flag} pull 2>&1", callback)
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

                # 5. 部署前版本检查
                self._log(callback, S("deploy_log.checking_version"))
                before = self._ssh_run(ssh,
                    f"cd {target.path} && docker compose ps -q 2>/dev/null | xargs docker inspect --format '{{{{.Name}}}} {{{{.Config.Image}}}}' 2>/dev/null | grep -F '{project_short}'",
                    image)
                self._log(callback, S("deploy_log.current_version"))
                self._log(callback, before.output or "(无)")

                # 6. 执行部署
                self._log(callback, S("deploy_log.starting_deploy"))
                deploy_text = self._ssh_exec_stream(ssh,
                    f"cd {target.path} && docker compose {env_flag} up -d --force-recreate 2>&1", callback)

                # 7. 部署后验证
                self._log(callback, S("deploy_log.waiting_container"))
                self._ssh_run(ssh, "sleep 3", image)

                running = self._ssh_run(ssh,
                    f"cd {target.path} && docker compose ps -q 2>/dev/null | xargs docker inspect --format '{{{{.Name}}}} {{{{.Config.Image}}}}' 2>/dev/null | grep -F '{project_short}'",
                    image)
                if not running.output.strip():
                    running = self._ssh_run(ssh,
                        f"docker ps --format '{{{{.Names}}}} {{{{.Image}}}}' 2>/dev/null | grep -F '{project_short}'", image)

                if running.output and tag in running.output:
                    self._log(callback, S("deploy_log.container_started"))
                    result = DeployResult(image=image, status="ok",
                        output=f"Before version:\n{before.output or '(none)'}\n\nDeploy output:\n{deploy_text}\n\nAfter version:\n{running.output}\n\nVerification: ✅ Deploy succeeded!")
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
                    self._log(callback, running.output or "(无)")
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
            out, err = _exec_on(ssh, cmd)
            return DeployResult(
                image=image,
                status="ok",
                output=(err or out)[:settings.log_truncate_chars],
            )
        except Exception as e:
            return DeployResult(image=image, status="failed", output=str(e))

    def _ssh_exec_stream(self, ssh, cmd: str, callback) -> str:
        """实时流式执行命令，边执行边通过 callback 推送输出"""
        channel = ssh.get_transport().open_session()
        try:
            channel.exec_command(cmd)
            all_output = []
            while not channel.exit_status_ready():
                if channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    for line in data.split("\n"):
                        line = line.strip()
                        if line:
                            self._log(callback, line)
                            all_output.append(line)
                if channel.recv_stderr_ready():
                    err_data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    for line in err_data.split("\n"):
                        line = line.strip()
                        if line:
                            self._log(callback, line)
                            all_output.append(line)
            while channel.recv_ready():
                data = channel.recv(4096).decode("utf-8", errors="replace")
                for line in data.split("\n"):
                    line = line.strip()
                    if line:
                        self._log(callback, line)
                        all_output.append(line)
            return "\n".join(all_output)
        finally:
            channel.close()

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
