from .base import Deployer, DeployTarget, DeployResult
from .registry import deployer_registry, DeployerRegistry
from .ssh import SSHDeployer
from .compose import ComposeDeployer
from .k8s import K8sDeployer
from .k8s_kubectl import KubectlDeployer
from .k8s_argocd import ArgoCDDeployer
from .k8s_helm import HelmDeployer
from .k8s_fluxcd import FluxCDDeployer

# 启动时注册所有部署器
def _register_all():
    deployer_registry.register("ssh", lambda: SSHDeployer())
    deployer_registry.register("compose", lambda: ComposeDeployer())
    deployer_registry.register("k8s", lambda: K8sDeployer())
    # K8S 子模式部署器
    deployer_registry.register("k8s/kubectl", lambda: KubectlDeployer())
    deployer_registry.register("k8s/argocd", lambda: ArgoCDDeployer())
    deployer_registry.register("k8s/helm", lambda: HelmDeployer())
    deployer_registry.register("k8s/fluxcd", lambda: FluxCDDeployer())

_register_all()
