"""Pydantic 模型单元测试 — 验证请求模型字段和校验"""

import pytest
from pydantic import ValidationError as PydanticValidationError
from backend.models import (
    LoginRequest,
    BotRequest,
    ServerRequest,
    BuildTriggerRequest,
)


class TestLoginRequest:
    def test_valid(self):
        m = LoginRequest(user="admin", password="secret")
        assert m.user == "admin"
        assert m.password == "secret"

    def test_missing_user(self):
        with pytest.raises(PydanticValidationError):
            LoginRequest(password="secret")

    def test_missing_password(self):
        with pytest.raises(PydanticValidationError):
            LoginRequest(user="admin")

    def test_empty_fields_pass_pydantic(self):
        """Pydantic 默认不拦截空字符串，由业务层校验"""
        m = LoginRequest(user="", password="")
        assert m.user == ""


class TestBotRequest:
    def test_valid(self):
        m = BotRequest(
            name="测试Bot",
            type="dingtalk",
            webhook_url="https://oapi.dingtalk.com/robot/send",
        )
        assert m.name == "测试Bot"
        assert m.type == "dingtalk"

    def test_defaults(self):
        m = BotRequest(name="Bot1", webhook_url="https://example.com")
        assert m.type == "custom"
        assert m.template == ""

    def test_missing_webhook(self):
        with pytest.raises(PydanticValidationError):
            BotRequest(name="Bot1")


class TestServerRequest:
    def test_defaults(self):
        m = ServerRequest(name="prod", host="10.0.0.1")
        assert m.port == 22
        assert m.user == "root"
        assert m.auth_type == "password"
        assert m.type == "ssh"
        assert m.tags == ""

    def test_full(self):
        m = ServerRequest(
            name="k8s-cluster",
            host="10.0.1.1",
            port=6443,
            user="k8s-admin",
            auth_type="key",
            ssh_key="-----BEGIN RSA PRIVATE KEY-----",
            type="k8s",
            tags="prod,web",
        )
        assert m.port == 6443
        assert m.type == "k8s"
        assert "prod" in m.tags

    def test_missing_required(self):
        with pytest.raises(PydanticValidationError):
            ServerRequest()
        with pytest.raises(PydanticValidationError):
            ServerRequest(name="test")


class TestBuildTriggerRequest:
    """v1.2.2 新增模型"""

    def test_valid_full(self):
        m = BuildTriggerRequest(ref="main", variables={"KEY": "val"})
        assert m.ref == "main"
        assert m.variables == {"KEY": "val"}

    def test_empty(self):
        m = BuildTriggerRequest()
        assert m.ref == ""
        assert m.variables == {}

    def test_ref_only(self):
        m = BuildTriggerRequest(ref="feature/login")
        assert m.ref == "feature/login"
        assert m.variables == {}

    def test_variables_only(self):
        m = BuildTriggerRequest(variables={"BRANCH": "dev"})
        assert m.ref == ""
        assert m.variables == {"BRANCH": "dev"}

    def test_exported_from_models_package(self):
        """确保 BuildTriggerRequest 已从 models/__init__.py 正确导出"""
        from backend.models import BuildTriggerRequest as BTR2
        m = BTR2(ref="test")
        assert m.ref == "test"
