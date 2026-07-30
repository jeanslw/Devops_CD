"""统一异常体系 — 替代裸 HTTPException，让业务层抛出语义异常，路由层统一处理"""


class AppException(Exception):
    """应用层异常基类"""
    def __init__(self, message: str, status_code: int = 400, detail: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}


class NotFoundError(AppException):
    """资源不存在 (404)"""
    def __init__(self, message: str = "资源不存在", detail: dict | None = None):
        super().__init__(message, 404, detail)


class ConflictError(AppException):
    """资源冲突 (409) — 如名称重复"""
    def __init__(self, message: str = "资源冲突", detail: dict | None = None):
        super().__init__(message, 409, detail)


class ValidationError(AppException):
    """参数校验失败 (400)"""
    def __init__(self, message: str = "参数无效", detail: dict | None = None):
        super().__init__(message, 400, detail)


class ServiceUnavailableError(AppException):
    """外部服务不可用 (503) — 如 Harbor / CI 不可达"""
    def __init__(self, message: str = "服务不可用", detail: dict | None = None):
        super().__init__(message, 503, detail)


class DatabaseError(AppException):
    """数据库错误 (500)"""
    def __init__(self, message: str = "数据库错误", detail: dict | None = None):
        super().__init__(message, 500, detail)
