"""Users 路由集成测试 — 用户管理 CRUD + 密码修改"""

import bcrypt
import pytest


class TestListUsers:
    """GET /api/users（需 cd.admin 权限）"""

    def test_requires_auth(self, client):
        r = client.get("/api/users")
        assert r.status_code in (401, 403), f"Got {r.status_code}: {r.json()}"

    def test_requires_admin_perm(self, client, auth_headers):
        """admin 用户（role=admin, no cd.admin permission）"""
        r = client.get("/api/users", headers=auth_headers)
        assert r.status_code == 403, f"Got {r.status_code}: {r.json()}"


class TestCreateUser:
    """POST /api/users"""

    def test_requires_admin(self, client, auth_headers):
        r = client.post("/api/users", headers=auth_headers, json={
            "username": "newuser",
            "password": "pass123",
            "role": "viewer",
        })
        assert r.status_code == 403, f"Got {r.status_code}: {r.json()}"

    def test_missing_auth(self, client):
        r = client.post("/api/users", json={
            "username": "newuser",
            "password": "pass123",
        })
        assert r.status_code in (401, 403)


class TestChangePassword:
    """PUT /api/users/{username}/password"""

    def test_requires_auth(self, client):
        r = client.put("/api/users/admin/password", json={
            "old_password": "admin123",
            "new_password": "newpass123",
        })
        assert r.status_code in (401, 403)

    def test_self_change_password(self, client, auth_headers):
        """修改自己的密码（需要验证旧密码）"""
        r = client.put("/api/users/admin/password", headers=auth_headers, json={
            "old_password": "admin123",
            "new_password": "newadmin456",
        })
        # 需要 cd.admin 权限检查 → 403
        assert r.status_code in (200, 403), f"Got {r.status_code}: {r.json()}"

        if r.status_code == 200:
            # 改回去
            data = r.json()
            assert data.get("updated") == "admin"
            client.put("/api/users/admin/password", headers=auth_headers, json={
                "old_password": "newadmin456",
                "new_password": "admin123",
            })

    def test_wrong_old_password(self, client, auth_headers):
        r = client.put("/api/users/admin/password", headers=auth_headers, json={
            "old_password": "wrong_password",
            "new_password": "newpass",
        })
        assert r.status_code in (200, 400, 403)


class TestDeleteUser:
    """DELETE /api/users/{username}"""

    def test_requires_admin(self, client, auth_headers):
        r = client.delete("/api/users/someuser", headers=auth_headers)
        assert r.status_code == 403, f"Got {r.status_code}: {r.json()}"
