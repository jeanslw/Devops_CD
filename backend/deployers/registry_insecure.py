"""明文 HTTP 镜像仓库的目标机 docker 放行处理(契约 registry_url 以 http:// 声明时).

为什么必须有这一步:
    dockerd 对未列入 insecure-registries 的镜像仓库**只尝试 HTTPS**;而镜像引用与
    docker login 的语法只接受 host[:port],scheme 无处安放,docker CLI 也没有 podman /
    skopeo 的 --tls-verify=false 之类开关(客户端无法强制明文).因此「契约声明了 http://」
    这件事只能落到目标机 dockerd 的配置上,否则拉取必然报
    `server gave HTTP response to HTTPS client`(它在说 HTTPS,却收到了 HTTP 响应).

两种处理方式(由 settings.registry_insecure_auto 决定):
    - false(默认):只检测 + 给出可复制的修复命令,绝不写目标机文件;未放行即中止拉取
      (避免继续落到 compose 4c 的 Docker Hub 静默换源,把同名公共镜像当作自己的镜像部署).
    - true:备份并合并目标机 /etc/docker/daemon.json 的 insecure-registries,再 reload
      (不生效 / 该单元无 ExecReload 时回退 restart)dockerd.注意 restart 会重启该机所有
      容器(未开 live-restore 时),故默认关闭.

本模块只服务于「CD 自己执行 docker pull 的部署器」(compose remote 模式).SSH 模式是纯透传
(命令由用户模板给出,CD 不介入),K8S 的拉取由节点 kubelet / containerd 完成(对应
containerd 的 certs.d 配置),都不在此模块覆盖范围内.
"""

import ipaddress
import json
import logging
import shlex
from typing import NamedTuple

from .base import _exec_on

logger = logging.getLogger(__name__)

DAEMON_DIR = "/etc/docker"
DAEMON_JSON = f"{DAEMON_DIR}/daemon.json"
# dockerd 23+ 可用多个 --config-file 指定 drop-in 配置,这里一并检测(不存在则忽略)
DAEMON_DROPIN_GLOB = f"{DAEMON_DIR}/daemon.json.d/*.json"
DAEMON_BACKUP = f"{DAEMON_JSON}.cd-bak"

_DAEMON_MARKER = "===CDDAEMON:"

# 一次性 dump 目标机全部 dockerd 配置(纯只读;文件/目录不存在也算成功,
# 便于区分「确实没配置」与「读不到」).命令不包 sh -c,与部署器既有约定一致.
_DUMP_CMD = (
    f"for f in {DAEMON_JSON} {DAEMON_DROPIN_GLOB}; do "
    f'[ -f "$f" ] && {{ echo "{_DAEMON_MARKER}$f"; cat "$f" 2>/dev/null; }}; done; exit 0'
)

# 「HTTPS 客户端收到 HTTP 响应」这一确定性故障签名(明文 HTTP 仓库未放行)
_PLAIN_HTTP_SIGNATURE = "server gave http response to https client"


class InsecureCheckResult(NamedTuple):
    """放行检测结果:ok=是否可继续拉取,auto_fixed=是否由 CD 自动写完配置,detail=失败时给运维的指引."""

    ok: bool
    auto_fixed: bool
    detail: str


def parse_daemon_configs(dump: str) -> dict[str, str]:
    """把 dump 输出切成 {配置路径: 文件内容}(按 ===CDDAEMON: 标记分段)."""
    files: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in (dump or "").splitlines():
        if line.startswith(_DAEMON_MARKER):
            if current is not None:
                files[current] = "\n".join(buf)
            current = line[len(_DAEMON_MARKER) :].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        files[current] = "\n".join(buf)
    return files


def _entry_matches(entry: str, registry: str) -> bool:
    """insecure-registries 的单个条目是否覆盖 registry(host[:port]).

    对齐 dockerd 的匹配语义:条目可以是 host / host:port / CIDR(按 IP 网段放行).
    条目为 CIDR 时,要求 registry 的主机部分是字面 IP 且落在网段内;域名形式的仓库
    只能靠逐字相等的条目放行(dockerd 亦然).
    """
    e = (entry or "").strip().lower()
    r = (registry or "").strip().lower()
    if not e or not r:
        return False
    if e == r:
        return True
    if "/" not in e:
        return False
    # registry 可能是 [v6]:port / host:port / host,取最后一个冒号前的主机部分
    host = r.rsplit(":", 1)[0].strip("[]") if ":" in r else r
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network(e, strict=False)
    except ValueError:
        return False


def insecure_configured(files: dict[str, str], registry: str) -> bool:
    """目标机 dockerd 配置里是否已放行该仓库(遍历所有 daemon 配置,任一命中即视为已放行)."""
    for text in files.values():
        try:
            cfg = json.loads(text)
        except (ValueError, TypeError):
            continue
        entries = cfg.get("insecure-registries") if isinstance(cfg, dict) else None
        if not isinstance(entries, list):
            continue
        if any(_entry_matches(e, registry) for e in entries if isinstance(e, str)):
            return True
    return False


