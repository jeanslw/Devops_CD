"""OIDC 身份缓存 / 登录路由 / 双轨鉴权回归(L1a,见 docs/SSO_OIDC_ZH.md §3.2 / §4 / §7 / §11).

三组语义各有专门的用例:
  1. glue_identity:窗口内零调用,role 变化才重取权限,角色 404 fail-closed(不降级为空权限),
     软失败窗口(last-known-good)与超窗 fail-closed,明确否定永不沿用旧身份;
  2. routers/oidc:state 单次使用,invalid_grant 静默重走一次,
     准入失败回 /login?error=<error_key>;
  3. auth 请求路径:verify_token / get_current_user 不读任何共享表,
     通配权限 "*" 放行,停用/身份不可用即删会话行.

不连网络也不连库:Glue 调用打桩(`glue_identity._glue_get`),Database 用 MagicMock.

运行(项目根):
    python -m pytest backend/tests/test_oidc_login.py -q
"""

import os
import sys
import time
import types
import unittest
from contextlib import ExitStack
from typing import Any, cast
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import HTTPException, Request

from backend import auth as auth_module
from backend.auth import enforce_deploy_perm, get_current_user, require_perm, verify_token
from backend.config import settings
from backend.exceptions import AppException
from backend.routers import oidc as oidc_router
from backend.services import glue_identity


class _OidcSettingsMixin:
    """settings 是全局单例:用例自己保存/恢复 OIDC 相关字段,避免相互污染."""

    FIELDS = (
        "oidc_issuer",
        "oidc_client_secret",
        "oidc_redirect_uri",
        "oidc_revalidate_seconds",
        "oidc_stale_grace_seconds",
        "oidc_id_token_max_skew",
        "ci_api_url",
        "ci_api_token",
        "allow_empty_systems",
    )

    def setUp(self):
        super().setUp()
        self._saved = {f: getattr(settings, f) for f in self.FIELDS}
        settings.oidc_issuer = "https://glue.example.com"
        # 必填三项给全:局部 OIDC 预检(errors.oidc_not_configured)不应在这些用例里触发
        settings.oidc_client_secret = "test-secret"
        settings.oidc_redirect_uri = "http://cd.example.com/api/auth/oidc/callback"
        settings.oidc_revalidate_seconds = 60
        settings.oidc_stale_grace_seconds = 600
        settings.ci_api_url = "http://glue:8080"
        settings.ci_api_token = "dg_test"
        glue_identity.reset_cache()

    def tearDown(self):
        glue_identity.reset_cache()
        for f, v in self._saved.items():
            setattr(settings, f, v)
        super().tearDown()


def _ident(role: str = "dev", systems: str = "cd", status: Any = 1, permissions: list | None = None) -> dict:
    return {
        "username": "alice",
        "role": role,
        "systems": systems,
        "status": status,
        "permissions": ["cd.deploy.single"] if permissions is None else permissions,
        "usable": True,
        "checked_at": time.time(),
        "last_ok_at": time.time(),
    }


class _FakeGlue:
    """`glue_identity._glue_get` 的打桩:按路径给响应并记录调用序列(验证缓存与负缓存语义)."""

    def __init__(self, users: dict | None = None, perms: dict | None = None):
        self.users = users if users is not None else {}
        self.perms = perms if perms is not None else {}
        self.calls: list[str] = []
        self.down = False

    def __call__(self, path: str) -> tuple[int, Any]:
        self.calls.append(path)
        if self.down:
            raise glue_identity._GlueUnavailable("connection refused")
        if path.startswith("/api/rbac/users/"):
            name = path.rsplit("/", 1)[1]
            return (200, self.users[name]) if name in self.users else (404, None)
        if path.startswith("/api/rbac/roles/") and path.endswith("/permissions"):
            role = path[len("/api/rbac/roles/") : -len("/permissions")]
            return (200, {"permissions": self.perms[role]}) if role in self.perms else (404, None)
        raise AssertionError(f"未预期的 Glue 路径：{path}")

    def count(self, needle: str) -> int:
        return len([c for c in self.calls if needle in c])


