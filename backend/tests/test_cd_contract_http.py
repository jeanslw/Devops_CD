"""契约读的唯一通道:Glue GET /api/integration/services/cd + 窗口缓存.

背景(见 Glue ServiceContractController 的类注释):CD 的"是否被授权、以哪种建表方式授权"
曾经直读共享库的 integrated_services 行,把 CD 与 CI 的库结构绑死(表名 / 列名 / 驱动都是耦合面);
契约端点把这份契约搬到 HTTP 上:契约仍归 CI 写入,消费方只读一个窄接口. v2.9.0 起 Glue 表名
收口(ci_integrated_services),CD 侧**直读回退已彻底移除** -- 端点是唯一通道,CD 不再读
共享库的任何 CI 表.

本文件锁定三件事:
  1) 200 的 enabled / provision 以响应为准(_normalize_contract 是唯一归一化实现,不存在
     双通道判定分叉);token_active 仅供展示,不作为闸门条件;
  2) 任何"问不到"(404 / 405 / 401 / 403 / 网络 / 形状不符 / 未配 CI)都按"契约不可达"
     fail-closed:抛 ContractUnavailableError(503 ci_contract_unreachable),不会退化成
     "一直未启用"(问不到 ≠ CI 关过开关),也不静默放行;
  3) 窗口缓存:窗口内复用(不再打 Glue),过期 / invalidate_contract_cache() 后重新问;
     端点不可达也有负缓存(窗口内快速失败,不每个请求都白等一次超时).

全部用假 client(**不发真实网络**):本机根 .env 往往已配 CI_API_URL / CI_API_TOKEN,真连会把单测
变成环境依赖. 脚手架的建表阶段走 cd_contract.py 的进程内替身(模拟 Glue 答复 enabled=1);
断言阶段把真函数换回来,让"不可达 / 缓存"逻辑真实跑一遍.

运行(项目根):
    python -m pytest backend/tests/test_cd_contract_http.py -q
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import requests  # noqa: E402

from backend.config import settings  # noqa: E402

settings.db_driver = "sqlite"

import backend.database as database  # noqa: E402
from backend.auth import cd_deploy_gate  # noqa: E402
from backend.database import (  # noqa: E402
    CdIntegrationState,
    ContractUnavailableError,
    Database,
    _normalize_contract,
    contract_registry_hints,
    invalidate_contract_cache,
)
from backend.exceptions import AppException  # noqa: E402
from backend.services.ci_client import CiClient, CiClientError  # noqa: E402

from cd_contract import ORIGINAL_FETCH_CONTRACT_HTTP, enable_cd  # noqa: E402


class _FakeClient:
    """假 CI 客户端:返回预设的契约响应(或抛错),并记录调用次数(证明缓存 / 负缓存生效)."""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0

    def get_service_contract(self, service: str):
        self.calls += 1
        assert service == "cd", f"契约读只允许查 CD 自己的服务名，实际：{service}"
        if self.exc is not None:
            raise self.exc
        return self.result


class _ContractHttpTestCase(unittest.TestCase):
    """公共脚手架:临时 SQLite + 契约替身建表 + 断言阶段换回真函数 + 假 client.

    建表阶段走 cd_contract 的进程内替身(模拟 Glue 端点答复 enabled=1 + auto),与其它闸门
    测试同一路径;每个用例再把真 _fetch_contract_http 换回来,让"不可达 / 负缓存"逻辑真实生效.
    """

    def setUp(self):
        self._env = (
            settings.ci_contract_ttl,
            settings.ci_api_url,
            settings.ci_api_token,
        )
        settings.ci_contract_ttl = 0  # 建表阶段不缓存(替身路径, ttl=0 即时生效)
        invalidate_contract_cache()

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "contract.db")
        enable_cd(self.db_path)  # 契约替身的"真相":enabled=1 + auto(模拟 Glue 端点答复)
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn():
            pass  # 建全清单(走契约替身)
        Database._tables_ensured = True

        settings.ci_contract_ttl = 30
        settings.ci_api_url = "http://glue.test:8080"
        settings.ci_api_token = "dg_test"
        invalidate_contract_cache()

    def tearDown(self):
        (settings.ci_contract_ttl, settings.ci_api_url, settings.ci_api_token) = self._env
        invalidate_contract_cache()
        Database._tables_ensured = False
        self._tmp.cleanup()

    def _patch_client(self, result=None, exc=None) -> _FakeClient:
        """把真 _fetch_contract_http 换回来并注入假 client(记录调用次数,零网络)."""
        fake = _FakeClient(result=result, exc=exc)
        for patcher in (
            patch.object(database, "_fetch_contract_http", ORIGINAL_FETCH_CONTRACT_HTTP),
            patch.object(database, "get_ci_client", return_value=fake),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake


class HttpStateTestCase(_ContractHttpTestCase):
    """200 的状态判定:以响应为准."""

    def test_enabled_auto(self):
        fake = self._patch_client(
            result={"service": "cd", "enabled": 1, "provision": "auto", "token_active": True, "updated_at": ""}
        )
        state = self.db.cd_integration_state()
        self.assertEqual(state, CdIntegrationState(True, "auto", True))
        self.assertEqual(fake.calls, 1, "HTTP 通道应被真实调用一次")

    def test_enabled_manual(self):
        self._patch_client(result={"service": "cd", "enabled": True, "provision": "manual", "token_active": True})
        self.assertEqual(self.db.cd_integration_state(), CdIntegrationState(True, "manual", True))

    def test_disabled_blocks_gate(self):
        """200 + enabled=false:未启用(端点明确答复)→ 闸门 503 cd_not_enabled."""
        self._patch_client(result={"service": "cd", "enabled": 0, "provision": "auto", "token_active": True})
        state = self.db.cd_integration_state()
        self.assertFalse(state.enabled)
        self.assertTrue(state.row_exists)
        with self.assertRaises(AppException) as ctx:
            cd_deploy_gate(self.db)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")

    def test_unknown_provision_normalizes_to_auto(self):
        """provision 只认 CI 写入的 manual,其余(空值 / 未知值)按 auto."""
        self._patch_client(result={"service": "cd", "enabled": 1, "provision": "weird", "token_active": True})
        self.assertEqual(self.db.cd_integration_state().provision, "auto")

    def test_token_active_is_not_a_gate_condition(self):
        """token_active 仅供展示(Glue 明确):凭据活性为假也不许把 CD 关掉(否则一次凭据漂移就全站 503)."""
        self._patch_client(
            result={"service": "cd", "enabled": True, "provision": "auto", "token_active": False, "updated_at": ""}
        )
        self.assertTrue(self.db.cd_integration_state().enabled)

    def test_normalization_is_the_single_implementation(self):
        """_normalize_contract 是唯一归一化实现:任何响应值都按同一规则判定,不存在第二套."""
        for raw_enabled, raw_provision in [
            (1, "auto"),
            (0, "manual"),
            ("true", "MANUAL"),
            ("false", ""),
            ("", "auto"),
            (None, None),
        ]:
            invalidate_contract_cache()
            self._patch_client(result={"enabled": raw_enabled, "provision": raw_provision, "token_active": True})
            state = self.db.cd_integration_state()
            self.assertEqual(
                (state.enabled, state.provision),
                _normalize_contract(raw_enabled, raw_provision),
                f"归一化结果不一致：enabled={raw_enabled!r} provision={raw_provision!r}",
            )


class UnreachableTestCase(_ContractHttpTestCase):
    """ "问不到"≠"未启用":一律按契约不可达 fail-closed(503 ci_contract_unreachable),不会静默放行."""

    def test_404_raises_contract_unreachable(self):
        """404 = 服务名不在 Glue 白名单(配置写错),不是未启用:必须 503,否则拼错服务名会静默关站."""
        self._patch_client(exc=CiClientError("CI API GET 失败: unknown_service", status_code=404))
        with self.assertRaises(ContractUnavailableError) as ctx:
            self.db.cd_integration_state()
        self.assertEqual(ctx.exception.error_key, "errors.ci_contract_unreachable")

    def test_405_raises_contract_unreachable(self):
        """405 = Glue 版本过旧(无该路由):503 提示环境故障,而不是假装 CD 被关了."""
        self._patch_client(exc=CiClientError("CI API GET 失败 405", status_code=405))
        with self.assertRaises(ContractUnavailableError):
            self.db.cd_integration_state()

    def test_401_with_revoked_token_raises_contract_unreachable(self):
        """Glue 停用集成服务时会一并撤销 CD 的 token:此时 401 与"CI 关了开关"无法区分 --
        不按状态码猜,统一按"问不到"处理(503),真相以运维查 CI 后台为准."""
        self._patch_client(exc=CiClientError("CI API token 无效（已撤销或过期）"))
        with self.assertRaises(ContractUnavailableError):
            self.db.cd_integration_state()

    def test_403_scope_mismatch_raises_contract_unreachable(self):
        self._patch_client(exc=CiClientError("CI API GET 失败 403", status_code=403))
        with self.assertRaises(ContractUnavailableError):
            self.db.cd_integration_state()

    def test_network_error_raises_contract_unreachable(self):
        self._patch_client(exc=requests.ConnectionError("glue unreachable"))
        with self.assertRaises(ContractUnavailableError):
            self.db.cd_integration_state()

    def test_response_without_enabled_field_raises_contract_unreachable(self):
        """缺 enabled 字段时若当 false,CD 会被静默关掉:必须 503(形状不符 = 问不到)."""
        self._patch_client(result={"service": "cd", "provision": "manual", "token_active": True})
        with self.assertRaises(ContractUnavailableError):
            self.db.cd_integration_state()

    def test_non_dict_response_raises_contract_unreachable(self):
        self._patch_client(result="<html>proxy error</html>")
        with self.assertRaises(ContractUnavailableError):
            self.db.cd_integration_state()

    def test_missing_ci_config_raises_contract_unreachable(self):
        """未配 CI_API_URL / CI_API_TOKEN:契约通道没有目的地,属部署配置缺失 → 503(一次 HTTP 都不发)."""
        settings.ci_api_url = ""
        fake = self._patch_client(result={"enabled": 1, "provision": "auto"})
        with self.assertRaises(ContractUnavailableError) as ctx:
            self.db.cd_integration_state()
        self.assertIn("CI_API_URL", ctx.exception.message)
        self.assertEqual(fake.calls, 0)

    def test_unreachable_gate_is_503_not_not_enabled(self):
        """闸门链路里端点不可达同样 503 ci_contract_unreachable:与 cd_not_enabled 严格区分,别把运维指错方向."""
        self._patch_client(exc=requests.ConnectionError("glue unreachable"))
        with self.assertRaises(AppException) as ctx:
            cd_deploy_gate(self.db)
        self.assertEqual(ctx.exception.error_key, "errors.ci_contract_unreachable")
        self.assertNotEqual(ctx.exception.error_key, "errors.cd_not_enabled")


class CacheTestCase(_ContractHttpTestCase):
    """窗口缓存:窗口内复用,过期 / invalidate 后重新问;不可达也有负缓存."""

    def test_window_reuses_result_without_touching_db(self):
        """HTTP 命中时压根不该碰数据库:用一个"父目录不存在"的库路径证明这一点."""
        fake = self._patch_client(result={"enabled": 1, "provision": "auto"})
        never_db_dir = os.path.join(self._tmp.name, "nodir")
        db = Database(os.path.join(never_db_dir, "never.db"))
        self.assertTrue(db.cd_integration_state().enabled)
        self.assertTrue(db.cd_integration_state().enabled)
        self.assertEqual(fake.calls, 1, "窗口内的第二次读必须命中缓存")
        self.assertFalse(os.path.exists(never_db_dir), "HTTP 通道可用时不应打开数据库")

    def test_ttl_zero_queries_every_time(self):
        settings.ci_contract_ttl = 0
        fake = self._patch_client(result={"enabled": 1, "provision": "auto"})
        self.assertTrue(self.db.cd_integration_state().enabled)
        self.assertTrue(self.db.cd_integration_state().enabled)
        self.assertEqual(fake.calls, 2)

    def test_window_expiry_requeries(self):
        fake = self._patch_client(result={"enabled": 1, "provision": "auto"})
        clock = [1000.0]
        with patch.object(database, "_contract_now", side_effect=lambda: clock[0]):
            self.assertTrue(self.db.cd_integration_state().enabled)
            self.assertEqual(fake.calls, 1)
            clock[0] += settings.ci_contract_ttl + 1  # 窗口过期
            fake.result = {"enabled": 0, "provision": "auto"}
            self.assertFalse(self.db.cd_integration_state().enabled)
        self.assertEqual(fake.calls, 2)

    def test_invalidate_contract_cache_makes_next_call_fresh(self):
        fake = self._patch_client(result={"enabled": 1, "provision": "auto"})
        self.assertTrue(self.db.cd_integration_state().enabled)
        invalidate_contract_cache()
        fake.result = {"enabled": 0, "provision": "auto"}  # 刚在 CI 后台关了开关
        self.assertFalse(self.db.cd_integration_state().enabled)
        self.assertEqual(fake.calls, 2)

    def test_http_down_is_remembered_within_window(self):
        """端点不可达进负缓存:窗口内不再重试(否则每个请求都白等一次 connect 超时,登录会被拖住),
        且每次都得到同一个 503(不是"有时 503 有时未启用"的摇摆)."""
        fake = self._patch_client(exc=requests.ConnectionError("glue unreachable"))
        for _ in range(3):
            with self.assertRaises(ContractUnavailableError):
                self.db.cd_integration_state()
        self.assertEqual(fake.calls, 1, "不可达窗口内的重试必须被负缓存挡住")
        invalidate_contract_cache()
        fake.exc = None  # Glue 恢复:清掉故障注入
        fake.result = {"enabled": 1, "provision": "auto"}
        self.assertTrue(self.db.cd_integration_state().enabled, "invalidate 后应重新问并恢复")


class RegistryHintsTestCase(_ContractHttpTestCase):
    """镜像仓库提示(registry_type / registry_url):契约透传 + 归一化 + 契约不可达时不报错.

    契约端点不可达时返回上次取到的值,从未取到过则返回 ("", "");本读取不参与授权判定.
    """

    def test_registry_fields_pass_through_normalized(self):
        """Glue 下发的镜像仓库字段随契约进入缓存:类型归一(小写),URL 剥 scheme 与尾部斜杠."""
        self._patch_client(
            result={
                "service": "cd",
                "enabled": 1,
                "provision": "auto",
                "registry_type": " Registry ",
                "registry_url": "https://hub.example.com/",
            }
        )
        self.assertEqual(contract_registry_hints(), ("registry", "hub.example.com"))

    def test_old_glue_without_fields_yields_empty_hints(self):
        """旧版 Glue(无 registry_* 字段)→ ("",""):消费方按 harbor 默认 / 地址缺失报错处理."""
        self._patch_client(result={"service": "cd", "enabled": 1, "provision": "auto"})
        self.assertEqual(contract_registry_hints(), ("", ""))

    def test_invalid_registry_type_yields_empty_hint(self):
        """registry_type 非法值归一为 "":不让脏值把消费方引到不存在的提供方."""
        self._patch_client(result={"enabled": 1, "provision": "auto", "registry_type": "what?"})
        self.assertEqual(contract_registry_hints(), ("", ""))

    def test_unreachable_endpoint_fails_open(self):
        """契约端点不可达且从没取到过 → ("",""):registry 功能不因此返回 503."""
        self._patch_client(exc=requests.ConnectionError("glue unreachable"))
        self.assertEqual(contract_registry_hints(), ("", ""))

    def test_unreachable_endpoint_reuses_last_known_hints(self):
        """取到过再问不到 → 沿用上次取到的地址 / 类型,不返回空值."""
        settings.ci_contract_ttl = 0  # 每请求都真问:本用例的"问不到"必须打到端点
        fake = self._patch_client(
            result={"enabled": 1, "provision": "auto", "registry_type": "harbor", "registry_url": "hub.example.com"}
        )
        self.assertEqual(contract_registry_hints(), ("harbor", "hub.example.com"))
        fake.exc = requests.ConnectionError("glue unreachable")  # Glue 抖了
        self.assertEqual(contract_registry_hints(), ("harbor", "hub.example.com"), "抖动期间应沿用上次取到的值")
        self.assertEqual(fake.calls, 2)

    def test_hints_share_the_window_cache(self):
        """提示读取与闸门读共用窗口缓存:一次 HTTP 两种读都命中,不额外打 Glue."""
        fake = self._patch_client(
            result={"enabled": 1, "provision": "auto", "registry_type": "harbor", "registry_url": "hub.example.com"}
        )
        self.assertTrue(self.db.cd_integration_state().enabled)
        self.assertEqual(contract_registry_hints(), ("harbor", "hub.example.com"))
        self.assertEqual(fake.calls, 1, "窗口内提示读必须命中闸门读留下的缓存")


class CiClientContractCallTestCase(unittest.TestCase):
    """调用形状(HARD 约束):契约端点不能带 ?format=json,且响应解包要防御."""

    def test_call_shape_and_data_unwrap(self):
        client = CiClient("http://glue.test:8080", token="dg_test", timeout=30)
        captured: dict = {}

        def fake_get(url, params=None, timeout=None):
            captured.update(url=url, params=params, timeout=timeout)
            return {"data": {"enabled": True, "provision": "auto"}}

        with patch.object(CiClient, "_get", side_effect=fake_get):
            data = client.get_service_contract("cd")
        self.assertEqual(captured["url"], "http://glue.test:8080/api/integration/services/cd")
        self.assertIsNone(captured["params"], "契约端点不得带 ?format=json（会被包成 {data: ...}）")
        self.assertLessEqual(captured["timeout"], 5.0, "契约读在请求路径上：超时必须收紧，宁可快速失败")
        self.assertEqual(data, {"enabled": True, "provision": "auto"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
