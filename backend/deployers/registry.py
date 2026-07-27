"""DeployerRegistry —— 对齐 PHP BuildProviderRegistry"""

from typing import Dict, Callable
from .base import Deployer


class DeployerRegistry:
    """部署器注册表：按名称注册懒加载工厂，按需创建实例"""

    def __init__(self):
        self._factories: Dict[str, Callable[[], Deployer]] = {}

    def register(self, name: str, factory: Callable[[], Deployer]):
        """注册部署器工厂"""
        self._factories[name] = factory

    def create(self, name: str) -> Deployer:
        """创建部署器实例（懒加载）"""
        if name not in self._factories:
            raise ValueError(
                f"不支持的部署类型: {name}，可用: {list(self._factories.keys())}"
            )
        return self._factories[name]()

    def is_registered(self, name: str) -> bool:
        return name in self._factories

    def names(self) -> list:
        return list(self._factories.keys())

    def __len__(self) -> int:
        return len(self._factories)


# 全局单例
deployer_registry = DeployerRegistry()
