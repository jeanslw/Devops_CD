"""OIDC 客户端(BFF 机密客户端):discovery,JWKS,authorize URL 组装,token 兑换 + id_token 验签.

id_token 只用于确定 username;role / systems / status 以 Glue 的 /api/rbac 端点为准.
"""

import base64
import json
import logging
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend.config import settings
from backend.exceptions import AppException

logger = logging.getLogger("oidc")

_DISCOVERY_TTL = 3600  # discovery 字段是静态的(端点地址),不必频繁拉取
_JWKS_TTL = 3600  # JWKS 同理;未知 kid 会强制刷新一次(签名密钥轮换)
_HTTP_TIMEOUT = 10


class OidcError(AppException):
    """OIDC 流程异常.

    status_code 映射为回调路由的响应码(502 = Glue 侧不可用或协议不符).
    invalid_grant=True 专指 token 端点的 invalid_grant,回调路由据此静默重走一次 start.
    """

    def __init__(self, message: str, status_code: int = 502, error_key: str | None = None, invalid_grant: bool = False):
        super().__init__(message, status_code, None, error_key)
        self.invalid_grant = invalid_grant


class _TtlCache:
    """进程内 TTL 缓存(单进程 uvicorn;时钟与缓存不写入数据库)."""

    def __init__(self, name: str):
        self._name = name
        self._lock = threading.Lock()
        self._value: Any = None
        self._expires = 0.0

    def get(self) -> Any:
        with self._lock:
            if self._value is not None and time.time() < self._expires:
                return self._value
        return None

    def set(self, value: Any, ttl: int) -> None:
        with self._lock:
            self._value = value
            self._expires = time.time() + ttl

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._expires = 0.0


_discovery_cache = _TtlCache("discovery")
_jwks_cache = _TtlCache("jwks")

# discovery / JWKS 是幂等 GET,可以带重试.
_session = requests.Session()
_retry = requests.adapters.Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
_session.mount("http://", requests.adapters.HTTPAdapter(max_retries=_retry))
_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=_retry))


