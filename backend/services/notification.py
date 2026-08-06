"""通知服务 — 钉钉 / 企业微信 webhook"""

import time
import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime
from urllib.parse import urlparse

from backend.config import settings

_ALLOWED_WEBHOOK_DOMAINS = ["oapi.dingtalk.com", "qyapi.weixin.qq.com", "open.feishu.cn", "api.feishu.cn"]


def _is_allowed_webhook(url: str) -> bool:
    """检查 webhook URL 是否在允许的域名白名单内。"""
    try:
        domain = urlparse(url).hostname or ""
        return any(domain == d or domain.endswith("." + d) for d in _ALLOWED_WEBHOOK_DOMAINS)
    except Exception:
        return False


def send_webhook(url: str, message: str) -> bool:
    """发送钉钉/企微 text 消息，失败不抛异常。"""
    if not url or not message:
        return False
    if not _is_allowed_webhook(url):
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


# ── 默认模板（中/英文） ──
_DEFAULT_TEMPLATES = {
    "zh": (
        "[{time}] {project} [部署通知]\n"
        "版本：{tag} --> {status}\n"
        "目标：{target}\n"
        "模式：{mode}\n"
        "镜像：{image}"
    ),
    "en": (
        "[{time}] {project} [Deploy Notification]\n"
        "Version: {tag} --> {status}\n"
        "Target: {target}\n"
        "Mode: {mode}\n"
        "Image: {image}"
    ),
}

# ── 中英文状态/模式映射 ──
_LANG_MAP = {
    "zh": {
        "status": {
            "✅ Success":               "✅ 部署成功",
            "❌ Failed":                "❌ 部署失败",
            "⚠️ Partial success":       "⚠️ 部分成功",
        },
        "mode": {
            "ssh":      "SSH 单机",
            "compose":  "Docker Compose",
            "remote":   "远程部署",
            "commands": "自定义命令",
            "docker":   "Docker",
            "kubectl":  "Kubectl",
            "helm":     "Helm",
            "argocd":   "Argo CD",
            "fluxcd":   "Flux CD",
        },
    },
}

def _t(lang: str, field: str, value: str) -> str:
    """根据 lang 翻译 status/mode 值，lang 非 zh/en 则原样返回。"""
    if lang not in _LANG_MAP or field not in _LANG_MAP[lang]:
        return value
    # 状态可能是 "⚠️ Partial success 2/3" 这种带数字格式，做前缀匹配
    if field == "status":
        for en_key, zh_val in _LANG_MAP[lang]["status"].items():
            if value.startswith(en_key):
                return zh_val + value[len(en_key):]
    return _LANG_MAP[lang][field].get(value, value)


def notify_deploy(db, bot_id: int, tag: str, project_key: str, image: str,
                  status: str, deploy_mode: str, targets: list, lang: str = "en"):
    """构造消息并发送部署通知。bot_id=0 则跳过。
    targets 如 ["k8s[192.168.1.1]"] 或 ["ssh[1.1.1.1]", "docker[2.2.2.2]"]
    lang: 前端当前语言 (en/zh)，用于选择 status/mode 的文本语言
    """
    if not bot_id:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_str = ", ".join(targets)

    # 翻译 status 和 mode
    display_status = _t(lang, "status", status)
    display_mode = _t(lang, "mode", deploy_mode)

    with db.conn() as conn:
        bot = conn.execute("SELECT * FROM cd_bots WHERE id=?", (bot_id,)).fetchone()
        if not bot:
            return
        tpl_raw = bot["template"] if "template" in bot.keys() else ""
        tpl = (tpl_raw or "").strip() or _DEFAULT_TEMPLATES.get(lang, _DEFAULT_TEMPLATES["en"]).strip()
        msg = tpl.format(
            time=now, project=project_key, tag=tag,
            status=display_status, image=image, target=target_str, mode=display_mode,
        )
        send_webhook(bot["webhook_url"], msg)
