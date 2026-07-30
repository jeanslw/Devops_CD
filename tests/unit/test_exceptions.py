"""异常体系单元测试 — 验证状态码、消息、继承关系"""

import pytest
from backend.exceptions import (
    AppException,
    NotFoundError,
    ConflictError,
    ValidationError,
    ServiceUnavailableError,
    DatabaseError,
)


class TestAppException:
    """基类功能"""

    def test_default_status(self):
        ex = AppException("错了")
        assert ex.status_code == 400
        assert ex.message == "错了"
        assert ex.detail == {}

    def test_custom_status(self):
        ex = AppException("未授权", status_code=401)
        assert ex.status_code == 401

    def test_custom_detail(self):
        ex = AppException("失败", detail={"field": "name"})
        assert ex.detail == {"field": "name"}

    def test_is_exception(self):
        ex = AppException("测试")
        assert isinstance(ex, Exception)


class TestNotFoundError:
    def test_default(self):
        ex = NotFoundError()
        assert ex.status_code == 404
        assert ex.message == "资源不存在"

    def test_custom_message(self):
        ex = NotFoundError("服务器不存在")
        assert ex.status_code == 404
        assert ex.message == "服务器不存在"

    def test_with_detail(self):
        ex = NotFoundError("项目未找到", {"id": 999})
        assert ex.detail == {"id": 999}

    def test_inherits_app_exception(self):
        assert issubclass(NotFoundError, AppException)


class TestConflictError:
    def test_default(self):
        ex = ConflictError()
        assert ex.status_code == 409
        assert ex.message == "资源冲突"

    def test_custom_message(self):
        ex = ConflictError("Bot 'prod-通知' 已存在")
        assert ex.status_code == 409
        assert ex.message == "Bot 'prod-通知' 已存在"

    def test_inherits_app_exception(self):
        assert issubclass(ConflictError, AppException)


class TestValidationError:
    def test_default(self):
        ex = ValidationError()
        assert ex.status_code == 400
        assert ex.message == "参数无效"

    def test_custom_message(self):
        ex = ValidationError("用户名和密码不能为空")
        assert ex.status_code == 400
        assert ex.message == "用户名和密码不能为空"

    def test_e2e_forward_compat(self):
        """v1.2.2 关键约束：状态码必须为 400（兼容前端 HTTPException(400) 替换）"""
        ex = ValidationError("测试")
        assert ex.status_code == 400


class TestServiceUnavailableError:
    def test_default(self):
        ex = ServiceUnavailableError()
        assert ex.status_code == 503

    def test_custom_message(self):
        ex = ServiceUnavailableError("Harbor 不可达")
        assert ex.status_code == 503
        assert ex.message == "Harbor 不可达"


class TestDatabaseError:
    def test_default(self):
        ex = DatabaseError()
        assert ex.status_code == 500

    def test_custom_message(self):
        ex = DatabaseError("写入失败")
        assert ex.status_code == 500
        assert ex.message == "写入失败"
