"""docker-compose 部署器"""

from .base import Deployer, DeployTarget, DeployResult
from app.config import settings


class ComposeDeployer(Deployer):
    """docker-compose 部署 — 远程 YAML / 在线编写 / 自定义命令"""

    def name(self) -> str:
        return "compose"

    def deploy(
        self, target: DeployTarget, image: str, project: str, tag: str
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="缺少目标主机")

        mode = target.mode or "remote"
        yaml_content = target.options.get("yaml_content", "")

        if mode == "commands":
            template = target.options.get("commands", "")
            if not template:
                return DeployResult(image=image, status="failed", output="缺少自定义命令")
            cmd = template.replace("{image}", image).replace("{tag}", tag).replace("{project}", project)
        else:
            if not target.path:
                return DeployResult(image=image, status="failed", output="缺少 compose 路径")
            if yaml_content:
                content = yaml_content.replace("{image}", image).replace("{tag}", tag).replace("{project}", project)
                err = self._upload_file(target, content)
                if err:
                    return DeployResult(image=image, status="failed", output=f"YAML 上传失败: {err}")

            login = f"echo {settings.harbor_password} | docker login {settings.harbor_registry} -u {settings.harbor_user} --password-stdin 2>/dev/null; " if settings.harbor_password else ""
            # 同时写入 .env，手动重启也保留版本
            cmd = (
                f"{login}"
                f"cd {target.path} && "
                f"sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG={tag}/' .env 2>/dev/null; "
                f"grep -q IMAGE_TAG .env 2>/dev/null || echo IMAGE_TAG={tag} >> .env; "
                f"IMAGE_TAG={tag} docker compose pull && "
                f"IMAGE_TAG={tag} docker compose up -d --force-recreate"
            )

        # 部署前查看当前运行版本
        before = self._ssh_run(target,
            f"cd {target.path} && docker compose ps -q 2>/dev/null | xargs docker inspect --format '{{{{.Config.Image}}}}' 2>/dev/null",
            image)

        result = self._ssh_run(target, cmd, image)
        deploy_text = result.output

        # 查看应用新版本
        self._ssh_run(target, "sleep 3", image)
        running = self._ssh_run(target,
            f"cd {target.path} && docker compose ps -q 2>/dev/null | xargs docker inspect --format '{{{{.Name}}}} {{{{.Config.Image}}}}' 2>/dev/null",
            image)
        if not running.output.strip():
            running = self._ssh_run(target,
                f"docker ps --format '{{{{.Names}}}} {{{{.Image}}}}' 2>/dev/null", image)

        result.output = f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}\n\n部署完成！\n\n查看应用新版本:\n{running.output or '(无运行中容器)'}"

        if running.output and tag in running.output:
            result.status = "ok"
            result.output += f"\n\n验证部署: ✅ 部署成功！"
        elif all(s not in deploy_text.lower() for s in ["up", "starting", "started", "created"]):
            result.status = "failed"
            result.output += f"\n\n验证部署: ❌ 部署失败！"
        else:
            result.status = "failed"
            result.output += f"\n\n验证部署: ❌ 部署失败！(版本不匹配)"
        return result

    def _upload_file(self, target: DeployTarget, content: str) -> str | None:
        """SFTP 写文件。返回 None=成功，返回 str=错误信息"""
        try:
            from .base import ssh_connect
            ssh = ssh_connect(target, settings.ssh_timeout)
            ssh.exec_command(f"mkdir -p {target.path}")
            sftp = ssh.open_sftp()
            with sftp.file(f"{target.path}/docker-compose.yml", "w") as f:
                f.write(content)
            sftp.close()
            ssh.close()
            return None
        except Exception as e:
            return str(e)

    def _ssh_run(self, target: DeployTarget, cmd: str, image: str) -> DeployResult:
        try:
            from .base import ssh_connect
            ssh = ssh_connect(target, settings.ssh_timeout)
            _, stdout, stderr = ssh.exec_command(cmd)
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            ssh.close()
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
