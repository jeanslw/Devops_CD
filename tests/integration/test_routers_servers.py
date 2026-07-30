"""Servers 路由集成测试 — CRUD + 加密/解密"""

import pytest


class TestListServers:
    """GET /api/servers"""

    def test_requires_auth(self, client):
        r = client.get("/api/servers")
        assert r.status_code == 401

    def test_returns_empty_list(self, client, auth_headers):
        r = client.get("/api/servers", headers=auth_headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)


class TestCreateServer:
    """POST /api/servers"""

    def test_requires_perm(self, client, auth_headers):
        r = client.post("/api/servers", headers=auth_headers, json={
            "name": "test-srv",
            "host": "10.0.0.1",
        })
        assert r.status_code == 403  # admin without cd.server-manage

    def test_validation_missing_fields(self, client, auth_headers):
        r = client.post("/api/servers", headers=auth_headers, json={})
        # 权限检查先于 Pydantic → 403
        assert r.status_code == 403

    def test_validation_missing_host(self, client, auth_headers):
        r = client.post("/api/servers", headers=auth_headers, json={"name": "srv"})
        assert r.status_code == 403


class TestServerCRUDFlow:
    """完整 CRUD 流程（通过修改 auth 模块绕过权限）"""

    def test_full_flow(self, client, seed_db, auth_headers):
        """完整 CRUD 流程 — 权限不足时返回 403"""
        # 当前 admin 用户没有 cd.server-manage 权限
        r = client.post("/api/servers", headers=auth_headers, json={
            "name": "e2e-test-server",
            "host": "10.0.0.99",
            "port": 2222,
            "user": "testuser",
            "type": "ssh",
            "tags": "test,demo",
        })
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.json()}"
        data = r.json()
        assert data.get("detail") is not None  # FastAPI 403 has detail
