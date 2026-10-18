"""访问日志脱敏:指定路径的 code / state / token 等参数值替换为 <masked>。

记录本身保留,路径、其余 query、状态码、耗时照常输出。
"""

import logging

MASK = "<masked>"

# 需要脱敏的参数名(小写比较):OIDC 与 token 两类
SENSITIVE_KEYS = frozenset(
    {"code", "state", "nonce", "token", "id_token", "access_token", "refresh_token", "code_verifier", "session"}
)

# 只对这几类路径做参数级脱敏;其余路由原样输出
MASK_PATH_PREFIXES = ("/api/auth/oidc/", "/ws/terminal")


def mask_query(path: str) -> str:
    """把敏感参数的值换成 <masked>,其余部分逐字符保留."""
    base, sep, query = path.partition("?")
    if not sep or not base.startswith(MASK_PATH_PREFIXES):
        return path  # 非目标路由 / 无 query:原样返回
    kept = []
    for chunk in query.split("&"):
        key, key_sep, _value = chunk.partition("=")
        kept.append(f"{key}={MASK}" if key_sep and key.strip().lower() in SENSITIVE_KEYS else chunk)
    return f"{base}?{'&'.join(kept)}"


class MaskCredentialQueryFilter(logging.Filter):
    """uvicorn 访问日志过滤器:兼容 http(tuple args)与 websocket(dict args)两种记录形态。

    仅在命中敏感参数时改写 record。
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
    """幂等安装,不叠加多个 filter."""
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, MaskCredentialQueryFilter) for f in logger.filters):
        logger.addFilter(MaskCredentialQueryFilter())


class SuppressTracebackFilter(logging.Filter):
    """清掉 exc_info,把异常消息(type: message)拼到 record.msg 末尾,去掉 Traceback 帧."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging.Filter 约定名
        if record.exc_info:
            exc = record.exc_info[1]
            if exc is not None:
                try:
                    msg = record.getMessage()
                except Exception:
                    msg = ""
                record.msg = f"{msg}: {type(exc).__name__}: {exc}" if msg else f"{type(exc).__name__}: {exc}"
                record.args = None
            record.exc_info = None
            record.exc_text = None
        return True


def install_console_log_filter() -> None:
    """幂等安装:挂到根 / uvicorn / uvicorn.error logger 与 lastResort handler."""
    for name in ("", "uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, SuppressTracebackFilter) for f in logger.filters):
            logger.addFilter(SuppressTracebackFilter())
    last_resort = logging.lastResort
    if last_resort is not None and not any(isinstance(f, SuppressTracebackFilter) for f in last_resort.filters):
        last_resort.addFilter(SuppressTracebackFilter())