def _glue(role: str = "dev", perms: list | dict | None = None, **user_over: Any) -> _FakeGlue:
    user = {"username": "Alice", "role": role, "systems": "cd", "status": 1}
    user.update(user_over)
    if perms is None:
        role_perms: dict = {role: ["cd.deploy.single"]}
    elif isinstance(perms, dict):
        role_perms = dict(perms)  # 直接给"角色 → 权限"映射:{} 即"该角色在 Glue 中不存在(404)"
    else:
        role_perms = {role: perms}
    return _FakeGlue(users={"alice": user}, perms=role_perms)


def _age_identity(username: str = "alice", seconds: int = 3600) -> None:
    """把缓存条目伪造成'已过期':避免用例依赖真实等待(窗口默认 60s)."""
    glue_identity._identity_cache[username]["checked_at"] -= seconds


def _age_last_ok(username: str = "alice", seconds: int = 3600) -> None:
    """只老化 last_ok_at:模拟'Glue 已不可达这么久',用于软失败窗口边界."""
    glue_identity._identity_cache[username]["last_ok_at"] -= seconds


def _age_role_permissions(role: str = "dev", seconds: int = 3600) -> None:
    """老化角色权限缓存:模拟'该角色的权限清单到了该重新拉取的时间'."""
    glue_identity._perm_cache[role]["checked_at"] -= seconds


def _req(cookies: dict | None = None, accept: str = "*/*") -> Request:
    """假 Request:Accept 默认 `*/*`(XHR / fetch / 脚本形态);整页导航用例显式传 text/html.

    失败呈现方式按 Accept 分流(§7.1):导航 → 302 回登录页弹提示;XHR → JSON + 状态码.
    """
    return cast(Request, types.SimpleNamespace(cookies=cookies or {}, headers={"accept": accept}))


# 浏览器地址栏 / 登录按钮产生的整页跳转(Chrome 形态;关键是含 text/html)
NAV_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class TestGlueIdentityCache(_OidcSettingsMixin, unittest.TestCase):
    """身份/权限的进程内缓存与惰性复核(§3.2):稳定态每窗口 1 次调用,role 变化才 +1."""

    def test_first_call_fetches_user_and_role_permissions(self):
        fake = _glue(perms=["cd.deploy.single", "cd.webshell"])
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            ident = glue_identity.get_identity("Alice")
        self.assertEqual(ident["username"], "alice")  # strip + lower(Glue rbac 端会归一)
        self.assertEqual(ident["role"], "dev")  # role 原样透传,不 lower
        self.assertEqual(ident["permissions"], ["cd.deploy.single", "cd.webshell"])
        self.assertEqual(fake.count("/api/rbac/users/"), 1)
        self.assertEqual(fake.count("/permissions"), 1)

    def test_within_window_is_zero_glue_calls(self):
        fake = _glue()
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            glue_identity.get_identity("alice")
            glue_identity.get_identity("alice")
            glue_identity.get_identity("ALICE")
        self.assertEqual(len(fake.calls), 2)  # 只有首次的 users + permissions

    def test_role_permissions_refreshed_once_per_window_per_role(self):
        """同角色也要按窗口刷新权限(用例 D:去掉某个 perm_key 必须在一个窗口内生效);
        代价是'每角色每窗口 1 次',与使用者数量无关 -- 第 2 个用户命中角色权限缓存."""
        fake = _glue()
        fake.users["bob"] = {"username": "bob", "role": "dev", "systems": "cd", "status": 1}
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            glue_identity.get_identity("alice")
            glue_identity.get_identity("bob")  # 同角色:命中角色权限缓存,只多一次 users 调用
            _age_identity("alice")
            _age_role_permissions()
            ident = glue_identity.get_identity("alice")
        self.assertEqual(ident["permissions"], ["cd.deploy.single"])
        self.assertEqual(fake.count("/api/rbac/users/"), 3)
        self.assertEqual(fake.count("/permissions"), 2)  # 首次 + 窗口到期那次(bob 没有额外开销)

    def test_permission_removed_from_role_takes_effect_within_window(self):
        # 用例 D:同角色被去掉 cd.deploy.approve → 一个窗口内必须反映到权限清单
        fake = _glue(perms=["cd.deploy.single", "cd.deploy.approve"])
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            self.assertIn("cd.deploy.approve", glue_identity.get_identity("alice")["permissions"])
            fake.perms["dev"] = ["cd.deploy.single"]
            _age_identity()
            _age_role_permissions()
            ident = glue_identity.get_identity("alice")
        self.assertEqual(ident["permissions"], ["cd.deploy.single"])

    def test_role_change_refetches_permissions(self):
        fake = _glue()
        fake.perms["ops"] = ["cd.deploy-manage"]
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            glue_identity.get_identity("alice")
            _age_identity()
            _age_role_permissions()
            fake.users["alice"]["role"] = "ops"
            ident = glue_identity.get_identity("alice")
        self.assertEqual(ident["role"], "ops")
        self.assertEqual(ident["permissions"], ["cd.deploy-manage"])
        self.assertEqual(fake.count("/permissions"), 2)

    def test_empty_role_yields_no_permissions_without_role_lookup(self):
        fake = _glue(role="", perms=[])
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            ident = glue_identity.get_identity("alice")
        self.assertEqual(ident["permissions"], [])
        self.assertEqual(fake.count("/permissions"), 0)

    def test_invalid_username_fails_closed_without_glue_call(self):
        fake = _glue()
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            for bad in ("", "   ", "x" * 300):
                with self.assertRaises(AppException) as ctx:
                    glue_identity.get_identity(bad)
                self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(fake.calls, [])


