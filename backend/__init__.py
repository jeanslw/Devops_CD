"""Devops-Glue CD Service — FastAPI 部署执行器（SSH / docker-compose / K8s）

版本号统一在此维护：发版时同步更新 __version__，main.py 的 FastAPI(version=...)
与 GET /api/info 均引用本常量，后端不出现第二处硬编码。
"""

__version__ = "1.5.0"
