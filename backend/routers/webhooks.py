"""Webhook 接收路由 — 接收 CI 构建完成等外部事件，可选转发到 Bot"""

import json
import secrets
from datetime import datetime

import pymysql
from fastapi import APIRouter, Depends, Request

from backend.auth import get_db, require_perm, verify_token
from backend.database import Database
from backend.exceptions import ConflictError, DatabaseError, NotFoundError
from backend.models import WebhookForwardRequest, WebhookRequest
from backend.responses import ok
from backend.services.notification import send_webhook

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _generate_token() -> str:
    """生成 32 字符随机 token"""
    return secrets.token_urlsafe(24)


# ── Webhook 配置管理 ──

@router.get("")
def list_webhooks(
    db: Database = Depends(get_db),
    _username: str = Depends(verify_token),
):
    """列出所有 Webhook 配置"""
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT id, name, token, bot_id, enabled, created_at "
            "FROM cd_webhooks ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


@router.post("")
def create_webhook(
    req: WebhookRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    """创建 Webhook，自动生成 token"""
    token = _generate_token()
    with db.conn() as conn:
        try:
            conn.execute(
                "INSERT INTO cd_webhooks (name, token, bot_id, enabled) VALUES (?,?,?,?)",
                (req.name, token, req.bot_id, 1),
            )
            return ok(data={"token": token}, message=f"Webhook '{req.name}' 已创建")
        except pymysql.err.IntegrityError as e:
            raise ConflictError(
                f"Webhook '{req.name}' 已存在",
                error_key="errors.webhook_already_exists",
                error_params={"name": req.name},
            ) from e
        except Exception as e:
            raise DatabaseError(
                f"创建 Webhook 失败: {e}",
                error_key="errors.webhook_create_failed",
            ) from e


@router.delete("/{wid}")
def delete_webhook(
    wid: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    """删除 Webhook 及其所有事件记录"""
    with db.conn() as conn:
        conn.execute("DELETE FROM cd_webhook_events WHERE webhook_id=?", (wid,))
        conn.execute("DELETE FROM cd_webhooks WHERE id=?", (wid,))
        return ok(message="Webhook 已删除")


@router.patch("/{wid}")
def update_webhook(
    wid: int,
    req: WebhookRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    """更新 Webhook 配置（名称、关联 Bot）"""
    with db.conn() as conn:
        try:
            result = conn.execute(
                "UPDATE cd_webhooks SET name=?, bot_id=? WHERE id=?",
                (req.name, req.bot_id, wid),
            )
            if result.rowcount == 0:
                raise NotFoundError("Webhook 不存在", error_key="errors.webhook_not_found")
            return ok(message="Webhook 已更新")
        except pymysql.err.IntegrityError as e:
            raise ConflictError(
                f"Webhook '{req.name}' 已存在",
                error_key="errors.webhook_already_exists",
                error_params={"name": req.name},
            ) from e


@router.post("/{wid}/toggle")
def toggle_webhook(
    wid: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    """启用/禁用 Webhook"""
    with db.conn() as conn:
        row = conn.execute(
            "SELECT enabled FROM cd_webhooks WHERE id=?", (wid,)
        ).fetchone()
        if not row:
            raise NotFoundError("Webhook 不存在", error_key="errors.webhook_not_found")
        new_val = 0 if row["enabled"] else 1
        conn.execute("UPDATE cd_webhooks SET enabled=? WHERE id=?", (new_val, wid))
        return ok(data={"enabled": new_val})


# ── 事件查看 ──

@router.get("/{wid}/events")
def list_events(
    wid: int,
    page: int = 1,
    page_size: int = 20,
    db: Database = Depends(get_db),
    _username: str = Depends(verify_token),
):
    """查看某 Webhook 收到的事件列表（分页）"""
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size
    with db.conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM cd_webhook_events WHERE webhook_id=?", (wid,)
        ).fetchone()["cnt"]
        rows = conn.execute(
            "SELECT id, webhook_id, payload, received_at, forwarded, forwarded_at "
            "FROM cd_webhook_events WHERE webhook_id=? "
            "ORDER BY received_at DESC LIMIT ? OFFSET ?",
            (wid, page_size, offset),
        ).fetchall()
        return {
            "success": True,
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }


@router.delete("/events/{eid}")
def delete_event(
    eid: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    """删除单条事件记录"""
    with db.conn() as conn:
        conn.execute("DELETE FROM cd_webhook_events WHERE id=?", (eid,))
        return ok(message="事件已删除")


# ── 手动转发 ──

@router.post("/events/{eid}/forward")
def forward_event(
    eid: int,
    req: WebhookForwardRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(verify_token),
):
    """手动转发某条事件到指定 Bot"""
    with db.conn() as conn:
        event = conn.execute(
            "SELECT * FROM cd_webhook_events WHERE id=?", (eid,)
        ).fetchone()
        if not event:
            raise NotFoundError("事件不存在", error_key="errors.webhook_event_not_found")

        bot = conn.execute(
            "SELECT * FROM cd_bots WHERE id=?", (req.bot_id,)
        ).fetchone()
        if not bot:
            raise NotFoundError("Bot 不存在", error_key="errors.bot_not_found")

        # 构造消息：尝试从 payload 中提取关键字段，否则原样输出 JSON
        try:
            data = json.loads(event["payload"])
        except Exception:
            data = {}

        msg = _format_event_message(data, bot)

        success = send_webhook(bot["webhook_url"], msg)
        if not success:
            raise DatabaseError(
                "转发失败，请检查 Bot Webhook URL",
                error_key="errors.webhook_forward_failed",
            )

        conn.execute(
            "UPDATE cd_webhook_events SET forwarded=1, forwarded_at=? WHERE id=?",
            (datetime.now(), eid),
        )
        return ok(message="已转发到 Bot")


# ── 公开接收端点（无需登录验证，靠 token 鉴权）──

@router.post("/receive/{token}")
async def receive_webhook(
    token: str,
    request: Request,
    db: Database = Depends(get_db),
):
    """CI / 外部系统 POST 到此端点，靠 token 匹配 Webhook 配置。
    收到后存记录，若配了 bot_id 则自动转发。"""
    with db.conn() as conn:
        wh = conn.execute(
            "SELECT * FROM cd_webhooks WHERE token=? AND enabled=1", (token,)
        ).fetchone()
        if not wh:
            raise NotFoundError("Webhook 不存在或已禁用", error_key="errors.webhook_not_found")

        # 读取原始 body
        body = await request.body()
        payload_str = body.decode("utf-8") if body else "{}"

        # 存事件记录
        conn.execute(
            "INSERT INTO cd_webhook_events (webhook_id, payload) VALUES (?,?)",
            (wh["id"], payload_str),
        )
        event_id = conn.execute("SELECT LAST_INSERT_ID() AS id").fetchone()["id"]

        # 自动转发
        bot_id = wh["bot_id"]
        if bot_id:
            bot = conn.execute(
                "SELECT * FROM cd_bots WHERE id=?", (bot_id,)
            ).fetchone()
            if bot:
                try:
                    data = json.loads(payload_str)
                except Exception:
                    data = {}
                msg = _format_event_message(data, bot)
                send_webhook(bot["webhook_url"], msg)
                conn.execute(
                    "UPDATE cd_webhook_events SET forwarded=1, forwarded_at=? WHERE id=?",
                    (datetime.now(), event_id),
                )

        return ok(message="received")


def _format_event_message(data: dict, bot) -> str:
    """从 payload 中提取关键字段，配合 Bot 模板生成消息。
    如果 Bot 有自定义模板则用模板，否则用默认格式。"""
    tpl_raw = bot.get("template", "")
    tpl = (tpl_raw or "").strip()

    if tpl:
        # 用户自定义模板：支持 {project} {tag} {image} {status} {time} 等占位符
        try:
            return tpl.format(
                time=data.get("built_at", data.get("time", "")),
                project=data.get("project", ""),
                tag=data.get("tag", ""),
                status=data.get("status", ""),
                image=data.get("image", ""),
                target=data.get("target", ""),
                mode=data.get("mode", ""),
            )
        except (KeyError, IndexError):
            pass  # 模板变量不匹配，回退到默认格式

    # 默认格式：提取关键字段拼成可读消息
    parts = []
    for key in ("project", "tag", "image", "status", "built_at"):
        val = data.get(key)
        if val:
            parts.append(f"{key}: {val}")
    if parts:
        return "\n".join(parts)
    # 兜底：原样 JSON
    return json.dumps(data, ensure_ascii=False, indent=2)