def _fake_db() -> MagicMock:
    """MagicMock 代替 Database:闸门要'已启用 + 表建全',会话查询返回 alice 的有效会话."""
    db = MagicMock()
    db.driver = "sqlite"
    db.cd_missing_tables.return_value = []
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"username": "alice", "expires_at": time.time() + 3600}
    db.conn.return_value.__enter__.return_value = conn
    return db


class TestGlueIdentityFailClosed(_OidcSettingsMixin, unittest.TestCase):
    """角色悬空 / 账号消失 / Glue 不可达三态的区别(§4.2 / §8.4 / 用例 G)."""

    def test_unknown_user_fails_closed_with_401(self):
        fake = _FakeGlue(users={}, perms={})
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            with self.assertRaises(AppException) as ctx:
                glue_identity.get_identity("ghost")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.error_key, "errors.identity_unavailable")

    def test_role_404_fails_closed_never_downgrades_to_empty_permissions(self):
        fake = _glue(role="missing-role", perms={})  # perms={} 即 该角色在 Glue 中不存在(404)
        with (
            patch.object(glue_identity, "_glue_get", side_effect=fake),
            self.assertLogs("glue_identity", level="WARNING") as logs,
        ):
            with self.assertRaises(AppException) as ctx:
                glue_identity.get_identity("alice")
            # 负缓存命中:窗口内再取零 Glue 调用,但仍然 fail-closed
            with self.assertRaises(AppException) as again:
                glue_identity.get_identity("alice")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.error_key, "errors.identity_unavailable")
        self.assertEqual(again.exception.status_code, 401)
        self.assertEqual(len(fake.calls), 2)  # users + permissions 各一次,不重复打 Glue
        # 限流 WARN:一条日志里必须带角色名(否则排查"角色悬空"时没有抓手)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("missing-role", logs.output[0])
        self.assertFalse(glue_identity._identity_cache["alice"]["usable"])

    def test_role_restored_recovers_within_one_window(self):
        fake = _glue(role="missing-role", perms={})
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            with self.assertRaises(AppException):
                glue_identity.get_identity("alice")
            # 管理员补回角色行 + 窗口到期:≤ 1 个窗口自动恢复(用例 G)
            fake.perms["missing-role"] = ["cd.deploy.single"]
            glue_identity._role_negative["missing-role"] = 0.0
            _age_identity()
            ident = glue_identity.get_identity("alice")
        self.assertEqual(ident["permissions"], ["cd.deploy.single"])
        self.assertTrue(ident["usable"])

    def test_role_404_does_not_keep_last_known_good_permissions(self):
        # 先成功建立 last-known-good,再让角色从 Glue 消失:必须拒绝,不沿用旧权限 / 降为空权限
        fake = _glue(perms=["cd.deploy-manage"])
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            glue_identity.get_identity("alice")
            del fake.perms["dev"]
            _age_identity()
            _age_role_permissions()  # 角色权限缓存也过期 即 真的去问 Glue,拿到 404
            with self.assertRaises(AppException) as ctx:
                glue_identity.get_identity("alice")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_deleted_account_drops_last_known_good(self):
        fake = _glue()
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            glue_identity.get_identity("alice")
            del fake.users["alice"]
            _age_identity()
            with self.assertRaises(AppException) as ctx:
                glue_identity.get_identity("alice")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertNotIn("alice", glue_identity._identity_cache)  # 明确否定:不保留缓存


