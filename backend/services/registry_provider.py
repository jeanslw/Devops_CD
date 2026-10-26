"""镜像仓库提供方抽象 - Harbor 与标准 OCI Distribution(registry:2)。

对齐 Devops-Glue 2.11 的 RegistryProviderInterface:
  - HarborClient(harbor_client.py) → type="harbor",支持漏洞扫描 / 大小 / 推送时间
  - RegistryV2Provider(本文件)     → type="registry",只用 /v2/ 标准接口

仓库标识统一为「至少两段的 OCI 仓库名」(如 mycode/runner-ci):Harbor 侧按首段拆 project,
标准 registry 侧原样使用。

上层(registry_service)只依赖本模块的抽象:契约下发的 registry_type 决定工厂产出哪个实现;
能力开关(capabilities)随 GET /api/registry/config 下发给前端,前端据此隐藏
不可用操作(标准 registry 无漏洞扫描接口与推送时间；digest / 大小由 Glue 解析 manifest 提供)。

仓库类型与地址只取自 Glue 契约端点(GET /api/integration/services/cd)下发的 registry_type /
registry_url;本地不再保存任何镜像仓库凭据,全部经 Glue API 代理(cd-service token)。
"""

import logging
import urllib.parse
from typing import Any

from backend.database import contract_registry_hints
from backend.services.ci_client import CiClientError, get_ci_client

logger = logging.getLogger("registry")

# 能力开关:前端按此隐藏不可用列 / 按钮(Tag 删除已整体移除,两种类型都不提供)
CAPABILITIES = {
    "harbor": {"scan": True, "size": True, "push_time": True},
    # 标准 registry:2 无漏洞扫描接口、无推送时间(协议不记录)；
    # 但 digest 与镜像大小可经 Glue 解析 manifest 提供，故 size=True。
    "registry": {"scan": False, "size": True, "push_time": False},
}


class RegistryUnavailableError(Exception):
    """镜像仓库不可达(地址错 / 网络不通 / 服务未启动 / 返回非 JSON)"""


# 旧名保留:services / routers / tests 历史上按 Harbor 命名捕获同一个异常
HarborUnavailableError = RegistryUnavailableError


class RegistryConfigError(RuntimeError):
    """Glue 契约未下发镜像仓库地址时抛出(契约端点不可达,或 Glue 版本无该字段).

    调用方转为 503 或参数错误;不返回本地回退地址.
    """


class RegistryPermissionDeniedError(Exception):
    """镜像仓库拒绝访问(HTTP 401/403):凭据错误、token 缺少拉取(pull)权限或账号无该仓库的查询权限"""


def registry_type_hint() -> str:
    """契约下发的仓库类型原样提示:harbor | registry | ""(未取到).

    取不到的原因:Glue 版本无该字段,或服务凭据缺 Harbor 查询权限(Glue 闸门将字段置空),
    或契约端点短时不可达。显示层据此如实呈现「未知」,不得与默认类型混淆.
    """
    hint, _ = contract_registry_hints()
    return (hint or "").strip().lower()


def registry_type() -> str:
    """当前镜像仓库类型(供提供方选择 / 能力开关):harbor(默认) | registry

    契约提示为空时按 harbor 处理:旧版 Glue 只对接过 Harbor,取不到类型不等于标准仓库,
    不能因脏值把扫描 / 大小 / 推送时间列全部隐藏.
    """
    return registry_type_hint() or "harbor"


def registry_base_url() -> str:
    """镜像仓库地址(scheme-less host[:port]),取自 Glue 契约下发的 registry_url

    取不到时抛 RegistryConfigError。返回值不含 scheme 与尾部斜杠,客户端按需补 https / http 候选。
    """
    _, url = contract_registry_hints()
    if not url:
        raise RegistryConfigError(
            "未取到镜像仓库地址：地址由 Glue 契约端点 GET /api/integration/services/cd 下发，"
            "需 Glue 2.11 及以上，且 CI_API_URL / CI_API_TOKEN 可访问该端点"
        )
    # 契约里的地址可能带 scheme（运维填了 http://...）。部署登录（docker login）与镜像引用
    # 只接受 host[:port]，这里统一剥掉 scheme 与尾部斜杠。
    url = url.strip().rstrip("/")
    if "://" in url:
        url = url.split("://", 1)[1]
    return url