def _b64url_decode(data: str) -> bytes:
    """base64url → bytes(JWT/JWKS 不带 padding,需自行补齐)."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def reset_caches() -> None:
    """清空 discovery / JWKS 缓存(测试与排查用;生产无需调用)."""
    _discovery_cache.clear()
    _jwks_cache.clear()


# ── discovery ──


def _fetch_discovery() -> dict:
    """拉取并校验 /.well-known/openid-configuration(结果缓存 1 小时).

    discovery 的 issuer 与配置的 OIDC_ISSUER 不一致即拒绝,日志同时打印两侧取值.
    """
    url = f"{settings.oidc_issuer}/.well-known/openid-configuration"
    try:
        resp = _session.get(url, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise OidcError(f"无法访问 OIDC discovery：{exc}") from exc
    if resp.status_code != 200:
        raise OidcError(f"OIDC discovery 返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise OidcError("OIDC discovery 响应不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise OidcError("OIDC discovery 响应结构异常")

    issuer = str(data.get("issuer") or "").rstrip("/")
    if issuer != settings.oidc_issuer:
        logger.error(
            "OIDC issuer 不一致：discovery=%r，OIDC_ISSUER=%r（请确认配置的是 Glue 对外地址）",
            issuer,
            settings.oidc_issuer,
        )
        raise OidcError(
            f"OIDC issuer 不一致：discovery={issuer!r}，配置={settings.oidc_issuer!r}"
            "（OIDC_ISSUER 必须与 discovery 的 issuer 完全一致）"
        )

    missing = [k for k in ("authorization_endpoint", "token_endpoint", "jwks_uri") if not data.get(k)]
    if missing:
        raise OidcError(f"OIDC discovery 缺少字段：{', '.join(missing)}")
    algs = data.get("id_token_signing_alg_values_supported")
    if isinstance(algs, list) and algs and "RS256" not in algs:
        raise OidcError(f"OIDC 提供方未声明 RS256 签名算法：{algs}")
    return data


def discovery() -> dict:
    """取 discovery(带缓存)."""
    cached = _discovery_cache.get()
    if cached is not None:
        return cached
    data = _fetch_discovery()
    _discovery_cache.set(data, _DISCOVERY_TTL)
    return data


# ── JWKS ──


def _fetch_jwks() -> dict:
    """拉取 /.well-known/jwks.json(结果缓存 1 小时)."""
    url = discovery()["jwks_uri"]
    try:
        resp = _session.get(url, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise OidcError(f"无法访问 OIDC JWKS：{exc}") from exc
    if resp.status_code != 200:
        raise OidcError(f"OIDC JWKS 返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise OidcError("OIDC JWKS 响应不是合法 JSON") from exc
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list) or not keys:
        raise OidcError("OIDC JWKS 中没有可用公钥")
    return {"keys": keys}


def jwks(force_refresh: bool = False) -> dict:
    """取 JWKS(带缓存);force_refresh=True 时强制重新拉取(签名密钥轮换场景)."""
    if not force_refresh:
        cached = _jwks_cache.get()
        if cached is not None:
            return cached
    data = _fetch_jwks()
    _jwks_cache.set(data, _JWKS_TTL)
    return data


def _find_key(kid: str) -> dict | None:
    """按 kid 找 RSA 签名公钥;未知 kid 强制刷新 JWKS 一次再查."""
    for key in jwks().get("keys", []):
        if key.get("kid") == kid and key.get("kty") == "RSA":
            return key
    for key in jwks(force_refresh=True).get("keys", []):
        if key.get("kid") == kid and key.get("kty") == "RSA":
            return key
    return None


# ── authorize URL ──


def authorize_url(state: str, nonce: str) -> str:
    """组装 /oauth/authorize 跳转地址(端点取自 discovery,不硬编码路径)."""
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
    }
    return f"{discovery()['authorization_endpoint']}?{urlencode(params)}"


# ── token 兑换 ──


def exchange_code(code: str) -> dict:
    """用授权码兑换 token(HTTP Basic 客户端认证).

    三个硬约束:
    1. 必须回传 redirect_uri(Glue 用 consumeCode($code,$clientId,$redirectUri) 精确比对,
       漏传或不一致都表现为 invalid_grant);
    2. 不带 ?format=json;
    3. 不回传 code_verifier(Glue 侧是机密客户端,非 PKCE 流).
    """
    endpoint = discovery()["token_endpoint"]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
    }
    try:
        resp = _session.post(
            endpoint,
            data=body,
            auth=(settings.oidc_client_id, settings.oidc_client_secret),
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise OidcError(f"token 端点不可达：{exc}") from exc

    if resp.status_code == 200:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OidcError("token 端点响应不是合法 JSON") from exc
        if not isinstance(payload, dict) or not payload.get("id_token"):
            # scope 不含 openid 时 Glue 不报错,只是不下发 id_token(§8.7 第 3 条)
            raise OidcError("token 端点未返回 id_token（请确认 OIDC_SCOPES 含 openid）")
        return payload

    # 只按 HTTP 状态码判错,不解析 message(§8.7 第 2 条:message 是已翻译文本,不是稳定 key)
    err_code = ""
    try:
        err_code = str((resp.json() or {}).get("error") or "")
    except ValueError:
        err_code = ""
    if err_code == "invalid_grant":
        # 授权码已用/已超时(Glue 60s)或 redirect_uri 不匹配 即 由路由静默重走 start
        raise OidcError("授权码无效或已过期（invalid_grant）", status_code=400, invalid_grant=True)
    if resp.status_code in (400, 401):
        raise OidcError(f"token 端点拒绝兑换（HTTP {resp.status_code}）：请核对 CD_OIDC_SECRET 与 CD_OIDC_REDIRECT_URI")
    raise OidcError(
        f"token 端点返回 HTTP {resp.status_code}（Devops-Glue 服务端错误，请查看 Glue 容器日志中 /oauth/token 的 ERROR 记录）",
        error_key="errors.sso_upstream_error",
    )


# ── id_token 校验 ──


def _verify_signature(header: dict, signature: bytes, signing_input: bytes) -> None:
    """RS256 验签(cryptography 手写,不引 PyJWT)."""
    if header.get("alg") != "RS256":
        raise OidcError(f"id_token 签名算法不受支持：{header.get('alg')!r}（只接受 RS256）")
    kid = header.get("kid")
    key = _find_key(kid) if kid else None
    if key is None:
        raise OidcError(f"id_token 的 kid={kid!r} 不在 JWKS 中（已刷新一次）")
    try:
        n = int.from_bytes(_b64url_decode(key["n"]), "big")
        e = int.from_bytes(_b64url_decode(key["e"]), "big")
        public_key = rsa.RSAPublicNumbers(e, n).public_key()
    except (KeyError, ValueError, TypeError) as exc:
        raise OidcError("JWKS 公钥结构异常（缺 n/e 或 base64url 非法）") from exc
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise OidcError("id_token 验签失败（签名不匹配）") from exc


def _verify_claims(claims: dict, nonce: str) -> str:
    """校验 iss / aud / exp / iat / nonce,返回 username."""
    iss = str(claims.get("iss") or "").rstrip("/")
    if iss != settings.oidc_issuer:
        raise OidcError(f"id_token iss 不匹配：{iss!r} != {settings.oidc_issuer!r}")

    aud = claims.get("aud")
    auds = [aud] if isinstance(aud, str) else list(aud or [])
    if settings.oidc_client_id not in auds:
        raise OidcError(f"id_token aud 不匹配：{aud!r} 不含 {settings.oidc_client_id!r}")

    now = time.time()
    skew = settings.oidc_id_token_max_skew
    try:
        exp = float(claims["exp"])
        iat = float(claims.get("iat", now))
    except (KeyError, TypeError, ValueError) as exc:
        raise OidcError("id_token 缺少合法的 exp/iat") from exc
    if now - skew > exp:
        raise OidcError("id_token 已过期（exp 早于当前时间，超出时钟偏移容忍）")
    if iat - skew > now:
        raise OidcError("id_token 的 iat 在未来（双方时钟偏差超过容忍值）")

    # nonce 必须校验(§8.2):防重放,与授权请求里生成的值逐字符比对
    if not nonce or str(claims.get("nonce") or "") != nonce:
        raise OidcError("id_token nonce 不匹配（可能为重放请求）")

    # username 取 preferred_username,回退 sub;统一 strip + lower 归一
    username = str(claims.get("preferred_username") or claims.get("sub") or "").strip().lower()
    if not username:
        raise OidcError("id_token 中没有可用的用户名（preferred_username / sub 均为空）")
    return username


def verify_id_token(id_token: str, nonce: str) -> str:
    """验签 + 全量校验 id_token,返回 username.

    任一失败都抛 OidcError(fail-closed):签名 / iss / aud / exp / iat / nonce 全部必验.
    """
    parts = (id_token or "").split(".")
    if len(parts) != 3:
        raise OidcError("id_token 结构非法（应为 header.payload.signature）")
    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(signature_b64)
    except (ValueError, TypeError) as exc:
        raise OidcError("id_token 无法解码（base64url / JSON 非法）") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OidcError("id_token 结构异常（header / payload 不是对象）")

    signing_input = f"{header_b64}.{payload_b64}".encode()
    _verify_signature(header, signature, signing_input)
    return _verify_claims(claims, nonce)