class TestGlueIdentitySoftFail(_OidcSettingsMixin, unittest.TestCase):
    """Glue 不可达:复核窗口 + 软失败窗口(§4.2)--窗口内沿用 last-known-good,超窗 fail-closed."""

    def test_within_grace_uses_last_known_good(self):
        fake = _glue(perms=["cd.deploy.single"])
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            glue_identity.get_identity("alice")
            fake.down = True
            _age_identity(seconds=120)  # 超出复核窗口 即 触发一次复核(失败)
            with self.assertLogs("glue_identity", level="WARNING"):
                ident = glue_identity.get_identity("alice")
        self.assertEqual(ident["permissions"], ["cd.deploy.single"])

    def test_beyond_grace_fails_closed_503(self):
        fake = _glue()
        settings.oidc_stale_grace_seconds = 600
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            glue_identity.get_identity("alice")
            fake.down = True
            _age_identity()
            _age_last_ok(seconds=3600)  # 已不可达 1 小时 > 软失败窗口 600s
            with self.assertRaises(AppException) as ctx:
                glue_identity.get_identity("alice")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.service_unavailable")

    def test_zero_grace_fails_closed_immediately(self):
        settings.oidc_stale_grace_seconds = 0
        fake = _glue()
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            glue_identity.get_identity("alice")
            fake.down = True
            _age_identity()
            with self.assertRaises(AppException) as ctx:
                glue_identity.get_identity("alice")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_no_cached_identity_fails_closed_immediately(self):
        fake = _glue()
        fake.down = True
        with patch.object(glue_identity, "_glue_get", side_effect=fake):
            with self.assertRaises(AppException) as ctx:
                glue_identity.get_identity("alice")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.service_unavailable")

    def test_missing_ci_api_token_is_unavailable_not_denied(self):
        # oidc 模式下 CI_API_URL / CI_API_TOKEN 是身份来源(§3.1):缺了就是"Glue 不可用",
        # 不能当成"无权限"静默放行 -- 这里走真实 _glue_get(不连网,缺配置直接判不可用)
        settings.ci_api_token = ""
        with self.assertRaises(AppException) as ctx:
            glue_identity.get_identity("alice")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.service_unavailable")


