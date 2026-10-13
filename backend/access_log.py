"""访问日志脱敏 -- SSO 回跳 URL 里的 code / state 不进日志.

uvicorn 的 access log 会打印完整请求行,其中三类 URL 自带凭据:
  · `/api/auth/oidc/callback?code=...&state=...`:code 是 Glue 发的一次性授权码(60s 失效,只能兑换一次),
    state 是本次登录的 CSRF 凭据;
  · `/api/auth/oidc/start?state=...`(签发时)以及 `?retry=1` 的静默重试;
  · `/ws/terminal/{id}?token=...`:旧客户端的回退写法里 token 是会话凭据(现在浏览器走 cookie).

"凭据类参数不落盘"是安全不变量(§10),但访问日志本身是排障刚需,所以这里不做整条丢弃,
只把**敏感参数的值**替换成 `<masked>`:路径,其余 query,状态码,耗时全部照常记录.

挂载方式:`install_access_log_filter()` 在 `main.py` 导入期调用一次即生效,
`uvicorn main:app` / `python main.py` / `--workers N` 三种启动方式都覆盖
(uvicorn 自身调用 dictConfig 时会重建 handler/level,但**不会清空 logger 上已注册的 filters**).
"""

import logging

MASK = "<masked>"

# 需要脱敏的参数名(小写比较):OIDC 与 token 两类
SENSITIVE_KEYS = frozenset(
    {"code", "state", "nonce", "token", "id_token", "access_token", "refresh_token", "code_verifier", "session"}
)

# 只对这几类路径做参数级脱敏;其余路由原样输出(避免误伤业务参数)
MASK_PATH_PREFIXES = ("/api/auth/oidc/", "/ws/terminal")


def mask_query(path: str) -> str:
    """把敏感参数的值换成 <masked>,其余部分逐字符保留(便于排障时对比 URL 形状)."""
    base, sep, query = path.partition("?")
    if not sep or not base.startswith(MASK_PATH_PREFIXES):
        return path  # 非目标路由 / 无 query:原样返回(调用方据此跳过改写)
    kept = []
    for chunk in query.split("&"):
        key, key_sep, _value = chunk.partition("=")
        kept.append(f"{key}={MASK}" if key_sep and key.strip().lower() in SENSITIVE_KEYS else chunk)
    return f"{base}?{'&'.join(kept)}"


class MaskCredentialQueryFilter(logging.Filter):
    """uvicorn 访问日志过滤器:兼容 http(tuple args)与 websocket(dict args)两种记录形态.

    只在**确实有敏感参数**时才改写 record(访问日志每请求一条,普通路由走零分配快路径).
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging.Filter 约定名
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            # uvicorn.access: (client_addr, method, full_path, http_version, status_code)
            masked = mask_query(args[2])
            if masked != args[2]:
                record.args = (*args[:2], masked, *args[3:])
        elif isinstance(args, dict):
            # uvicorn 的 websocket 访问日志用 dict 参数(键:client_addr / path / status_code ...)
            path = args.get("path")
            if isinstance(path, str):
                masked = mask_query(path)
                if masked != path:
                    record.args = {**args, "path": masked}  # 复制而非就地改写:不影响其他 handler
        return True  # 只改写,不过滤任何记录


def install_access_log_filter(logger_name: str = "uvicorn.access") -> None:
    """幂等安装(模块被重复导入 / 测试反复调用都不会叠加多个 filter)."""
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, MaskCredentialQueryFilter) for f in logger.filters):
        logger.addFilter(MaskCredentialQueryFilter())
