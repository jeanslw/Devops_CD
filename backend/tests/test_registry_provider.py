"""镜像仓库提供方抽象(registry_provider)单元测试.

覆盖:
    - registry_type():契约 registry_type 生效(大小写 / 空白归一),契约提示为空时默认 harbor
    - registry_base_url():取契约下发的 registry_url;取不到抛 RegistryConfigError(不返回空串)
    - registry_capabilities():能力开关闸门(harbor 全开,registry 全关,未知类型回退 harbor)
    - config.py:仓库「查询」配置(harbor_registry / registry_type / harbor_verify_ssl)已移除;部署登录凭据为 registry_user / registry_password
    - HarborUnavailableError 是 RegistryUnavailableError 的别名(services / routers 向后兼容)
    - RegistryService._mapped_repo 双读:registry_repository(Glue 2.11+)优先,旧字段 harbor_repository 仍可读取
    - RegistryV2Provider(标准 OCI Distribution / registry:2):
        * 经 Glue /api/registry/* 代理(cd-service token)
        * tags 列表映射为 artifacts(digest 等能力字段留空)
        * Glue 2.11+ tags 端点分页信封({items,total_pages})按页遍历取全量;裸数组(旧契约)原样兼容
        * 404 仓库不存在 → 空列表(容错);401/403 → 权限错误;其他 → RegistryUnavailableError
        * 漏洞扫描接口固定不可用(get_scan_report=None / trigger_scan=False)
    - get_registry_provider() 工厂:契约类型 registry → RegistryV2Provider,否则 HarborClient

运行(项目根):
    python -m pytest backend/tests/test_registry_provider.py -q
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.config import settings

settings.db_driver = "sqlite"

import backend.services.harbor_client as hc  # noqa: E402
import backend.services.registry_provider as rp  # noqa: E402
from backend.services.ci_client import CiClientError  # noqa: E402
from backend.services.registry_provider import (  # noqa: E402
    RegistryPermissionDeniedError,
    RegistryUnavailableError,
    RegistryV2Provider,
    get_registry_provider,
    registry_capabilities,
    registry_type,
    registry_type_hint,
)
from backend.services.registry_service import RegistryService  # noqa: E402

SENTINEL_HARBOR = object()
SENTINEL_V2 = object()


class TestRegistryTypeAndCapabilities(unittest.TestCase):
    """registry_type() 取值与能力开关闸门(不连仓库)"""

    def test_type_comes_from_contract(self):
        with patch.object(rp, "contract_registry_hints", return_value=("registry", "hub.example.com")):
            self.assertEqual(registry_type(), "registry")
        with patch.object(rp, "contract_registry_hints", return_value=("harbor", "hub.example.com")):
            self.assertEqual(registry_type(), "harbor")

    def test_type_normalized(self):
        with patch.object(rp, "contract_registry_hints", return_value=("  Registry  ", "")):
            self.assertEqual(registry_type(), "registry")

    def test_type_defaults_to_harbor_when_hint_missing(self):
        """旧版 Glue 无 registry_type → harbor:静态接口不该因为取不到类型就 500"""
        with patch.object(rp, "contract_registry_hints", return_value=("", "")):
            self.assertEqual(registry_type(), "harbor")

    def test_capabilities_gates(self):
        with patch.object(rp, "contract_registry_hints", return_value=("harbor", "hub.example.com")):
            self.assertEqual(registry_capabilities(), {"scan": True, "size": True, "push_time": True})
        with patch.object(rp, "contract_registry_hints", return_value=("registry", "reg.example.com")):
            self.assertEqual(
                registry_capabilities(),
                # size=True:digest/大小由 Glue 解析 registry:2 manifest 提供(工作区改动后)
                {"scan": False, "size": True, "push_time": False},
            )
        with patch.object(rp, "contract_registry_hints", return_value=("what?", "")):
            self.assertEqual(registry_capabilities(), {"scan": True, "size": True, "push_time": True})
        with patch.object(rp, "contract_registry_hints", return_value=("", "")):
            self.assertEqual(registry_capabilities(), {"scan": True, "size": True, "push_time": True})

    def test_hint_raw_vs_effective_default(self):
        """原样提示与默认类型分离:显示层拿原样值(未取到时为 ""),
        提供方选择 / 能力开关仍默认 harbor(取不到 ≠ 标准仓库,不能因此隐藏列)"""
        with patch.object(rp, "contract_registry_hints", return_value=("harbor", "hub.example.com")):
            self.assertEqual(registry_type_hint(), "harbor")
            self.assertEqual(registry_type(), "harbor")
        with patch.object(rp, "contract_registry_hints", return_value=("Registry", "")):
            self.assertEqual(registry_type_hint(), "registry")
            self.assertEqual(registry_type(), "registry")
        with patch.object(rp, "contract_registry_hints", return_value=("", "")):
            self.assertEqual(registry_type_hint(), "")
            self.assertEqual(registry_type(), "harbor")

    def test_harbor_unavailable_alias(self):
        self.assertIs(hc.HarborUnavailableError, RegistryUnavailableError)

    def test_registry_query_config_removed_login_creds_kept(self):
        """仓库「查询」配置已移除(地址 / 类型 / TLS 校验);部署登录凭据为 registry_user / registry_password"""
        self.assertFalse(hasattr(settings, "harbor_registry"))
        self.assertFalse(hasattr(settings, "registry_type"))
        self.assertFalse(hasattr(settings, "harbor_verify_ssl"))
        self.assertTrue(hasattr(settings, "registry_user"))
        self.assertTrue(hasattr(settings, "registry_password"))


class TestRegistryScheme(unittest.TestCase):
    """registry_url_raw / registry_scheme / registry_is_insecure:明文 HTTP 仓库识别(不连仓库)"""

    def test_http_contract_is_insecure_but_image_ref_stays_scheme_less(self):
        with patch.object(rp, "contract_registry_hints", return_value=("registry", "http://192.168.137.5:5000")):
            self.assertEqual(rp.registry_url_raw(), "http://192.168.137.5:5000")
            self.assertEqual(rp.registry_scheme(), "http")
            self.assertTrue(rp.registry_is_insecure())
            # 镜像引用 / docker login 只接受 host[:port]:scheme 必须被剥掉(见 registry_base_url)
            self.assertEqual(rp.registry_base_url(), "192.168.137.5:5000")

    def test_https_contract_is_not_insecure(self):
        with patch.object(rp, "contract_registry_hints", return_value=("harbor", "https://hub.example.com/")):
            self.assertEqual(rp.registry_url_raw(), "https://hub.example.com")
            self.assertEqual(rp.registry_scheme(), "https")
            self.assertFalse(rp.registry_is_insecure())

    def test_scheme_is_case_insensitive(self):
        with patch.object(rp, "contract_registry_hints", return_value=("registry", "HTTP://Reg.Example.com:5000")):
            self.assertEqual(rp.registry_scheme(), "http")
            self.assertTrue(rp.registry_is_insecure())

    def test_scheme_less_address_is_treated_as_https(self):
        """docker 对无 scheme 的镜像引用默认走 HTTPS,不得判为明文"""
        with patch.object(rp, "contract_registry_hints", return_value=("registry", "reg.example.com:5000")):
            self.assertEqual(rp.registry_url_raw(), "reg.example.com:5000")
            self.assertEqual(rp.registry_scheme(), "")
            self.assertFalse(rp.registry_is_insecure())

    def test_empty_contract_hint(self):
        with patch.object(rp, "contract_registry_hints", return_value=("", "")):
            self.assertEqual(rp.registry_url_raw(), "")
            self.assertEqual(rp.registry_scheme(), "")
            self.assertFalse(rp.registry_is_insecure())


class TestMappedRepoDoubleRead(unittest.TestCase):
    """CI 映射字段双读:Glue 2.11 起改名 registry_repository,旧版 harbor_repository 仍可读取"""

    def test_prefers_registry_repository(self):
        self.assertEqual(RegistryService._mapped_repo({"registry_repository": "a/b"}), "a/b")
        self.assertEqual(
            RegistryService._mapped_repo({"registry_repository": "a/b", "harbor_repository": "c/d"}),
            "a/b",
        )

    def test_falls_back_to_harbor_repository(self):
        self.assertEqual(RegistryService._mapped_repo({"harbor_repository": "a/b"}), "a/b")
        self.assertEqual(
            RegistryService._mapped_repo({"registry_repository": "", "harbor_repository": " a/b "}),
            "a/b",
        )

    def test_missing_or_blank(self):
        self.assertEqual(RegistryService._mapped_repo({}), "")
        self.assertEqual(RegistryService._mapped_repo({"registry_repository": "  "}), "")


class TestContractSourcedRegistry(unittest.TestCase):
    """仓库地址 / 类型取自 Glue 契约(本地已无地址 / 类型配置):

    契约值直接生效;契约取不到 registry_url 时 registry_base_url() 抛 RegistryConfigError,
    不返回空串。
    """

    def test_type_and_capabilities_come_from_contract(self):
        with patch.object(rp, "contract_registry_hints", return_value=("registry", "reg.example.com")):
            self.assertEqual(registry_type(), "registry")
            # size=True:digest/大小由 Glue 解析 registry:2 manifest 提供(工作区改动后)
            self.assertEqual(registry_capabilities(), {"scan": False, "size": True, "push_time": False})

    def test_base_url_comes_from_contract(self):
        with patch.object(rp, "contract_registry_hints", return_value=("", "hub.contract.com")):
            self.assertEqual(rp.registry_base_url(), "hub.contract.com")

    def test_missing_url_raises_config_error(self):
        with patch.object(rp, "contract_registry_hints", return_value=("harbor", "")):
            with self.assertRaises(rp.RegistryConfigError) as ctx:
                rp.registry_base_url()
        self.assertIn("契约端点", str(ctx.exception))
        self.assertIsInstance(ctx.exception, RuntimeError)

    def test_base_url_strips_scheme_and_trailing_slash(self):
        """契约地址若带 scheme(运维填了 http://...),部署侧只保留 host[:port]"""
        with patch.object(rp, "contract_registry_hints", return_value=("registry", "http://127.0.0.1:5000/")):
            self.assertEqual(rp.registry_base_url(), "127.0.0.1:5000")
        with patch.object(rp, "contract_registry_hints", return_value=("harbor", "https://harbor.local")):
            self.assertEqual(rp.registry_base_url(), "harbor.local")

    def test_base_url_candidates_rejects_empty(self):
        with self.assertRaises(rp.RegistryConfigError):
            rp.base_url_candidates("")

    def test_base_url_candidates_http_scheme_kept_as_is(self):
        """显式 http(s) scheme 原样保留,不强行改 https(支持 http 仓库);尾部斜杠剥离"""
        self.assertEqual(rp.base_url_candidates("http://reg.example.com"), ["http://reg.example.com"])
        self.assertEqual(rp.base_url_candidates("https://reg.example.com/"), ["https://reg.example.com"])

    def test_base_url_candidates_no_scheme_tries_https_then_http(self):
        """未带 scheme 时先 https 后 http 两个候选"""
        self.assertEqual(
            rp.base_url_candidates("reg.example.com"),
            ["https://reg.example.com", "http://reg.example.com"],
        )

    def test_factory_follows_contract_type(self):
        """工厂按契约类型选实现:契约说 registry 时产出 RegistryV2Provider"""
        with (
            patch.object(rp, "contract_registry_hints", return_value=("registry", "reg.example.com")),
            patch.object(hc, "HarborClient", Mock(return_value=SENTINEL_HARBOR)) as m_h,
            patch.object(rp, "RegistryV2Provider", Mock(return_value=SENTINEL_V2)) as m_v2,
        ):
            self.assertIs(get_registry_provider(), SENTINEL_V2)
        m_v2.assert_called_once()
        m_h.assert_not_called()


class TestRegistryV2Provider(unittest.TestCase):
    """标准 OCI Distribution(registry:2)提供方:经 Glue /api/registry/* 代理"""

    def setUp(self):
        self.ci = MagicMock()
        self.ci._base = "https://glue.example.com"
        patcher = patch.object(rp, "get_ci_client", return_value=self.ci)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = RegistryV2Provider()

    def test_base_url_and_type(self):
        self.assertEqual(self.provider.base_url, "https://glue.example.com")
        self.assertEqual(self.provider.version, "registry")
        self.assertEqual(self.provider.type, "registry")
        # size=True:digest/大小由 Glue 解析 registry:2 manifest 提供(工作区改动后)
        self.assertEqual(self.provider.capabilities, {"scan": False, "size": True, "push_time": False})

    def test_list_artifacts_from_tags(self):
        self.ci.get_json.return_value = ["v1", "v2", "v3"]
        arts = self.provider.list_artifacts("mycode/static")
        self.assertEqual([a["tag"] for a in arts], ["v1", "v2", "v3"])
        # 现在带分页参数请求 Glue(page_size=200 与 Glue 端钳制上限一致)
        self.ci.get_json.assert_called_once_with(
            "/api/registry/mycode/repositories/static/tags",
            params={"page": 1, "page_size": 200},
        )
        for a in arts:
            self.assertEqual(a["digest"], "")
            self.assertEqual(a["size_bytes"], 0)
            self.assertEqual(a["push_time"], "")
            self.assertEqual(a["scan_status"], "")
            self.assertEqual(a["vuln_critical"], 0)

    def test_list_artifacts_paginates_paged_envelope(self):
        """Glue 2.11+ tags 端点返回分页信封时必须按 total_pages 遍历取全量,
        digest / size_bytes 由 Glue 解析 manifest 提供后照常入库(不因分页丢失字段)"""
        self.ci.get_json.side_effect = [
            {
                "items": [
                    {"name": "v1", "digest": "sha256:1111", "size_bytes": 1024},
                    {"name": "v2", "digest": "sha256:2222", "size_bytes": 2048},
                ],
                "total": 3,
                "page": 1,
                "page_size": 200,
                "total_pages": 2,
            },
            {
                "items": [{"name": "v3", "digest": "sha256:3333", "size_bytes": 4096}],
                "total": 3,
                "page": 2,
                "page_size": 200,
                "total_pages": 2,
            },
        ]
        arts = self.provider.list_artifacts("mycode/static")
        self.assertEqual([a["tag"] for a in arts], ["v1", "v2", "v3"])
        self.assertEqual([a["digest"] for a in arts], ["sha256:1111", "sha256:2222", "sha256:3333"])
        self.assertEqual([a["size_bytes"] for a in arts], [1024, 2048, 4096])
        self.assertEqual(self.ci.get_json.call_count, 2, "total_pages=2 必须恰好请求两页,不多不少")
        # 两页的请求参数必须按页递增
        self.assertEqual(
            [c.kwargs.get("params") for c in self.ci.get_json.call_args_list],
            [{"page": 1, "page_size": 200}, {"page": 2, "page_size": 200}],
        )

    def test_list_artifacts_envelope_single_page_no_extra_calls(self):
        """单页信封(total_pages=1)不得再发后续请求"""
        self.ci.get_json.return_value = {
            "items": [{"name": "v1", "digest": "sha256:1", "size_bytes": 1}],
            "total": 1,
            "page": 1,
            "page_size": 200,
            "total_pages": 1,
        }
        arts = self.provider.list_artifacts("mycode/static")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["digest"], "sha256:1")
        self.assertEqual(self.ci.get_json.call_count, 1)

    def test_list_artifacts_dict_tags(self):
        self.ci.get_json.return_value = [{"name": "v1"}, {"name": "v2"}]
        arts = self.provider.list_artifacts("a/b")
        self.assertEqual([a["tag"] for a in arts], ["v1", "v2"])

    def test_missing_repo_returns_empty(self):
        self.ci.get_json.side_effect = CiClientError("404", status_code=404)
        self.assertEqual(self.provider.list_artifacts("missing/repo"), [])

    def test_permission_denied_raises(self):
        for status in (401, 403):
            self.ci.get_json.side_effect = CiClientError("denied", status_code=status)
            with self.assertRaises(RegistryPermissionDeniedError):
                self.provider.list_artifacts("a/b")
            self.ci.get_json.side_effect = None

    def test_server_error_raises_unavailable(self):
        self.ci.get_json.side_effect = CiClientError("500", status_code=500)
        with self.assertRaises(RegistryUnavailableError):
            self.provider.list_artifacts("a/b")

    def test_scan_unsupported(self):
        self.assertIsNone(self.provider.get_scan_report("a/b", "v1"))
        self.assertFalse(self.provider.trigger_scan("a/b", "v1"))


