"""Webhook 接收路由 - 接收 CI 构建完成等外部事件,可选转发到 Bot"""

import asyncio
import json
import secrets
from datetime import datetime, timezone

import pymysql
from fastapi import APIRouter, Depends, Request

from backend.auth import cd_deploy_gate, get_db, require_perm, verify_token
from backend.crypto import ENCRYPT_PREFIX, decrypt, encrypt
from backend.database import Database
from backend.exceptions import ConflictError, DatabaseError, NotFoundError, ValidationError
from backend.models import WebhookForwardRequest, WebhookRequest
from backend.responses import ok
from backend.services.notification import render_template, send_webhook

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

_MAX_BODY_BYTES = 1024 * 1024


def _generate_token() -> str:
    """生成 32 字符随机 token"""
    return secrets.token_urlsafe(24)


def _decrypt_webhook_token(stored: str) -> str:
    """严格解密入库 token;非 enc: 密文按不可用处理(不兼容存量明文)."""
    if not stored or not stored.startswith(ENCRYPT_PREFIX):
        return ""
    try:
        return decrypt(stored)
    except Exception:
        return ""


def _find_enabled_webhook_by_token(conn, plain_token: str):
    """用明文 URL token 匹配数据库中的加密 token:逐行解密后常量时间比较."""
    rows = conn.execute("SELECT * FROM cd_webhooks WHERE enabled=1").fetchall()
    for row in rows:
        candidate = _decrypt_webhook_token(row["token"])
        if candidate and secrets.compare_digest(candidate, plain_token):
            return row
    return None


@router.get("")
def list_webhooks(
    db: Database = Depends(get_db),
    _username: str = Depends(verify_token),
):
    """列出所有 Webhook 配置"""
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT id, name, bot_id, enabled, created_at, token FROM cd_webhooks ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["has_token"] = bool(_decrypt_webhook_token(item.pop("token", "") or ""))
            result.append(item)
        return result


@router.post("")
def create_webhook(
    req: WebhookRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    """创建 Webhook,自动生成 token"""
    token = _generate_token()
    encrypted_token = encrypt(token)
    with db.conn() as conn:
        try:
            conn.execute(
                "INSERT INTO cd_webhooks (name, token, bot_id, enabled) VALUES (?,?,?,?)",
                (req.name, encrypted_token, req.bot_id, 1),
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
    """更新 Webhook 配置(名称,关联 Bot)"""
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
        row = conn.execute("SELECT enabled FROM cd_webhooks WHERE id=?", (wid,)).fetchone()
        if not row:
            raise NotFoundError("Webhook 不存在", error_key="errors.webhook_not_found")
        new_val = 0 if row["enabled"] else 1
        conn.execute("UPDATE cd_webhooks SET enabled=? WHERE id=?", (new_val, wid))
        return ok(data={"enabled": new_val})


@router.get("/{wid}/events")
def list_events(
    wid: int,
    page: int = 1,
    page_size: int = 20,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    """查看某 Webhook 收到的事件列表(分页)"""
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size
    with db.conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS cnt FROM cd_webhook_events WHERE webhook_id=?", (wid,)).fetchone()[
            "cnt"
        ]
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


@router.post("/events/{eid}/forward")
def forward_event(
    eid: int,
    req: WebhookForwardRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    """手动转发某条事件到指定 Bot"""
    with db.conn() as conn:
        event = conn.execute("SELECT payload FROM cd_webhook_events WHERE id=?", (eid,)).fetchone()
        if not event:
            raise NotFoundError("事件不存在", error_key="errors.webhook_event_not_found")

        bot = conn.execute("SELECT * FROM cd_bots WHERE id=?", (req.bot_id,)).fetchone()
        if not bot:
            raise NotFoundError("Bot 不存在", error_key="errors.bot_not_found")

        payload = event["payload"]
        bot_row = dict(bot)

    try:
        data = json.loads(payload)
    except Exception:
        data = {}

    msg = _format_event_message(data, bot_row)
    success = send_webhook(bot_row["webhook_url"], msg, bot_row.get("type", ""))
    if not success:
        raise DatabaseError(
            "转发失败，请检查 Bot Webhook URL",
            error_key="errors.webhook_forward_failed",
        )

    with db.conn() as conn:
        conn.execute(
            "UPDATE cd_webhook_events SET forwarded=1, forwarded_at=? WHERE id=?",
            (datetime.now(timezone.utc), eid),
        )
    return ok(message="已转发到 Bot")


@router.post("/receive/{token}")
async def receive_webhook(
    token: str,
    request: Request,
    db: Database = Depends(get_db),
):
    """CI / 外部系统 POST 到此端点,靠 token 匹配配置;存记录,配了 bot_id 则自动转发."""
    cd_deploy_gate(db)

    with db.conn() as conn:
        wh = _find_enabled_webhook_by_token(conn, token)
    if not wh:
        raise NotFoundError("Webhook 不存在或已禁用", error_key="errors.webhook_not_found")
    webhook_id = wh["id"]

    content_length = request.headers.get("content-length", "")
    if content_length.isdigit():
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > _MAX_BODY_BYTES:
            raise ValidationError("Webhook payload 过大", error_key="errors.webhook_payload_too_large")

    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise ValidationError("Webhook payload 过大", error_key="errors.webhook_payload_too_large")
        chunks.append(chunk)
    body = b"".join(chunks)
    payload_str = body.decode("utf-8", errors="replace") if body else "{}"

    with db.conn() as conn:
        row = conn.execute("SELECT enabled, bot_id FROM cd_webhooks WHERE id=?", (webhook_id,)).fetchone()
        if not row or not row["enabled"]:
            raise NotFoundError("Webhook 不存在或已禁用", error_key="errors.webhook_not_found")
        bot_id = row["bot_id"]
        cur = conn.execute(
            "INSERT INTO cd_webhook_events (webhook_id, payload) VALUES (?,?)",
            (webhook_id, payload_str),
        )
        event_id = cur.lastrowid

    if bot_id:
        with db.conn() as conn:
            bot = conn.execute("SELECT * FROM cd_bots WHERE id=?", (bot_id,)).fetchone()
        if bot:
            bot = dict(bot)
            try:
                data = json.loads(payload_str)
            except Exception:
                data = {}
            msg = _format_event_message(data, bot)
            sent = await asyncio.to_thread(send_webhook, bot["webhook_url"], msg, bot.get("type", ""))
            with db.conn() as conn:
                conn.execute(
                    "UPDATE cd_webhook_events SET forwarded=?, forwarded_at=? WHERE id=?",
                    (1 if sent else 0, datetime.now(timezone.utc), event_id),
                )

    return ok(message="received")


def _format_event_message(data: dict, bot) -> str:
    """从 payload 提取关键字段生成消息;Bot 有自定义模板则用模板,否则用默认格式."""
    if not isinstance(data, dict):
        data = {}
    values = {
        "time": data.get("built_at", data.get("time", "")),
        "project": data.get("project", ""),
        "tag": data.get("tag", ""),
        "status": data.get("status", ""),
        "image": data.get("image", ""),
        "target": data.get("target", ""),
        "mode": data.get("mode", ""),
    }
    parts = []
    for key in ("project", "tag", "image", "status", "built_at"):
        val = data.get(key)
        if val:
            parts.append(f"{key}: {val}")
    default_msg = "\n".join(parts) if parts else json.dumps(data, ensure_ascii=False, indent=2)
    return render_template(bot.get("template", ""), values, default_msg)
