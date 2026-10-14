"""CD 构建页 ref 候选值(分支 + Tag):Glue GET /api/git/{path}/refs 的取值与回退.

背景:构建页的 ref 下拉此前只列分支(GET /api/build/{path}/branches),Tag 无从选择;Glue ≥ 2.9.0
提供合并端点 /api/git/{path}/refs(一次 job→仓库映射解析,分支与 Tag 来自同一仓库快照),
CD 改走它 -- 分组渲染见 frontend/src/views/CiBuildView.vue(分支与 Tag 名称可能重名,故用 optgroup).

本文件锁定 CiClient 的取值与回退行为(**不发真实网络**):
  1) 200 直通:{"branches": [...], "tags": [...]},且**不带 ?format=json**、含斜杠的 path 不二次编码;
  2) 老 Glue 无该路由(404/405):回退 /api/build/{path}/branches,形状对齐(tags 为空数组),
     并负缓存一段窗口(窗口内不再白撞一次 404);
  3) 真实故障(5xx / token 失效 / 网络)照旧抛出,绝不伪装成"没有 Tag 的空表".

运行(项目根):
    python -m pytest backend/tests/test_ci_refs.py -q
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.config import settings  # noqa: E402

settings.db_driver = "sqlite"

import requests  # noqa: E402

from backend.services.ci_client import _REFS_MISSING_TTL, CiClient, CiClientError  # noqa: E402

BASE = "http://glue.test:8080"
_PROJECT = "php/devops-glue"  # 含斜杠的 job path:验证 {path} 不做二次编码
_REFS_URL = f"{BASE}/api/git/{_PROJECT}/refs"
_BRANCHES_URL = f"{BASE}/api/build/{_PROJECT}/branches"


class _Recorder:
    """假 _get:按 URL 记录调用并回放预设结果 / 异常(证明"调了哪个端点、几次")."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url not in self.responses:
            raise AssertionError(f"未预设该 URL：{url}")
        outcome = self.responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def hits(self, url: str) -> int:
        return len([u for u, _ in self.calls if u == url])


def _client() -> CiClient:
    return CiClient(BASE, token="dg_test", timeout=5)


class RefsPassthroughTestCase(unittest.TestCase):
    """200:直通返回,且调用形状符合 Glue 的硬约束."""

    def test_returns_branches_and_tags(self):
        rec = _Recorder({_REFS_URL: {"branches": ["main", "dev"], "tags": ["v1.5.0", "v1.5.7"]}})
        with patch.object(CiClient, "_get", side_effect=rec):
            refs = _client().get_refs(_PROJECT)
        self.assertEqual(refs, {"branches": ["main", "dev"], "tags": ["v1.5.0", "v1.5.7"]})
        self.assertEqual(rec.calls[0][0], _REFS_URL, "{path} 含斜杠:URL 必须与 job path 逐字符一致")
        self.assertIsNone(rec.calls[0][1], "refs 端点绝不带 ?format=json（会被包成 {data: ...}）")
        self.assertEqual(rec.hits(_BRANCHES_URL), 0, "refs 可用时不应再打 /branches")

    def test_unwraps_data_wrapper(self):
        """防御:万一被包成 {"data": {...}}(老调用方式残留),也要解出来."""
        rec = _Recorder({_REFS_URL: {"data": {"branches": ["main"], "tags": ["v1"]}}})
        with patch.object(CiClient, "_get", side_effect=rec):
            self.assertEqual(_client().get_refs(_PROJECT), {"branches": ["main"], "tags": ["v1"]})

    def test_normalizes_dirty_payload(self):
        """下拉框不留脏值:空串 / None / 嵌套对象丢弃,标量转字符串(与上游类型漂移解耦)."""
        rec = _Recorder({_REFS_URL: {"branches": [" main ", "", None, {"a": 1}, 3], "tags": "v1"}})
        with patch.object(CiClient, "_get", side_effect=rec):
            self.assertEqual(_client().get_refs(_PROJECT), {"branches": ["main", "3"], "tags": []})

    def test_empty_refs_is_not_an_error(self):
        """Glue 对"该 job 没有仓库映射"返回 200 + 空数组:照原样返回,不当故障."""
        rec = _Recorder({_REFS_URL: {"branches": [], "tags": []}})
        with patch.object(CiClient, "_get", side_effect=rec):
            self.assertEqual(_client().get_refs(_PROJECT), {"branches": [], "tags": []})


