"""统一异常体系 — 替代裸 HTTPException，让业务层抛出语义异常，路由层统一处理"""


class AppException(Exception):
    """应用层异常基类

    error_key:  前端 i18n 翻译 key，如 "errors.server_not_found"
    error_params: i18n 插值参数，如 {"name": "my-server"}
    message:    降级兜底文本（无前端翻译时展示）
    """
    def __init__(self, message: str = "", status_code: int = 400,
                 detail: dict | None = None,
                 error_key: str | None = None,
                 error_params: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}
        self.error_key = error_key
        self.error_params = error_params or {}


class NotFoundError(AppException):
    """资源不存在 (404)"""
    def __init__(self, message: str = "资源不存在", detail: dict | None = None,
                 error_key: str | None = "errors.not_found",
                 error_params: dict | None = None):
        super().__init__(message, 404, detail, error_key, error_params)


class ConflictError(AppException):
    """资源冲突 (409) — 如名称重复"""
    def __init__(self, message: str = "资源冲突", detail: dict | None = None,
                 error_key: str | None = "errors.conflict",
                 error_params: dict | None = None):
        super().__init__(message, 409, detail, error_key, error_params)


class ValidationError(AppException):
    """参数校验失败 (400)"""
    def __init__(self, message: str = "参数无效", detail: dict | None = None,
                 error_key: str | None = "errors.validation",
                 error_params: dict | None = None):
        super().__init__(message, 400, detail, error_key, error_params)


class ServiceUnavailableError(AppException):
    """外部服务不可用 (503) — 如 Harbor / CI 不可达"""
    def __init__(self, message: str = "服务不可用", detail: dict | None = None,
                 error_key: str | None = "errors.service_unavailable",
                 error_params: dict | None = None):
        super().__init__(message, 503, detail, error_key, error_params)


class DatabaseError(AppException):
    """数据库错误 (500)"""
    def __init__(self, message: str = "数据库错误", detail: dict | None = None,
                 error_key: str | None = "errors.database",
                 error_params: dict | None = None):
        super().__init__(message, 500, detail, error_key, error_params)
