from .base import Deployer, DeployTarget, DeployResult
from .registry import deployer_registry, DeployerRegistry
from .ssh import SSHDeployer
from .compose import ComposeDeployer
from .k8s import K8sDeployer

# 启动时注册所有部署器
def _register_all():
    deployer_registry.register("ssh", lambda: SSHDeployer())
    deployer_registry.register("compose", lambda: ComposeDeployer())
    deployer_registry.register("k8s", lambda: K8sDeployer())

_register_all()