class RefsFallbackTestCase(unittest.TestCase):
    """老 Glue(无 /refs 路由):回退 /branches,形状对齐 + 负缓存窗口."""

    def test_404_falls_back_to_branches_with_empty_tags(self):
        rec = _Recorder(
            {
                _REFS_URL: CiClientError("CI API GET 失败 404", status_code=404),
                _BRANCHES_URL: ["main", "dev"],
            }
        )
        with patch.object(CiClient, "_get", side_effect=rec):
            refs = _client().get_refs(_PROJECT)
        self.assertEqual(refs, {"branches": ["main", "dev"], "tags": []})
        self.assertEqual(rec.hits(_BRANCHES_URL), 1)

    def test_405_falls_back_too(self):
        rec = _Recorder(
            {
                _REFS_URL: CiClientError("CI API GET 失败 405", status_code=405),
                _BRANCHES_URL: ["main"],
            }
        )
        with patch.object(CiClient, "_get", side_effect=rec):
            self.assertEqual(_client().get_refs(_PROJECT), {"branches": ["main"], "tags": []})

    def test_fallback_is_remembered_within_window(self):
        """负缓存:老 Glue 上每开一次弹窗都白撞一次 404 太浪费,窗口内直接走 /branches."""
        rec = _Recorder(
            {
                _REFS_URL: CiClientError("CI API GET 失败 404", status_code=404),
                _BRANCHES_URL: ["main"],
            }
        )
        client = _client()
        with patch.object(CiClient, "_get", side_effect=rec):
            for _ in range(3):
                self.assertEqual(client.get_refs(_PROJECT)["branches"], ["main"])
        self.assertGreater(client._refs_missing_until, 0.0, f"负缓存窗口应为 {_REFS_MISSING_TTL}s")
        self.assertEqual(rec.hits(_REFS_URL), 1, "窗口内不应重复打 /refs")
        self.assertEqual(rec.hits(_BRANCHES_URL), 3)

    def test_negative_cache_expires_back_to_refs(self):
        """窗口过期后重新试 /refs:升级 Glue(该端点上线)后自动恢复,无需重启 CD."""
        rec = _Recorder(
            {
                _REFS_URL: {"branches": ["main"], "tags": ["v1.5.0"]},
                _BRANCHES_URL: ["main"],
            }
        )
        client = _client()
        client._refs_missing_until = 0.0  # 首次直通
        with patch.object(CiClient, "_get", side_effect=rec):
            self.assertEqual(client.get_refs(_PROJECT)["tags"], ["v1.5.0"])
            client._refs_missing_until = 0.0  # 等价于"负缓存窗口已过"
            self.assertEqual(client.get_refs(_PROJECT)["tags"], ["v1.5.0"])
        self.assertEqual(rec.hits(_REFS_URL), 2)

    def test_fallback_branches_shape_is_normalized(self):
        """回退时上游若给了包装 / 脏值,同样收敛(下拉框不出现脏值)."""
        rec = _Recorder(
            {
                _REFS_URL: CiClientError("CI API GET 失败 404", status_code=404),
                _BRANCHES_URL: {"data": [" main ", "", 2, None]},
            }
        )
        with patch.object(CiClient, "_get", side_effect=rec):
            self.assertEqual(_client().get_refs(_PROJECT), {"branches": ["main", "2"], "tags": []})


class RefsFailureTestCase(unittest.TestCase):
    """真实故障不许被回退吞掉:5xx / token 失效 / 网络异常一律上抛.

    否则"Glue 挂了"会被渲染成"这个仓库没有 Tag 的空表",把故障藏起来(排障时最怕这种假成功).
    """

    def test_500_propagates_without_fallback(self):
        rec = _Recorder({_REFS_URL: CiClientError("CI API GET 失败 500", status_code=500)})
        with patch.object(CiClient, "_get", side_effect=rec), self.assertRaises(CiClientError):
            _client().get_refs(_PROJECT)
        self.assertEqual(rec.hits(_BRANCHES_URL), 0)

    def test_revoked_token_propagates(self):
        """静态 token 模式下 401 抛的 CiClientError 没有 status_code:不能落进 404/405 的回退分支."""
        rec = _Recorder({_REFS_URL: CiClientError("CI API token 无效（已撤销或过期）")})
        with patch.object(CiClient, "_get", side_effect=rec), self.assertRaises(CiClientError):
            _client().get_refs(_PROJECT)
        self.assertEqual(rec.hits(_BRANCHES_URL), 0)

    def test_network_error_propagates(self):
        rec = _Recorder({_REFS_URL: requests.ConnectionError("glue unreachable")})
        with patch.object(CiClient, "_get", side_effect=rec), self.assertRaises(requests.RequestException):
            _client().get_refs(_PROJECT)
        self.assertEqual(rec.hits(_BRANCHES_URL), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
