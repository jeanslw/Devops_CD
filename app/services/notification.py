"""通知服务 — 钉钉 / 企业微信 webhook"""


def send_webhook(url: str, message: str) -> bool:
    """发送钉钉/企微 text 消息，失败不抛异常。"""
    if not url or not message:
        return False
    try:
        import requests
        response = requests.post(
            url,
            json={"msgtype": "text", "text": {"content": message}},
            timeout=5,
        )
        return response.ok
    except Exception:
        return False
