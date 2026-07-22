"""docker-compose 部署器"""

from .base import Deployer, DeployTarget, DeployResult, ssh_session, _exec_on
from app.config import settings


class ComposeDeployer(Deployer):
    """docker-compose 部署 — 远程 YAML / 在线编写 / 自定义命令"""

    def name(self) -> str:
        return "compose"

    def deploy(
        self, target: DeployTarget, image: str, project: str, tag: str,
        callback=None,
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="缺少目标主机")

        mode = target.mode or "remote"
        yaml_content = target.options.get("yaml_content", "")

        if mode == "commands":
            template = target.options.get("commands", "")
            if not template:
                return DeployResult(image=image, status="failed", output="缺少自定义命令")
            image_name = image.split(":")[0]
            cmd = template.replace("{image}", image).replace("{image_name}", image_name).replace("{tag}", tag).replace("{project}", project)
        else:
            if not target.path:
                return DeployResult(image=image, status="failed", output="缺少 compose 路径")

        try:
            project_short = project.split("/")[-1]
            with ssh_session(target, settings.ssh_timeout) as ssh:
                self._log(callback, "正在验证路径...")
                path_check = self._ssh_run(ssh, f"test -d {target.path} && echo 'OK' || echo 'NOT_FOUND'", image)
                if path_check.output.strip() != "OK":
                    self._log(callback, f"❌ 部署失败：路径不存在或路径错误 - {target.path}")
                    return DeployResult(image=image, status="failed", output=f"部署失败：路径不存在或路径错误 - {target.path}")
                self._log(callback, f"✅ 路径验证通过: {target.path}")

                if mode != "commands" and yaml_content:
                    self._log(callback, "正在上传 docker-compose.yml...")
                    image_name = image.split(":")[0]
                    content = yaml_content.replace("{image}", image).replace("{image_name}", image_name).replace("{tag}", tag).replace("{project}", project)
                    err = self._upload_file(ssh, target, content)
                    if err:
                        self._log(callback, f"❌ YAML 上传失败: {err}")
                        return DeployResult(image=image, status="failed", output=f"YAML 上传失败: {err}")
                    self._log(callback, "✅ YAML 上传成功")

                if mode != "commands":
                    login = f"echo {settings.harbor_password} | docker login {settings.harbor_registry} -u {settings.harbor_user} --password-stdin 2>/dev/null; " if settings.harbor_password else ""
                    cmd = (
                        f"{login}"
                        f"cd {target.path} && "
                        f"sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG={tag}/' .env 2>/dev/null; "
                        f"grep -q IMAGE_TAG .env 2>/dev/null || echo IMAGE_TAG={tag} >> .env; "
                        f"IMAGE_TAG={tag} docker compose pull && "
                        f"IMAGE_TAG={tag} docker compose up -d --force-recreate"
                    )

                self._log(callback, "正在查看当前运行版本...")
                before = self._ssh_run(ssh,
                    f"cd {target.path} && docker compose ps -q 2>/dev/null | xargs docker inspect --format '{{{{.Name}}}} {{{{.Config.Image}}}}' 2>/dev/null | grep -F '{project_short}'",
                    image)
                self._log(callback, f"当前运行版本:\n{before.output or '(无)'}")

                self._log(callback, "\n正在验证应用一致性...")
                if not before.output.strip():
                    all_containers = self._ssh_run(ssh, "docker ps --format '{{{{.Names}}}}' 2>/dev/null", image)
                    running_names = all_containers.output.strip()
                    if running_names:
                        self._log(callback, f"❌ 部署失败：未找到应用 [{project_short}]，当前运行的容器：\n{running_names}")
                        return DeployResult(image=image, status="failed", output=f"部署失败：未找到应用 [{project_short}]，当前运行的容器：\n{running_names}")
                    else:
                        self._log(callback, f"⚠️ 未检测到运行中的容器，将首次部署 [{project_short}]")
                else:
                    self._log(callback, f"✅ 应用 [{project_short}] 验证通过")

                self._log(callback, "\n开始部署...")
                result = self._ssh_run(ssh, cmd, image)
                deploy_text = result.output
                self._log(callback, deploy_text)

                self._log(callback, "\n等待容器启动...")
                self._ssh_run(ssh, "sleep 3", image)

                running = self._ssh_run(ssh,
                    f"cd {target.path} && docker compose ps -q 2>/dev/null | xargs docker inspect --format '{{{{.Name}}}} {{{{.Config.Image}}}}' 2>/dev/null | grep -F '{project_short}'",
                    image)
                if not running.output.strip():
                    running = self._ssh_run(ssh,
                        f"docker ps --format '{{{{.Names}}}} {{{{.Image}}}}' 2>/dev/null | grep -F '{project_short}'", image)

                if running.output and tag in running.output:
                    self._log(callback, "✅ 容器启动完成！")
                    result.status = "ok"
                    result.output = f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}\n\n部署后运行版本:\n{running.output}\n\n验证部署: ✅ 部署成功！"
                    self._log(callback, "\n部署后运行版本:\n" + running.output)
                    self._log(callback, "\n验证部署: ✅ 部署成功！")
                elif not running.output.strip():
                    self._log(callback, "❌ 容器启动失败！")
                    result.status = "failed"
                    result.output = f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}\n\n部署后运行版本: (无运行中容器)\n\n验证部署: ❌ 部署失败！(容器未启动)"
                    self._log(callback, "\n部署后运行版本: (无运行中容器)")
                    self._log(callback, "\n验证部署: ❌ 部署失败！(容器未启动)")
                elif all(s not in deploy_text.lower() for s in ["up", "starting", "started", "created"]):
                    self._log(callback, "❌ 容器启动失败！")
                    result.status = "failed"
                    result.output = f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}\n\n部署后运行版本:\n{running.output or '(无)'}\n\n验证部署: ❌ 部署失败！"
                    self._log(callback, "\n部署后运行版本:\n" + (running.output or "(无)"))
                    self._log(callback, "\n验证部署: ❌ 部署失败！")
                else:
                    self._log(callback, "❌ 容器启动失败！")
                    result.status = "failed"
                    result.output = f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}\n\n部署后运行版本:\n{running.output}\n\n验证部署: ❌ 部署失败！(版本不匹配)"
                    self._log(callback, "\n部署后运行版本:\n" + running.output)
                    self._log(callback, "\n验证部署: ❌ 部署失败！(版本不匹配)")
        except Exception as e:
            self._log(callback, f"\n❌ 部署失败: {e}")
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

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "目标主机不能为空"
        if target.mode == "commands":
            if not target.options.get("commands"):
                return "自定义命令不能为空"
        else:
            if not target.path:
                return "需要指定 compose 路径"
        return None
