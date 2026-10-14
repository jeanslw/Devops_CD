"""出站地址族优先级(HTTP_IP_FAMILY=ipv4 | ipv6 | auto,默认 ipv4).

install_ip_family() 把 socket.getaddrinfo 换成下面这版:按 _preferred_family 稳定重排结果,
目标族排最前(同族内保持系统返回顺序),条目不增不减;auto 不替换任何函数.

覆盖面:进程内所有经 socket.getaddrinfo 解析的出站连接(requests / urllib3、http.client、
paramiko SSH).未覆盖:asyncio 连接入口(loop.create_connection / asyncio.open_connection)
用事件循环自带的解析器,不受此处替换影响 —— CD 当前出站全为同步,将来引入异步 HTTP 客户端
时需在这里补上对应的连接入口.
"""

import logging
import socket
import threading

logger = logging.getLogger("net")

_installed = False
_lock = threading.Lock()
_orig_getaddrinfo = socket.getaddrinfo
# 当前生效的地址族优先级:ipv4 / ipv6 / auto(install_ip_family() 写入)
_preferred_family = "ipv4"


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """socket.getaddrinfo 的替代:目标族排最前;auto、指定 family、结果不足两条时原样返回."""
    infos = _orig_getaddrinfo(host, port, family, type, proto, flags)
    if _preferred_family == "auto" or family or len(infos) < 2:
        return infos
    want = socket.AF_INET if _preferred_family == "ipv4" else socket.AF_INET6
    return sorted(infos, key=lambda res: 0 if res[0] == want else 1)


def install_ip_family(family: str | None = None) -> bool:
    """安装地址族优先级(幂等,线程安全)。返回是否处于生效状态。

    生效时替换 socket.getaddrinfo —— 进程内所有经 socket 解析的出站连接都继承该顺序;
    family=None 读 settings.http_ip_family,未知取值按 auto(不替换任何函数)。
    """
    global _installed, _preferred_family
    with _lock:
        if _installed:
            return True
        if family is None:
            from backend.config import settings

            family = settings.http_ip_family
        family = (family or "").strip().lower() or "auto"
        if family not in ("ipv4", "ipv6", "auto"):
            logger.warning("HTTP_IP_FAMILY=%r 不是 ipv4 / ipv6 / auto,按 auto 处理", family)
            family = "auto"
        _preferred_family = family
        if family == "auto":
            logger.info("出站地址族优先级:auto(按系统顺序)")
            return False
        socket.getaddrinfo = getaddrinfo
        _installed = True
        logger.info("出站地址族优先级:%s(另一族兜底)", family)
        return True
