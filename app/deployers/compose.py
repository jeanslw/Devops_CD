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

            # 在线编写的 YAML → 先上传到服务器
            if yaml_content:
                content = yaml_content.replace("{image}", image).replace("{tag}", tag).replace("{project}", project)
                err = self._upload_file(target, content)
                if err:
                    return DeployResult(image=image, status="failed", output=f"YAML 上传失败: {err}")

            login = f"echo {settings.harbor_password} | docker login {settings.harbor_registry} -u {settings.harbor_user} --password-stdin 2>/dev/null; " if settings.harbor_password else ""
            cmd = (
                f"{login}"
                f"cd {target.path} && "
                f"IMAGE_TAG={tag} docker compose pull && "
                f"docker compose up -d --force-recreate"
            )

        return self._ssh_run(target, cmd, image)

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
