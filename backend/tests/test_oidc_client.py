"""OIDC 客户端与配置回归(L1a,见 docs/SSO_OIDC_ZH.md §6.3 / §8 / §11).

覆盖:
  · AUTH_MODE 白名单校验与 `oidc_ready()` / `oidc_missing_settings()` 自检(fail-closed 口径);
  · authorize URL 只信 discovery 的端点,参数齐全(response_type/client_id/redirect_uri/state/nonce);
  · token 兑换:必须回传 redirect_uri,invalid_grant 单独建模(供路由静默重走 start);
  · id_token:RS256 真签名验签(进程内生成 RSA 密钥对,不引 PyJWT,不打桩 cryptography)
    + iss/aud/exp/iat/nonce 全量校验,kid 轮换(未知 kid 强制刷新一次 JWKS).

不连网络:discovery / JWKS 与 requests 全部打桩.

运行(项目根):
    python -m pytest backend/tests/test_oidc_client.py -q
"""

import base64
import json
import os
import sys
import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend import oidc
from backend.config import Settings, settings

_ISSUER = "https://glue.example.com"
_REDIRECT = "https://cd.example.com/api/auth/oidc/callback"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "test-kid-1"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwk() -> dict:
    nums = _KEY.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": _KID,
        "alg": "RS256",
        "use": "sig",
        "n": _b64url(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")),
    }


def _id_token(claims: dict, kid: str = _KID, alg: str = "RS256", tamper: bool = False) -> str:
    """用本地私钥真签一个 RS256 id_token(tamper=True 时改动签名字节,用于验签失败用例)."""
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(claims).encode())
    sig = _KEY.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    if tamper:
        sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
    return f"{h}.{p}.{_b64url(sig)}"


def _claims(nonce: str = "n-1", **over: Any) -> dict:
    now = int(time.time())
    base: dict = {
        "iss": _ISSUER,
        "aud": "cd",
        "sub": "Alice",
        "preferred_username": "Alice",  # 大写:验证 username 会被 strip+lower(Glue 侧 rbac 会归一)
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
    }
    base.update(over)
    return base


class _OidcSettingsMixin:
    """settings 是全局单例:用例必须自己保存/恢复 OIDC 字段,避免相互污染."""

    FIELDS = (
        "auth_mode",
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_redirect_uri",
        "oidc_scopes",
        "oidc_revalidate_seconds",
        "oidc_stale_grace_seconds",
        "oidc_id_token_max_skew",
    )

    def setUp(self):
        super().setUp()
        self._saved = {f: getattr(settings, f) for f in self.FIELDS}
        settings.oidc_issuer = _ISSUER
        settings.oidc_client_secret = "s3cr3t"
        settings.oidc_redirect_uri = _REDIRECT
        oidc.reset_caches()

    def tearDown(self):
        for f, v in self._saved.items():
            setattr(settings, f, v)
        oidc.reset_caches()
        super().tearDown()