def registry_url_raw() -> str:
    """契约下发的原始仓库地址(保留 scheme,去掉尾部斜杠),未下发时返回 "".

    与 registry_base_url() 的区别:此处不剥 scheme,供需要识别「明文 HTTP 仓库」的调用方
    (部署前的 insecure-registries 放行校验)使用.
    """
    _, url = contract_registry_hints()
    return (url or "").strip().rstrip("/")


def registry_scheme() -> str:
    """契约仓库地址声明的 scheme:http | https | ""(地址缺失或未声明 scheme).

    未声明 scheme 时返回 "":docker 对无 scheme 的镜像引用默认走 HTTPS,故按 https 处理.
    """
    raw = registry_url_raw()
    if "://" not in raw:
        return ""
    return raw.split("://", 1)[0].strip().lower()


def registry_is_insecure() -> bool:
    """契约是否把仓库声明为明文 HTTP(registry_url 以 http:// 开头).

    声明为明文时,目标机 dockerd 必须把该 host[:port] 列入 daemon.json 的
    insecure-registries,否则拉取只会走 HTTPS 并报
    `server gave HTTP response to HTTPS client`——docker CLI 侧没有强制明文的开关,
    这一步只能落到目标机配置上(见 deployers/registry_insecure.py).
    """
    return registry_scheme() == "http"


def registry_capabilities() -> dict:
    """按契约下发的仓库类型返回能力开关(不连仓库,供 /api/registry/config 等静态接口)"""
    return dict(CAPABILITIES.get(registry_type(), CAPABILITIES["harbor"]))


def split_repo(repo_full: str) -> tuple[str, str]:
    """仓库名拆 project / repo:首段为 project,其余为 repo(OCI 仓库名可含斜杠)"""
    if "/" in repo_full:
        project, repo = repo_full.split("/", 1)
        return project, repo
    return repo_full, repo_full


def base_url_candidates(raw: str) -> list[str]:
    """镜像仓库地址候选:已带 scheme 则原样,否则先 https 后 http"""
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        raise RegistryConfigError("未取到镜像仓库地址：地址由 Glue 契约端点下发（需 Glue 2.11 及以上）")
    if raw.startswith("http://") or raw.startswith("https://"):
        return [raw]
    return [f"https://{raw}", f"http://{raw}"]


class RegistryProvider:
    """镜像仓库提供方基类 - 只声明契约,HarborClient / RegistryV2Provider 各自实现"""

    #: 提供方类型标识:harbor | registry(与 Glue 的 RegistryProviderInterface::type() 同义)
    type = "registry"
    #: 能力开关(漏洞扫描 / 大小 / 推送时间)
    CAPABILITIES: dict = CAPABILITIES["registry"]

    @property
    def version(self) -> str:
        """Harbor 为 v1 / v2;标准 registry 固定为 'registry'"""
        raise NotImplementedError

    @property
    def base_url(self) -> str:
        raise NotImplementedError

    @property
    def capabilities(self) -> dict:
        return dict(self.CAPABILITIES)

    def list_artifacts(self, repo_full: str) -> list[dict]:
        """仓库下所有 tags(统一字段,缺失能力的位置留空值)"""
        raise NotImplementedError

    def get_scan_report(self, repo_full: str, tag: str) -> dict | None:
        """漏洞扫描报告;不支持扫描的提供方返回 None"""
        raise NotImplementedError

    def trigger_scan(self, repo_full: str, tag: str) -> bool:
        """触发重新扫描;不支持扫描的提供方返回 False"""
        raise NotImplementedError


