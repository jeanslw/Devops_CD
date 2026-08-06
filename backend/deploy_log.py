"""部署日志 i18n 工具 — 通过 SSE 向后端推送 STATUS: 前缀的结构化消息，前端 translate。

用法:
    from backend.deploy_log import S
    _log(callback, S("deploy_log.verifying_path"))
    _log(callback, S("deploy_log.path_ok", path="/opt/app"))
    _log(callback, S("deploy_log.deploy_error", error="部署执行失败"))

设计约定:
  - 只用于 SSE 流式输出（callback），不用于写入数据库的 result.output。
  - 数据库 output 统一存英文（语言无关），SSE 实时推送通过前端 i18n 翻译为当前界面语言。
"""

import json


def S(key: str, **kwargs) -> str:
    """构建 STATUS: 前缀的 i18n 消息，供前端解析翻译。"""
    payload = {"key": key}
    if kwargs:
        payload.update(kwargs)
    return f"STATUS:{json.dumps(payload, ensure_ascii=False)}"