class TestOidcRoutes(_OidcSettingsMixin, unittest.TestCase):
    """routers/oidc:可用性由模式判定 + state 单次使用 + 失败语义分三类(§7.1 / §11)."""

    def setUp(self):
        super().setUp()
        self.db = _fake_db()

    def _callback(self, **kw: Any):
        return oidc_router.callback(request=_req({"cd_oidc_state": "st-1.no-1"}), db=self.db, **kw)

    def _stub_token_flow(self, ident: dict | None = None, get_identity=None):
        """把'兑 code + 验 token'打桩成成功,剩下只测准入与跳转."""
        stack = ExitStack()
        stack.enter_context(patch.object(oidc_router.oidc, "exchange_code", return_value={"id_token": "t"}))
        stack.enter_context(patch.object(oidc_router.oidc, "verify_id_token", return_value="alice"))
        stack.enter_context(
            patch.object(
                oidc_router.glue_identity,
                "get_identity",
                get_identity if get_identity is not None else MagicMock(return_value=ident),
            )
        )
        return stack

    def test_start_redirects_and_sets_single_use_cookie(self):
        with patch.object(
            oidc_router.oidc, "authorize_url", return_value="https://glue.example.com/oauth/authorize?state=st"
        ) as url:
            resp = oidc_router.start(_req(), db=self.db)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "https://glue.example.com/oauth/authorize?state=st")
        state, nonce = url.call_args.args
        cookie = resp.headers["set-cookie"]
        self.assertIn(f"{state}.{nonce}", cookie)
        self.assertIn("HttpOnly", cookie)  # state/nonce 不暴露给 JS

    def test_start_passes_discovery_error_through(self):
        with patch.object(oidc_router.oidc, "authorize_url", side_effect=oidc_router.oidc.OidcError("issuer 不一致")):
            with self.assertRaises(AppException) as ctx:
                oidc_router.start(_req(), db=self.db)
        self.assertEqual(ctx.exception.status_code, 502)

    def test_missing_required_config_navigation_redirects_with_items(self):
        """OIDC 必填项缺失(整页导航):302 回登录页 + errors.oidc_not_configured + 缺失清单."""
        with patch.object(settings, "oidc_issuer", ""):
            resp = oidc_router.start(self._nav(), db=self.db)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=errors.oidc_not_configured", resp.headers["location"])
        self.assertIn("missing=OIDC_ISSUER", resp.headers["location"])

    def test_missing_required_config_xhr_raises_503_with_key(self):
        """OIDC 必填项缺失(XHR / 脚本):503 + error_key + error_params(与导航同一份清单)."""
        with patch.object(settings, "oidc_client_secret", ""):
            with self.assertRaises(AppException) as ctx:
                oidc_router.start(_req(), db=self.db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.oidc_not_configured")
        self.assertEqual(ctx.exception.error_params["items"], "CD_OIDC_SECRET")

    def test_missing_required_config_blocks_callback_too(self):
        """必填项缺失时的回调(历史书签):同样按配置问题提示,不报成上游故障."""
        with patch.object(settings, "oidc_redirect_uri", ""):
            with self.assertRaises(AppException) as ctx:
                oidc_router.callback(request=_req(), db=self.db, code="c1", state="st-1")
        self.assertEqual(ctx.exception.error_key, "errors.oidc_not_configured")

    def test_missing_code_or_state_is_bad_request(self):
        resp = self._callback(state="st-1")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("code", resp.body.decode())

    def test_state_mismatch_is_bad_request(self):
        resp = self._callback(code="c1", state="other-state")
        self.assertEqual(resp.status_code, 400)

    def test_missing_state_cookie_is_bad_request(self):
        resp = oidc_router.callback(request=_req(), db=self.db, code="c1", state="st-1")
        self.assertEqual(resp.status_code, 400)

    def test_authorization_error_is_bad_request(self):
        resp = self._callback(error="access_denied")
        self.assertEqual(resp.status_code, 400)

    def test_success_redirects_home_with_session_cookie(self):
        with (
            patch.object(oidc_router.oidc, "exchange_code", return_value={"id_token": "t"}),
            patch.object(oidc_router.oidc, "verify_id_token", return_value="alice"),
            patch.object(oidc_router.glue_identity, "get_identity", return_value=_ident()),
            patch.object(oidc_router, "_create_session", return_value="tok-1"),
        ):
            resp = self._callback(code="c1", state="st-1")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/")
        self.assertIn("cd_token=tok-1", resp.headers["set-cookie"])
        self.assertIn("HttpOnly", resp.headers["set-cookie"])

    def test_invalid_grant_restarts_once_then_stops(self):
        err = oidc_router.oidc.OidcError("授权码无效或已过期（invalid_grant）", status_code=400, invalid_grant=True)
        with patch.object(oidc_router.oidc, "exchange_code", side_effect=err):
            first = self._callback(code="c1", state="st-1")
            second = self._callback(code="c1", state="st-1", retry="1")
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.headers["location"], "/api/auth/oidc/start?retry=1")
        self.assertEqual(second.status_code, 400)  # 已重试过:返回 400 而不是无限跳转

    def test_gate_failure_blocks_callback_before_credentials(self):
        self.db.cd_missing_tables.return_value = ["cd_sessions"]
        with self.assertRaises(AppException) as ctx:
            self._callback(code="c1", state="st-1")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_no_cd_access_redirects_to_login_with_key(self):
        with self._stub_token_flow(_ident(systems="ci,monitor")):
            resp = self._callback(code="c1", state="st-1")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/login?error=errors.no_cd_access")

    def test_disabled_account_redirects_to_login_with_key(self):
        with self._stub_token_flow(_ident(status=0)):
            resp = self._callback(code="c1", state="st-1")
        self.assertEqual(resp.headers["location"], "/login?error=errors.user_disabled")

    def test_identity_unavailable_redirects_to_login_with_key(self):
        boom = MagicMock(side_effect=AppException("角色悬空", status_code=401, error_key="errors.identity_unavailable"))
        with self._stub_token_flow(get_identity=boom):
            resp = self._callback(code="c1", state="st-1")
        self.assertEqual(resp.headers["location"], "/login?error=errors.identity_unavailable")

    def test_upstream_failure_is_502_and_clears_state_cookie(self):
        with patch.object(
            oidc_router.oidc, "exchange_code", side_effect=oidc_router.oidc.OidcError("token 端点不可达")
        ):
            resp = self._callback(code="c1", state="st-1")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("cd_oidc_state=", resp.headers["set-cookie"])  # 失败也要清一次性 cookie

    def test_identity_service_unavailable_is_502(self):
        boom = MagicMock(
            side_effect=AppException("Glue 不可达", status_code=503, error_key="errors.service_unavailable")
        )
        with self._stub_token_flow(get_identity=boom):
            resp = self._callback(code="c1", state="st-1")
        self.assertEqual(resp.status_code, 502)

    # ── 失败的"呈现方式"(§7.1):整页导航 → 302 回登录页弹提示;XHR/脚本 → JSON + 状态码 ──
    # 回归目标:登录按钮是整页跳转,过去 503/502/400 的 JSON 错误体会被浏览器当页面渲染成
    # "一屏裸文本"(切档期 errors.cd_not_enabled 就是这样被看到的).

    def _nav(self, cookies: dict | None = None):
        return _req(cookies if cookies is not None else {"cd_oidc_state": "st-1.no-1"}, accept=NAV_ACCEPT)

    def test_navigation_gate_failure_redirects_to_login_with_key(self):
        self.db.cd_integration_state.return_value.enabled = False  # 契约行 enabled=0:CD 未启用
        resp = oidc_router.start(self._nav(), db=self.db)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/login?error=errors.cd_not_enabled&service=cd")
        self.assertIn("cd_oidc_state=", resp.headers["set-cookie"])  # 失败也要清一次性 cookie

    def test_xhr_gate_failure_still_raises_503_with_key(self):
        self.db.cd_integration_state.return_value.enabled = False
        with self.assertRaises(AppException) as ctx:
            oidc_router.start(_req(), db=self.db)  # Accept: */* → 保持 HTTP 语义,交全局 handler 出 JSON
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")

    def test_navigation_missing_tables_redirects_to_login_with_key(self):
        self.db.cd_missing_tables.return_value = ["cd_sessions"]
        resp = oidc_router.start(self._nav(), db=self.db)
        self.assertEqual(resp.headers["location"], "/login?error=errors.cd_not_deployed&tables=cd_sessions")

    def test_navigation_discovery_failure_redirects_to_login(self):
        with patch.object(oidc_router.oidc, "authorize_url", side_effect=oidc_router.oidc.OidcError("issuer 不一致")):
            resp = oidc_router.start(self._nav(), db=self.db)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/login?error=errors.sso_unavailable")

    def test_navigation_authorization_denied_redirects_with_own_key(self):
        resp = oidc_router.callback(request=self._nav(), db=self.db, error="access_denied")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/login?error=errors.sso_denied")

    def test_navigation_state_mismatch_redirects_with_state_key(self):
        resp = oidc_router.callback(request=self._nav(), db=self.db, code="c1", state="other-state")
        self.assertEqual(resp.headers["location"], "/login?error=errors.sso_state_invalid")

    def test_navigation_missing_code_redirects_with_state_key(self):
        resp = oidc_router.callback(request=self._nav(), db=self.db, state="st-1")
        self.assertEqual(resp.headers["location"], "/login?error=errors.sso_state_invalid")

    def test_navigation_upstream_failure_redirects_with_unavailable_key(self):
        with patch.object(
            oidc_router.oidc, "exchange_code", side_effect=oidc_router.oidc.OidcError("token 端点不可达")
        ):
            resp = oidc_router.callback(request=self._nav(), db=self.db, code="c1", state="st-1")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/login?error=errors.sso_unavailable")

    def test_navigation_upstream_5xx_uses_glue_side_key(self):
        """Glue 侧 5xx 与「连不上 Glue」分流:登录页直接给出指向 Glue 日志的排查提示.

        回归背景:OIDC 签名私钥非 PEM 时 Glue 的 /oauth/token 返回 500,
        旧文案与「网络不可达」共用 errors.sso_unavailable,把服务端配置问题掩盖成网络问题.
        """
        with patch.object(
            oidc_router.oidc,
            "exchange_code",
            side_effect=oidc_router.oidc.OidcError(
                "token 端点返回 HTTP 500", error_key="errors.sso_upstream_error"
            ),
        ):
            resp = oidc_router.callback(request=self._nav(), db=self.db, code="c1", state="st-1")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/login?error=errors.sso_upstream_error")

    def test_navigation_gate_failure_on_callback_redirects_to_login(self):
        self.db.cd_missing_tables.return_value = ["cd_sessions"]
        resp = oidc_router.callback(request=self._nav(), db=self.db, code="c1", state="st-1")
        self.assertEqual(resp.headers["location"], "/login?error=errors.cd_not_deployed&tables=cd_sessions")

    def test_navigation_success_still_lands_on_home(self):
        """导航形态的成功路径不受分流影响:仍是 302 / + 会话 cookie."""
        with (
            patch.object(oidc_router.oidc, "exchange_code", return_value={"id_token": "t"}),
            patch.object(oidc_router.oidc, "verify_id_token", return_value="alice"),
            patch.object(oidc_router.glue_identity, "get_identity", return_value=_ident()),
            patch.object(oidc_router, "_create_session", return_value="tok-nav"),
        ):
            resp = oidc_router.callback(request=self._nav(), db=self.db, code="c1", state="st-1")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/")
        self.assertIn("cd_token=tok-nav", resp.headers["set-cookie"])