class RegistryV2Provider(RegistryProvider):
    """标准 OCI Distribution v2(registry:2)提供方:经 Glue /api/registry/* 代理.

    该类型不提供漏洞扫描 / 推送时间能力(协议不记录推送时间)；
    digest 与镜像大小由 Glue 解析 manifest 提供(能力开关 size=True).
    """

    type = "registry"
    CAPABILITIES = CAPABILITIES["registry"]

    # 与 Glue 端 tags 分页(page_size 钳制上限 200)对齐;页数上限兜底防御异常 total_pages
    TAGS_PAGE_SIZE = 200
    TAGS_MAX_PAGES = 50

    def __init__(self):
        self._ci = get_ci_client()
        self._base = self._ci._base

    @property
    def version(self) -> str:
        return "registry"

    @property
    def base_url(self) -> str:
        return self._base

    def _get(self, path: str, params: dict | None = None) -> Any:
        """GET Glue /api/registry/* :401/403→权限错误,404→空,其他→不可用"""
        try:
            return self._ci.get_json(path, params=params)
        except CiClientError as e:
            code = e.status_code
            if code in (401, 403):
                raise RegistryPermissionDeniedError(f"镜像仓库拒绝访问（HTTP {code}）: {path}")
            if code == 404:
                return []
            raise HarborUnavailableError(f"镜像仓库不可用：{e}") from e

    def _fetch_all_tags(self, path: str) -> list:
        """拉取全部 tag:Glue 2.11+ 的 tags 端点支持分页信封({items,total_pages,...}),按页遍历;
        旧版 Glue / 降级路径返回裸数组(名称或详情对象),原样使用。

        同步语义上要的是全量(tag 一个都不能少),分页的意义是把单次响应约束在
        TAGS_PAGE_SIZE 条内(仓库 tag 上千时避免一次性大响应)。
        任何一页失败照常抛错,由上层按同步失败处理,不静默丢页。
        """
        first = self._get(path, params={"page": 1, "page_size": self.TAGS_PAGE_SIZE})
        if isinstance(first, dict) and isinstance(first.get("items"), list):
            all_tags = list(first["items"])
            try:
                total_pages = max(1, int(first.get("total_pages") or 1))
            except (TypeError, ValueError):
                total_pages = 1
            for p in range(2, min(total_pages, self.TAGS_MAX_PAGES) + 1):
                page = self._get(path, params={"page": p, "page_size": self.TAGS_PAGE_SIZE})
                if isinstance(page, dict) and isinstance(page.get("items"), list):
                    all_tags.extend(page["items"])
            return all_tags
        return first if isinstance(first, list) else []

    def list_artifacts(self, repo_full: str) -> list[dict]:
        project, repo = split_repo(repo_full)
        enc_proj = urllib.parse.quote(project, safe="")
        enc_repo = urllib.parse.quote(repo, safe="")
        tags = self._fetch_all_tags(f"/api/registry/{enc_proj}/repositories/{enc_repo}/tags")
        result = []
        for tag in tags:
            # Glue registry 端点现已返回详细对象(name/digest/size_bytes),
            # 也兼容纯 tag 名字符串(旧契约 / 降级路径)。
            if isinstance(tag, dict):
                name = tag.get("name")
                digest = (tag.get("digest") or "") if tag.get("digest") is not None else ""
                size_bytes = int(tag.get("size_bytes") or 0)
            else:
                name = tag
                digest = ""
                size_bytes = 0
            if not name:
                continue
            result.append(
                {
                    "tag": name,
                    "digest": digest,
                    "size_bytes": size_bytes,
                    "size_mb": round(size_bytes / 1048576, 2),
                    "push_time": "",
                    "pull_time": "",
                    "scan_status": "",
                    "scan_severity": "",
                    "vuln_critical": 0,
                    "vuln_high": 0,
                    "vuln_medium": 0,
                    "vuln_low": 0,
                    "vuln_fixable": 0,
                }
            )
        return result

    def get_scan_report(self, repo_full: str, tag: str) -> dict | None:
        """标准 OCI Distribution 没有漏洞扫描接口,统一返回 None"""
        return None

    def trigger_scan(self, repo_full: str, tag: str) -> bool:
        """标准 OCI Distribution 没有扫描触发接口,统一返回 False"""
        return False


def get_registry_provider() -> RegistryProvider:
    """按契约下发的仓库类型构造提供方:harbor(默认) → HarborClient;registry → RegistryV2Provider"""
    if registry_type() == "registry":
        return RegistryV2Provider()
    # 延迟导入:HarborClient 依赖本模块的基类与异常,模块级导入会成环
    from backend.services.harbor_client import HarborClient

    return HarborClient()