def _discovery_doc(**over: Any) -> dict:
    doc = {
        "issuer": _ISSUER,
        "authorization_endpoint": f"{_ISSUER}/oauth/authorize",
        "token_endpoint": f"{_ISSUER}/oauth/token",
        "jwks_uri": f"{_ISSUER}/.well-known/jwks.json",
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    doc.update(over)
    return doc


class TestAuthModeConfig(unittest.TestCase):
    """AUTH_MODE 白名单 + oidc 模式必填项自检(§6.3:半配的 OIDC 一律拒绝启动)."""

    def test_validator_whitelist_and_normalisation(self):
        from pydantic import ValidationError

        self.assertEqual(Settings(auth_mode=" OIDC ").auth_mode, "oidc")
        self.assertEqual(Settings(auth_mode="local").auth_mode, "local")
        # 留空按未配置处理,落回默认档 oidc(compose 透传 ${AUTH_MODE:-oidc} 与根 .env 写空同此路径)
        self.assertEqual(Settings(auth_mode="").auth_mode, "oidc")
        self.assertEqual(Settings(auth_mode="   ").auth_mode, "oidc")
        # 拼错的模式名必须报错,不静默退化成 local(否则运维以为切到了 SSO)
        with self.assertRaises(ValidationError):
            Settings(auth_mode="basic")

    def test_code_default_is_oidc(self):
        """字段默认值必须是 oidc:环境变量与两份 env 文件全缺失时也落到 SSO 档."""
        self.assertEqual(Settings.model_fields["auth_mode"].default, "oidc")

    def test_issuer_trailing_slash_stripped(self):
        # issuer 与 discovery 精确比对,末尾多一个 / 就会误判不一致
        self.assertEqual(Settings(oidc_issuer=f"{_ISSUER}/").oidc_issuer, _ISSUER)

    def test_oidc_ready_false_and_missing_list(self):
        s = Settings(**{"auth_mode": "oidc", "oidc_issuer": "", "CD_OIDC_SECRET": "", "CD_OIDC_REDIRECT_URI": ""})
        self.assertFalse(s.oidc_ready())
        self.assertEqual(s.oidc_missing_settings(), ["OIDC_ISSUER", "CD_OIDC_SECRET", "CD_OIDC_REDIRECT_URI"])

    def test_oidc_ready_true_when_configured(self):
        s = Settings(
            **{"auth_mode": "oidc", "oidc_issuer": _ISSUER, "CD_OIDC_SECRET": "s", "CD_OIDC_REDIRECT_URI": _REDIRECT}
        )
        self.assertTrue(s.oidc_ready())
        self.assertEqual(s.oidc_missing_settings(), [])

    def test_local_mode_is_ready_regardless(self):
        s = Settings(**{"auth_mode": "local", "oidc_issuer": "", "CD_OIDC_SECRET": "", "CD_OIDC_REDIRECT_URI": ""})
        self.assertTrue(s.oidc_ready())  # local 模式下这些项与它无关


class TestIdTokenVerification(_OidcSettingsMixin, unittest.TestCase):
    """id_token 校验:RS256 真签名 + iss/aud/exp/iat/nonce 全量必验(§8.2/§8.5)."""

    def _verify(self, token: str, nonce: str = "n-1", keys: list | None = None) -> str:
        jwks_doc = {"keys": [_jwk()] if keys is None else keys}
        with patch.object(oidc, "jwks", return_value=jwks_doc):
            return oidc.verify_id_token(token, nonce)

    def test_valid_token_returns_normalised_username(self):
        # preferred_username="Alice" → 返回 "alice"(必须 strip+lower,否则 Glue rbac 端点 404)
        self.assertEqual(self._verify(_id_token(_claims())), "alice")

    def test_username_falls_back_to_sub(self):
        self.assertEqual(self._verify(_id_token(_claims(preferred_username=""))), "alice")

    def test_tampered_signature_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify(_id_token(_claims(), tamper=True))
        self.assertIn("验签", ctx.exception.message)
        self.assertEqual(ctx.exception.status_code, 502)

    def test_alg_other_than_rs256_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify(_id_token(_claims(), alg="HS256"))
        self.assertIn("RS256", ctx.exception.message)

    def test_unknown_kid_triggers_one_forced_refresh(self):
        # 签名密钥轮换:首次查不到 → 强制刷新一次 JWKS(仍查不到才失败)
        with patch.object(oidc, "jwks", return_value={"keys": [_jwk()]}) as jwks:
            with self.assertRaises(oidc.OidcError):
                oidc.verify_id_token(_id_token(_claims(), kid="rotated-kid"), "n-1")
        self.assertEqual(jwks.call_count, 2)

    def test_iss_mismatch_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify(_id_token(_claims(iss="https://evil.example.com")))
        self.assertIn("iss 不匹配", ctx.exception.message)

    def test_iss_trailing_slash_tolerated(self):
        self.assertEqual(self._verify(_id_token(_claims(iss=f"{_ISSUER}/"))), "alice")

    def test_aud_mismatch_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify(_id_token(_claims(aud="other-client")))
        self.assertIn("aud 不匹配", ctx.exception.message)

    def test_aud_list_containing_client_id_accepted(self):
        self.assertEqual(self._verify(_id_token(_claims(aud=["other", "cd"]))), "alice")

    def test_expired_token_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify(_id_token(_claims(exp=int(time.time()) - 3600)))
        self.assertIn("已过期", ctx.exception.message)

    def test_exp_within_clock_skew_accepted(self):
        self.assertEqual(self._verify(_id_token(_claims(exp=int(time.time()) - 30))), "alice")

    def test_future_iat_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify(_id_token(_claims(iat=int(time.time()) + 3600)))
        self.assertIn("iat 在未来", ctx.exception.message)

    def test_nonce_mismatch_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify(_id_token(_claims(nonce="other-nonce")), nonce="n-1")
        self.assertIn("nonce", ctx.exception.message)

    def test_missing_nonce_rejected(self):
        with self.assertRaises(oidc.OidcError):
            self._verify(_id_token(_claims()), nonce="")

    def test_no_username_claim_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify(_id_token(_claims(preferred_username="", sub="")))
        self.assertIn("用户名", ctx.exception.message)

    def test_malformed_token_rejected(self):
        with self.assertRaises(oidc.OidcError) as ctx:
            self._verify("not-a-jwt")
        self.assertIn("结构非法", ctx.exception.message)

    def test_b64url_decode_without_padding(self):
        self.assertEqual(oidc._b64url_decode("YWJj"), b"abc")


class TestAuthorizeAndTokenExchange(_OidcSettingsMixin, unittest.TestCase):
    """authorize URL 组装 + token 兑换的三个硬约束(§8.1 / §8.7 第 1,2 条)."""

    def test_authorize_url_uses_discovery_endpoint_and_all_params(self):
        with patch.object(oidc, "discovery", return_value=_discovery_doc()):
            url = oidc.authorize_url("st-1", "no-1")
        # 端点只信 discovery(不硬编码 /oauth/authorize 路径)
        self.assertTrue(url.startswith(f"{_ISSUER}/oauth/authorize?"))
        q = dict(parse_qsl(url.split("?", 1)[1]))
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["client_id"], "cd")
        self.assertEqual(q["redirect_uri"], _REDIRECT)
        self.assertEqual(q["state"], "st-1")
        self.assertEqual(q["nonce"], "no-1")
        self.assertIn("openid", q["scope"])

    def test_discovery_rejects_issuer_mismatch(self):
        # discovery 的 issuer 与 OIDC_ISSUER 不一致即拒绝(日志同时打印两侧取值,用例 F 最省时间的那一步)
        resp = MagicMock(status_code=200)
        resp.json.return_value = _discovery_doc(issuer="http://glue-internal:8080")
        with patch.object(oidc, "_session") as sess:
            sess.get.return_value = resp
            with self.assertRaises(oidc.OidcError) as ctx:
                oidc._fetch_discovery()
        self.assertIn("issuer 不一致", ctx.exception.message)

    def test_discovery_requires_core_endpoints(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"issuer": _ISSUER}
        with patch.object(oidc, "_session") as sess:
            sess.get.return_value = resp
            with self.assertRaises(oidc.OidcError) as ctx:
                oidc._fetch_discovery()
        self.assertIn("authorization_endpoint", ctx.exception.message)

    def test_exchange_code_sends_redirect_uri_and_basic_auth(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id_token": "x.y.z", "access_token": "at"}
        with (
            patch.object(oidc, "discovery", return_value=_discovery_doc()),
            patch.object(oidc.requests, "post", return_value=resp) as post,
        ):
            payload = oidc.exchange_code("code-1")
        self.assertEqual(payload["id_token"], "x.y.z")
        _, kwargs = post.call_args
        # Glue 用 consumeCode($code,$clientId,$redirectUri) 精确比对:漏传即 invalid_grant
        self.assertEqual(kwargs["data"]["redirect_uri"], _REDIRECT)
        self.assertEqual(kwargs["data"]["grant_type"], "authorization_code")
        self.assertNotIn("code_verifier", kwargs["data"])  # 机密客户端,非 PKCE 流
        self.assertEqual(kwargs["auth"], ("cd", "s3cr3t"))  # HTTP Basic 客户端认证
        self.assertNotIn("params", kwargs)  # 不带 ?format=json(会把响应包成 {"data":...})

    def test_exchange_code_invalid_grant_is_modelled(self):
        resp = MagicMock(status_code=400)
        resp.json.return_value = {"error": "invalid_grant", "message": "已翻译的文案"}
        with (
            patch.object(oidc, "discovery", return_value=_discovery_doc()),
            patch.object(oidc.requests, "post", return_value=resp),
        ):
            with self.assertRaises(oidc.OidcError) as ctx:
                oidc.exchange_code("used-code")
        self.assertTrue(ctx.exception.invalid_grant)  # 路由据此静默重走一次 start
        self.assertEqual(ctx.exception.status_code, 400)

    def test_exchange_code_401_hints_secret_config(self):
        resp = MagicMock(status_code=401)
        resp.json.return_value = {"error": "invalid_client", "message": "客户端认证失败"}
        with (
            patch.object(oidc, "discovery", return_value=_discovery_doc()),
            patch.object(oidc.requests, "post", return_value=resp),
        ):
            with self.assertRaises(oidc.OidcError) as ctx:
                oidc.exchange_code("code-1")
        self.assertFalse(ctx.exception.invalid_grant)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("CD_OIDC_SECRET", ctx.exception.message)

    def test_exchange_code_without_id_token_rejected(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"access_token": "at"}  # scope 漏 openid 时的表现(Glue 不报错)
        with (
            patch.object(oidc, "discovery", return_value=_discovery_doc()),
            patch.object(oidc.requests, "post", return_value=resp),
        ):
            with self.assertRaises(oidc.OidcError) as ctx:
                oidc.exchange_code("code-1")
        self.assertIn("openid", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
