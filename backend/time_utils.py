"""时区工具:全仓库统一以 UTC 为准(对齐 Devops_Glue 的 UTC 模型).

约定:
- 存储层(MySQL / SQLite / DATETIME 列)一律存 UTC 墙钟字符串 'YYYY-MM-DD HH:MM:SS';
- MySQL 连接会话时区由 backend/database.py 强制为 '+00:00',使 DEFAULT CURRENT_TIMESTAMP 也落 UTC;
- 前端按 config/app.env 的 DISPLAY_TIMEZONE(默认注释 = 浏览器本地时间)展示并换算定时输入,服务端不做时区转换.
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间(时区感知),用于需要 tz-aware 对象的场景."""
    return datetime.now(timezone.utc)


def utc_now_str() -> str:
    """返回 UTC 墙钟字符串 'YYYY-MM-DD HH:MM:SS'(无时区标注,约定为 UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
