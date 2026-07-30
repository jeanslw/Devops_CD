"""Auth 路由集成测试 — 登录/Token 验证/权限"""

import base64
import pytest


class TestLogin:
    """POST /api/login"""

    def test_login_success(self, client):
        r = client.post("/api/login", json={
            "user": "admin",
            "password": "admin123",
        })
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        # Token 应是 base64 编码
        decoded = base64.b64decode(data["token"]).decode()
        assert decoded.startswith("admin:")

    def test_login_wrong_password(self, client):
        r = client.post("/api/login", json={
            "user": "admin",
            "password": "wrong_password",
        })
        assert r.status_code == 401
        data = r.json()
        assert data["success"] is False
        assert "错误" in data.get("error", "")

    def test_login_nonexistent_user(self, client):
        r = client.post("/api/login", json={
            "user": "nonexistent",
            "password": "whatever",
        })
        assert r.status_code == 401

    def test_login_missing_fields(self, client):
        r = client.post("/api/login", json={})
        assert r.status_code == 422  # Pydantic validation

    def test_login_missing_password(self, client):
        r = client.post("/api/login", json={"user": "admin"})
        assert r.status_code == 422


class TestTokenValidation:
    """Token 验证"""

    def test_valid_token_on_protected_route(self, client, auth_headers):
        r = client.get("/api/bots", headers=auth_headers)
        assert r.status_code == 200

    def test_no_token(self, client):
        r = client.get("/api/bots")
        assert r.status_code == 401

    def test_invalid_token(self, client):
        r = client.get("/api/bots", headers={
            "Authorization": "Bearer invalid-token-here"
        })
        assert r.status_code == 401

    def test_empty_token(self, client):
        r = client.get("/api/bots", headers={
            "Authorization": "Bearer "
        })
        assert r.status_code == 401

    def test_tampered_token(self, client):
        """篡改过的 token 应被拒绝"""
        # 创建合法的 token 格式但 hash 不对
        fake_token = base64.b64encode(
            "admin:tampered_hash_12345".encode()
        ).decode()
        r = client.get("/api/bots", headers={
            "Authorization": f"Bearer {fake_token}"
        })
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.json()}"

    def test_no_bearer_prefix(self, client):
        r = client.get("/api/bots", headers={
            "Authorization": "token-123"
        })
        assert r.status_code == 401 or r.status_code == 403

    def test_non_base64_token(self, client):
        r = client.get("/api/bots", headers={
            "Authorization": "Bearer !!!not-base64!!!"
        })
        assert r.status_code == 401


class TestGetCurrentUser:
    """Token → 用户信息"""

    def test_token_returns_user(self, client, auth_token):
        """验证 get_current_user 依赖"""
        r = client.get("/api/users", headers={
            "Authorization": f"Bearer {auth_token}"
        })
        # admin 用户可能没有 cd.admin 权限，所以 403 也在预期内
        assert r.status_code in (200, 403)

    def test_401_error_format(self, client):
        """401 错误响应格式统一"""
        r = client.get("/api/bots")
        data = r.json()
        assert "detail" in data