class TestHarborClient(unittest.TestCase):
    """Harbor 提供方:经 Glue /api/harbor/* 代理(cd-service token,已含 registry.read)"""

    def setUp(self):
        self.ci = MagicMock()
        self.ci._base = "https://glue.example.com"
        patcher = patch.object(hc, "get_ci_client", return_value=self.ci)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = hc.HarborClient()

    def test_base_url_and_type(self):
        self.assertEqual(self.provider.base_url, "https://glue.example.com")
        self.assertEqual(self.provider.version, "v2")
        self.assertEqual(self.provider.type, "harbor")
        self.assertEqual(self.provider.capabilities, {"scan": True, "size": True, "push_time": True})

    def test_list_artifacts_from_tags(self):
        self.ci.get_json.return_value = ["v1", "v2"]
        arts = self.provider.list_artifacts("mycode/static")
        self.assertEqual([a["tag"] for a in arts], ["v1", "v2"])
        self.ci.get_json.assert_called_once_with("/api/harbor/mycode/repositories/static/tags")
        for a in arts:
            self.assertEqual(a["digest"], "")

    def test_get_scan_report_ok(self):
        self.ci.get_json.return_value = {"scan_status": "Success", "severity": "High"}
        report = self.provider.get_scan_report("mycode/static", "v1")
        self.assertEqual(report["scan_status"], "Success")
        self.ci.get_json.assert_called_once_with("/api/harbor/mycode/repositories/static/tags/v1/scan-report")

    def test_get_scan_report_none_on_empty(self):
        self.ci.get_json.return_value = []
        self.assertIsNone(self.provider.get_scan_report("mycode/static", "v1"))

    def test_get_scan_report_none_on_unavailable(self):
        self.ci.get_json.side_effect = CiClientError("500", status_code=500)
        self.assertIsNone(self.provider.get_scan_report("mycode/static", "v1"))

    def test_trigger_scan(self):
        self.ci.post_json.return_value = {}
        self.assertTrue(self.provider.trigger_scan("mycode/static", "v1"))
        self.ci.post_json.assert_called_once_with("/api/harbor/mycode/repositories/static/tags/v1/scan")

    def test_permission_denied_raises(self):
        self.ci.get_json.side_effect = CiClientError("401", status_code=401)
        with self.assertRaises(RegistryPermissionDeniedError):
            self.provider.list_artifacts("mycode/static")


