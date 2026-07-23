"""通知服务 — 钉钉 / 企业微信 webhook"""

import time
import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime

from app.config import settings


def send_webhook(url: str, message: str) -> bool:
    """发送钉钉/企微 text 消息，失败不抛异常。"""
    if not url or not message:
        return False
    try:
        # 钉钉加签
        secret = settings.dingtalk_secret
        if secret and "oapi.dingtalk.com" in url:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"{url}&timestamp={timestamp}&sign={sign}"

        import requests
        response = requests.post(
            url,
            json={"msgtype": "text", "text": {"content": message}},
            timeout=5,
        )
        return response.ok
    except Exception:
        return False


def notify_deploy(db, bot_id: int, tag: str, project_key: str, image: str,
                  status: str, deploy_mode: str, targets: list):
    """构造消息并发送部署通知。bot_id=0 则跳过。
    targets 如 ["集群[192.168.1.1]"] 或 ["单机[1.1.1.1]", "docker[2.2.2.2]"]
    """
    if not bot_id:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_str = "、".join(targets)
    msg = f"[{now}] {project_key} 【变动提醒】\n部署版本：{tag} --> {status}\n部署目标：{target_str}\n部署模式: {deploy_mode}\n部署镜像: {image}"

    conn = db.conn()
    try:
        bot = conn.execute("SELECT * FROM cd_bots WHERE id=?", (bot_id,)).fetchone()
        if bot:
            send_webhook(bot["webhook_url"], msg)
    finally:
        conn.close()
