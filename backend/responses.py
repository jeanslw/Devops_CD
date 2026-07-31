"""统一 API 响应格式 — 所有路由返回一致的结构"""

from typing import Any


def ok(data: Any = None, message: str = "") -> dict:
    """成功响应"""
    result = {"success": True}
    if data is not None:
        result["data"] = data
    if message:
        result["message"] = message
    return result


def items(items: list, total: int, page: int = 1, page_size: int = 20) -> dict:
    """分页列表响应"""
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "success": True,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def error(message: str, code: int = 400) -> dict:
    """统一错误响应（预留，当前项目统一使用 AppException 异常体系）"""
    return {"success": False, "error": message, "code": code}