class TestRegistryProviderFactory(unittest.TestCase):
    """get_registry_provider 工厂:按契约下发的仓库类型选择实现"""

    def setUp(self):
        self._env = (patch.object(rp, "contract_registry_hints", return_value=("harbor", "http://harbor.local")),)
        for p in self._env:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._env])

    def test_default_type_is_harbor(self):
        with (
            patch.object(hc, "HarborClient", Mock(return_value=SENTINEL_HARBOR)) as m_h,
            patch.object(rp, "RegistryV2Provider", Mock(return_value=SENTINEL_V2)) as m_v2,
        ):
            get_registry_provider()
        m_h.assert_called_once()
        m_v2.assert_not_called()

    def test_registry_type_selects_v2(self):
        with (
            patch.object(rp, "contract_registry_hints", return_value=("registry", "http://reg.local")),
            patch.object(hc, "HarborClient", Mock(return_value=SENTINEL_HARBOR)) as m_h,
            patch.object(rp, "RegistryV2Provider", Mock(return_value=SENTINEL_V2)) as m_v2,
        ):
            self.assertIs(get_registry_provider(), SENTINEL_V2)
        m_v2.assert_called_once()
        m_h.assert_not_called()

    def test_unknown_type_falls_back_to_harbor(self):
        with (
            patch.object(rp, "contract_registry_hints", return_value=("etcd", "http://harbor.local")),
            patch.object(hc, "HarborClient", Mock(return_value=SENTINEL_HARBOR)),
            patch.object(rp, "RegistryV2Provider", Mock(return_value=SENTINEL_V2)) as m_v2,
        ):
            get_registry_provider()
        m_v2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
