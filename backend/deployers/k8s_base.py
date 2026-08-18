"""K8S 子模式部署器基类

不对齐 Deployer ABC 的 deploy 签名，因为 K8S CD 场景需要额外的请求字段。
但通过同一个 DeployerRegistry 管理，路由层统一创建。
"""

from abc import ABC, abstractmethod


class K8sSubDeployer(ABC):
    """K8S CD 子模式部署器基类（kubectl | argocd | helm | fluxcd）"""

    @abstractmethod
    def cd_type(self) -> str:
        """子模式标识：kubectl | argocd | helm | fluxcd"""
        ...

    @abstractmethod
    def deploy(
        self,
        req,
        image: str,
        project: str,
        host: str,
        port: int = 22,
        user: str = "root",
        pwd: str = "",
        ssh_key: str = "",
        callback=None,
    ) -> dict:
        """执行部署，返回 {"success": bool, "output": str}"""
        ...

    def validate(self, _target=None, **kwargs) -> str | None:
        """校验请求参数有效性，返回 None 通过，否则返回错误信息。
        双调用兼容：
        - DeployService.execute 调法：validate(target) → target 位置参数
        - K8sSubDeployer 子类扩展调法：validate(req=...) → kwargs
        """
        return None

    def stop(
        self, req, project: str, host: str, port: int = 22, user: str = "root", pwd: str = "", ssh_key: str = ""
    ) -> dict:
        """停止服务。默认抛出 NotImplementedError，子类按需覆盖。"""
        raise NotImplementedError(f"{self.name()} does not support stop")

    def name(self) -> str:
        """注册名，与 registry 键对应：k8s/<cd_type>"""
        return f"k8s/{self.cd_type()}"