def merge_insecure_entry(text: str, registry: str) -> str | None:
    """把 registry 追加进 daemon.json 的 insecure-registries,返回新文本.

    返回 None 表示「不能安全改写」:原内容不是 JSON 对象(例如带注释或语法错误).
    此时调用方不得写入,避免把运维手工维护的配置改坏.
    """
    cfg: dict = {}
    if (text or "").strip():
        try:
            cfg = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(cfg, dict):
            return None
    entries = cfg.get("insecure-registries")
    if not isinstance(entries, list):
        entries = []
    kept = [e for e in entries if isinstance(e, str)]
    if not any(_entry_matches(e, registry) for e in kept):
        kept.append(registry)
    cfg["insecure-registries"] = kept
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def is_plain_http_registry_error(text: str) -> bool:
    """拉取输出是否命中「明文 HTTP 仓库未放行」故障签名.

    命中时上层必须直接失败,不得回退 Docker Hub:换源会把同名公共镜像当自己的镜像部署上线.
    """
    return _PLAIN_HTTP_SIGNATURE in (text or "").lower()


def guidance(registry: str, reason: str = "") -> str:
    """未放行时给运维的可复制修复指引(纯文本,随部署输出原样展示;中英双语)."""
    lines = [
        f"[plain-HTTP registry] target docker daemon does not allow {registry}.",
        f"目标机 dockerd 未放行明文 HTTP 仓库 {registry}:未列入 insecure-registries 的仓库只尝试 HTTPS,拉取必然失败.",
    ]
    if reason:
        lines.append(f"原因 / reason: {reason}")
    lines += [
        f"请在目标机({registry} 所在的 docker 宿主机,需 root/sudo)执行:",
        "  1) 合并配置(保留 daemon.json 原有内容):",
        "     sudo python3 - <<'PY'",
        "     import json, os, shutil",
        f'     p = "{DAEMON_JSON}"',
        "     cfg = {}",
        "     if os.path.exists(p):",
        f'         shutil.copy(p, "{DAEMON_BACKUP}")',
        "         with open(p) as f: cfg = json.load(f)",
        f'     os.makedirs("{DAEMON_DIR}", exist_ok=True)',
        '     inc = cfg.get("insecure-registries") or []',
        f'     if "{registry}" not in inc: inc.append("{registry}")',
        '     cfg["insecure-registries"] = inc',
        '     with open(p, "w") as f: json.dump(cfg, f, indent=2)',
        "     PY",
        "  2) 生效(reload 对 insecure-registries 的支持随 docker 版本而异,不生效即 restart;restart 会重启该机所有容器):",
        "     sudo systemctl reload docker || sudo systemctl restart docker",
        f"  3) 复验:docker pull {registry}/<repo>:<tag>",
        "另一条路:给仓库加 TLS(契约 registry_url 下发 https://)后重新部署,CD 侧无需任何配置.",
        f"如需 CD 自动完成 1)/2)(需目标机 root,且会重启 dockerd),置 REGISTRY_INSECURE_AUTO=true 后重新部署.",
    ]
    return "\n".join(lines)


def ensure_insecure_registry(ssh, registry: str, allow_auto: bool = False) -> InsecureCheckResult:
    """拉取前确认目标机 dockerd 已放行明文 HTTP 仓库 registry(host[:port]).

    allow_auto=False(默认):只读检测;未放行返回 ok=False + 修复指引,绝不写目标机任何文件.
    allow_auto=True:未放行时备份并合并 daemon.json,再 reload(失败回退 restart)dockerd.
    任何写操作失败都返回 ok=False + 指引,由调用方中止部署并输出(绝不静默继续).
    """
    files = parse_daemon_configs(_exec_on(ssh, _DUMP_CMD)[0])
    if insecure_configured(files, registry):
        return InsecureCheckResult(ok=True, auto_fixed=False, detail="")

    if not allow_auto:
        return InsecureCheckResult(ok=False, auto_fixed=False, detail=guidance(registry))

    # 自动修复只改主 daemon.json:drop-in 是多文件,合并语义会互相覆盖,不碰
    merged = merge_insecure_entry(files.get(DAEMON_JSON, ""), registry)
    if merged is None:
        return InsecureCheckResult(
            ok=False,
            auto_fixed=False,
            detail=guidance(registry, reason=f"{DAEMON_JSON} 不是合法 JSON 对象,已放弃自动改写以免破坏人工配置"),
        )

    write_cmd = (
        f"mkdir -p {DAEMON_DIR}; "
        f"if [ -f {DAEMON_JSON} ]; then cp -p {DAEMON_JSON} {DAEMON_BACKUP}; fi; "
        f"printf %s {shlex.quote(merged)} > {DAEMON_JSON}"
    )
    out, err, ec = _exec_on(ssh, write_cmd)
    if ec != 0:
        return InsecureCheckResult(
            ok=False,
            auto_fixed=False,
            detail=guidance(registry, reason=f"写入 {DAEMON_JSON} 失败(需 root/sudo):{err or out}"),
        )
    logger.info("daemon.json updated for insecure registry %s (backup: %s)", registry, DAEMON_BACKUP)

    # SIGHUP 重载即可让 dockerd 重读 insecure-registries 的版本较多,但并非所有版本 / 单元都支持,
    # 故 reload 失败即回退 restart(代价:该机容器重启)
    out, err, ec = _exec_on(ssh, "systemctl reload docker 2>&1 || systemctl restart docker 2>&1")
    if ec != 0:
        return InsecureCheckResult(
            ok=False,
            auto_fixed=True,
            detail=guidance(registry, reason=f"配置已写入但重载 dockerd 失败:{err or out}"),
        )
    return InsecureCheckResult(ok=True, auto_fixed=True, detail="")