class TestAuthRequestPath(_OidcSettingsMixin, unittest.TestCase):
    """auth 请求路径(§7.2):只查 cd_sessions + Glue 身份;通配权限放行;停用/身份不可用即删会话行."""

    def _stub_identity(self, ident: dict | None = None, exc: Exception | None = None):
        if exc is not None:
            return patch.object(glue_identity, "get_identity", side_effect=exc)
        return patch.object(glue_identity, "get_identity", return_value=ident if ident else _ident())

    def test_verify_token_never_reads_shared_tables(self):
        """共享表(admin_users / roles / role_permissions)的读取入口已随本地登录模式删除."""
        db = _fake_db()
        with self._stub_identity():
            username = verify_token(_req({"cd_token": "tok-1"}), None, db=db)
        self.assertEqual(username, "alice")
        for name in ("_query_user_with_systems", "_query_permissions", "authenticate", "verify_password"):
            self.assertFalse(hasattr(auth_module, name), f"{name} 应随本地登录模式一并删除")

    def test_get_current_user_returns_glue_permissions(self):
        db = _fake_db()
        with self._stub_identity(_ident(role="ops", systems="cd,monitor", permissions=["cd.deploy.k8s"])):
            user = get_current_user(_req({"cd_token": "tok-1"}), None, db=db)
        # 响应结构与前端 useAuth / 路由守卫的期望一致(§6.2)
        self.assertEqual(
            user,
            {"username": "alice", "role": "ops", "systems": "cd,monitor", "permissions": ["cd.deploy.k8s"]},
        )

    def test_disabled_account_revokes_session_and_raises_401(self):
        db = _fake_db()
        conn = db.conn.return_value.__enter__.return_value
        with self._stub_identity(_ident(status=0)):
            with self.assertRaises(AppException) as ctx:
                verify_token(_req({"cd_token": "tok-1"}), None, db=db)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.error_key, "errors.user_disabled")
        self.assertTrue(any("DELETE FROM cd_sessions" in str(c.args[0]) for c in conn.execute.call_args_list))

    def test_no_cd_system_revokes_session_and_raises_401(self):
        db = _fake_db()
        conn = db.conn.return_value.__enter__.return_value
        with self._stub_identity(_ident(systems="ci")):
            with self.assertRaises(AppException) as ctx:
                get_current_user(_req({"cd_token": "tok-1"}), None, db=db)
        self.assertEqual(ctx.exception.error_key, "errors.no_cd_access")
        self.assertTrue(any("DELETE FROM cd_sessions" in str(c.args[0]) for c in conn.execute.call_args_list))

    def test_identity_unavailable_revokes_session(self):
        db = _fake_db()
        conn = db.conn.return_value.__enter__.return_value
        boom = AppException("角色悬空", status_code=401, error_key="errors.identity_unavailable")
        with self._stub_identity(exc=boom):
            with self.assertRaises(AppException):
                verify_token(_req({"cd_token": "tok-1"}), None, db=db)
        self.assertTrue(any("DELETE FROM cd_sessions" in str(c.args[0]) for c in conn.execute.call_args_list))

    def test_service_unavailable_keeps_session(self):
        # 503 = 暂时不可用:保留会话,Glue 恢复后 ≤1 个窗口自动可用(与 401 的踢下线严格区分)
        db = _fake_db()
        conn = db.conn.return_value.__enter__.return_value
        boom = AppException("Glue 不可达", status_code=503, error_key="errors.service_unavailable")
        with self._stub_identity(exc=boom):
            with self.assertRaises(AppException) as ctx:
                verify_token(_req({"cd_token": "tok-1"}), None, db=db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertFalse(any("DELETE FROM cd_sessions" in str(c.args[0]) for c in conn.execute.call_args_list))

    def test_expired_session_rejected_before_glue(self):
        db = _fake_db()
        db.conn.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {
            "username": "alice",
            "expires_at": time.time() - 1,
        }
        with self._stub_identity(exc=AssertionError("会话已过期不该再去查 Glue")):
            with self.assertRaises(HTTPException) as ctx:
                verify_token(_req({"cd_token": "tok-1"}), None, db=db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wildcard_permission_passes_require_perm(self):
        # Glue 对 super_admin 返回 ["*"]:角色改名(SUPER_ADMIN_ROLE 或 Glue 侧)后依然必须放行(§7.2)
        user = {"username": "root", "role": "renamed-role", "permissions": ["*"]}
        self.assertIs(require_perm("cd.deploy.k8s")(user), user)

    def test_require_perm_still_denies_without_permission(self):
        user = {"username": "dev", "role": "dev", "permissions": ["cd.deploy.single"]}
        with self.assertRaises(HTTPException) as ctx:
            require_perm("cd.deploy.k8s")(user)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_wildcard_permission_passes_enforce_deploy_perm(self):
        enforce_deploy_perm({"role": "renamed-role", "permissions": ["*"]}, "k8s", "helm")  # 不抛即通过

    def test_enforce_deploy_perm_denies_without_permission(self):
        with self.assertRaises(HTTPException) as ctx:
            enforce_deploy_perm({"role": "dev", "permissions": ["cd.deploy.single"]}, "k8s", "helm")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
