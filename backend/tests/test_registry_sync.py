"""镜像同步:Harbor 不可达必须报'连接不可达',不能显示'同步完成'(回归测试).

回归点(本次修复):
    RegistryService.sync_repo 原来用 `except Exception` 把 HarborUnavailableError 一起吞掉并
    `return 0`,于是 sync_all 返回 {ok: true, total: 0, repos: N} → 界面显示'同步完成:0 artifacts',
    把'连不上 Harbor'伪装成'同步成功但没数据'(日志同样会记'定时同步完成').
    现在连接层错误必须上抛,由 sync_all / sync_for_project 转成
    {ok: false, error_key: 'errors.harbor_unavailable'},前端据此显示'连接不可达'.

覆盖:
    - sync_repo:Harbor 不可达 → 抛 HarborUnavailableError(不再返回 0)
    - sync_all:不可达 → ok=False + error_key=errors.harbor_unavailable
    - sync_all:账号无查询权限(401/403) → ok=False + error_key=errors.registry_permission_denied
    - _to_mysql_dt:空时间返回 None(写 NULL),MySQL DATETIME 列不接受空串(错误 1292)
    - sync_repo:拉取失败(不可达 / 无权限 / 解析异常)不得先建仓库记录 → 界面不留"0 Tags"占位行
    - sync_all / sync_for_project:清理历史同步失败残留的"0 Tags"占位行,有数据的行不动
    - sync_all:单仓库非连接类异常仍保持容错(ok=True,只记日志)--不因修复而变严
    - sync_all:Harbor 正常时仍然 upsert 成功(happy path 未被破坏)
    - sync_for_project:不可达 → ok=False + error_key
    - 后台线程:ok=False 时记'定时同步中止',不得记'定时同步完成'
    - 漏洞报告/触发扫描(Harbor 与标准 registry:2 的差异):
        * 标准 registry → 明确返回"该仓库类型不支持漏洞扫描"的标识
        * Harbor Tag 不存在 → 返回错误信息,不给空报告
        * Harbor 实时详情取不到 → 回退数据库汇总;取得到 → 优先最新数据
        * Harbor 触发扫描正常工作

运行(项目根):
    python -m pytest backend/tests/test_registry_sync.py -q
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

# 允许直接运行时找到 backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 测试一律用临时 SQLite,忽略 .env 中可能配置的 MySQL(必须在导入 Database 前设置)
from backend.config import settings

settings.db_driver = "sqlite"

from backend.database import Database  # noqa: E402
from backend.services import harbor_client as hc_module  # noqa: E402
from backend.services import registry_provider as rp_module  # noqa: E402
from backend.services import registry_service  # noqa: E402
from backend.services.harbor_client import HarborClient, HarborUnavailableError  # noqa: E402
from backend.services.registry_provider import (  # noqa: E402
    RegistryPermissionDeniedError,
)
from backend.services.registry_service import RegistryService  # noqa: E402

from cd_contract import enable_cd  # noqa: E402

CI_REPOS = [{"project": "job-a", "repo": "job-a/app"}]

# Harbor list_artifacts 期望的字段(少一个键 sync_repo 的 INSERT 就会抛 KeyError)
ARTIFACT = {
    "tag": "v1.0.0",
    "digest": "sha256:abc",
    "size_bytes": 1024,
    "push_time": "2026-07-17T07:17:04.737Z",
    "pull_time": "",
    "scan_status": "Success",
    "scan_severity": "Low",
    "vuln_critical": 0,
    "vuln_high": 0,
    "vuln_medium": 0,
    "vuln_low": 1,
    "vuln_fixable": 0,
}


class RegistrySyncTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_registry.db")
        # 启用 CD(= 运维在 CI 后台点集成服务开关):auto 模式下首次连库自动建表,闸门随之放行
        enable_cd(self.db_path)
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn() as conn:
            conn.execute("SELECT 1")
        Database._tables_ensured = True
        self.svc = RegistryService(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def _patch_unreachable_harbor(self):
        """让 svc._provider 访问即抛 HarborUnavailableError(等价于地址错/网络不通/服务未启动)"""
        return patch.object(
            RegistryService,
            "_provider",
            new_callable=PropertyMock,
            side_effect=HarborUnavailableError("Harbor 连接失败：connection refused"),
        )

    # ── 用例 ──

    def test_sync_repo_reraises_harbor_unavailable(self):
        """连接层不可达必须上抛,不能吞成 '0 条'"""
        with self._patch_unreachable_harbor(), self.db.conn() as conn:
            with self.assertRaises(HarborUnavailableError):
                self.svc.sync_repo(conn, "job-a", "job-a/app")

    def test_sync_all_reports_unreachable(self):
        """全量同步:不可达 → ok=False + error_key,前端据此显示'连接不可达'"""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            self._patch_unreachable_harbor(),
        ):
            result = self.svc.sync_all()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_key"], "errors.harbor_unavailable")
        self.assertIn("不可达", result["error"])
        # 关键回归:不能出现"成功 + 0 条"这种把故障伪装成完成的结果
        self.assertNotIn("total", result)

    def test_sync_for_project_reports_unreachable(self):
        """单仓库同步:不可达 → ok=False + error_key"""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            self._patch_unreachable_harbor(),
        ):
            result = self.svc.sync_for_project("job-a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_key"], "errors.harbor_unavailable")

    def test_sync_all_reports_permission_denied(self):
        """账号无查询权限(401/403) → ok=False + error_key,不得伪装成"同步了 0 条\""""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(
                RegistryService,
                "_provider",
                new_callable=PropertyMock,
                side_effect=RegistryPermissionDeniedError("镜像仓库拒绝访问（HTTP 403）"),
            ),
        ):
            result = self.svc.sync_all()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_key"], "errors.registry_permission_denied")
        self.assertIn("查询权限", result["error"])
        self.assertNotIn("total", result)

    def test_sync_for_project_reports_permission_denied(self):
        """单仓库同步:无查询权限 → ok=False + error_key"""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(
                RegistryService,
                "_provider",
                new_callable=PropertyMock,
                side_effect=RegistryPermissionDeniedError("镜像仓库拒绝访问（HTTP 401）"),
            ),
        ):
            result = self.svc.sync_for_project("job-a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_key"], "errors.registry_permission_denied")

    def test_empty_datetime_converts_to_none(self):
        """空时间 → None(写 NULL):MySQL DATETIME 列不接受空字符串(错误 1292),
        标准 OCI 仓库(registry:2)本就不提供推送/拉取时间"""
        self.assertIsNone(RegistryService._to_mysql_dt(""))
        self.assertIsNone(RegistryService._to_mysql_dt("  "))
        self.assertEqual(RegistryService._to_mysql_dt("2026-07-17T07:17:04.737Z"), "2026-07-17 07:17:04")

    def test_non_connection_error_stays_tolerant(self):
        """非连接类错误(如解析异常)仍按旧行为容错:sync_repo 内部吞掉只记日志,
        不打断其他仓库,也不计入 errors(errors 仅收集 sync_repo 上抛的异常)"""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(
                RegistryService,
                "_provider",
                new_callable=PropertyMock,
                side_effect=RuntimeError("unexpected payload"),
            ),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["errors"], [])

    def test_sync_all_imports_artifacts_when_harbor_ok(self):
        """happy path:Harbor 正常时仍然 upsert 入库(确认修复未误伤正常同步)"""
        client = MagicMock()
        client.list_artifacts.return_value = [dict(ARTIFACT)]
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=client),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["errors"], [])
        with self.db.conn() as conn:
            row = conn.execute("SELECT tag FROM cd_registry_artifacts").fetchone()
        self.assertEqual(row["tag"], "v1.0.0")

    def test_sync_repo_failure_leaves_no_placeholder_row(self):
        """拉取失败不得先建仓库记录:同步失败后界面不能出现"0 Tags"的占位行"""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(
                RegistryService,
                "_provider",
                new_callable=PropertyMock,
                side_effect=RuntimeError("unexpected payload"),
            ),
            self.db.conn() as conn,
        ):
            self.svc.sync_repo(conn, "job-a", "job-a/app")
        with self.db.conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM cd_registry_repositories").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_sync_all_removes_stale_empty_rows(self):
        """全量同步:历史失败残留的"0 Tags"占位行要清掉;本轮拉取仍失败则不再重建"""
        with self.db.conn() as conn:
            conn.execute("INSERT INTO cd_registry_repositories(project_name,repo_name) VALUES ('job-a','job-a/app')")
        failing = MagicMock()
        failing.list_artifacts.side_effect = RuntimeError("unexpected payload")
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=failing),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        with self.db.conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM cd_registry_repositories").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_sync_all_keeps_empty_repo_when_pull_succeeds(self):
        """拉取成功但仓库确实没有 Tag:记录保留(这是真实状态,不是失败残留)"""
        client = MagicMock()
        client.list_artifacts.return_value = []
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=client),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        with self.db.conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM cd_registry_repositories").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_sync_for_project_cleans_placeholder_keeps_real_rows(self):
        """单仓库同步:只清匹配仓库里的占位行;有 Tag 的记录与数据必须原样保留"""
        # 先用 happy path 造一条真实记录(1 Tag)
        client = MagicMock()
        client.list_artifacts.return_value = [dict(ARTIFACT)]
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=client),
        ):
            self.svc.sync_all()
        # 手工补一条同项目的占位行(0 Tag)
        with self.db.conn() as conn:
            conn.execute("INSERT INTO cd_registry_repositories(project_name,repo_name) VALUES ('job-a','job-a/other')")
        # 再跑一次单仓库同步(容忍类失败):占位行应被清掉,真实记录不受影响
        failing = MagicMock()
        failing.list_artifacts.side_effect = RuntimeError("unexpected payload")
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=failing),
        ):
            result = self.svc.sync_for_project("job-a")
        self.assertTrue(result["ok"])
        with self.db.conn() as conn:
            rows = conn.execute("SELECT repo_name FROM cd_registry_repositories ORDER BY repo_name").fetchall()
            arts = conn.execute("SELECT COUNT(*) AS c FROM cd_registry_artifacts").fetchone()["c"]
        self.assertEqual([r["repo_name"] for r in rows], ["job-a/app"])
        self.assertEqual(arts, 1)

    def test_registry_meta_returns_raw_type_hint(self):
        """配置接口下发契约原样类型:取不到时为空串(前端显示「未知」),不默认成 harbor;
        能力开关按契约类型给出(取不到类型时按 harbor 全开,不隐藏列)"""
        with (
            patch.object(registry_service, "registry_type_hint", return_value=""),
            patch.object(
                registry_service,
                "registry_capabilities",
                return_value={"scan": True, "size": True, "push_time": True},
            ),
        ):
            meta = self.svc.get_registry_meta()
        self.assertEqual(meta["registry_type"], "")
        self.assertEqual(meta["capabilities"], {"scan": True, "size": True, "push_time": True})
        with (
            patch.object(registry_service, "registry_type_hint", return_value="registry"),
            patch.object(
                registry_service,
                "registry_capabilities",
                return_value={"scan": False, "size": False, "push_time": False},
            ),
        ):
            meta = self.svc.get_registry_meta()
        self.assertEqual(meta["registry_type"], "registry")
        self.assertEqual(meta["capabilities"], {"scan": False, "size": False, "push_time": False})

    # ── 漏洞报告 / 触发扫描:Harbor 与标准 registry:2 的差异 ──

    def _seed_one_repo_with_artifact(self):
        """用 happy path 同步造一条仓库 + artifact 记录,返回 repo_id"""
        client = MagicMock()
        client.list_artifacts.return_value = [dict(ARTIFACT)]
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=client),
        ):
            self.svc.sync_all()
        with self.db.conn() as conn:
            return conn.execute("SELECT id FROM cd_registry_repositories").fetchone()["id"]

    def test_scan_report_unsupported_for_registry_v2(self):
        """标准 registry(能力开关关闭):明确返回"该仓库类型不支持漏洞扫描"的标识,
        不得落成 500 或伪装成报告(前端隐藏按钮之外的双保险)"""
        with patch.object(
            registry_service, "registry_capabilities", return_value={"scan": False, "size": False, "push_time": False}
        ):
            result = self.svc.get_scan_report(1, "v1.0.0")
        self.assertEqual(result["error_key"], "errors.scan_unsupported")

    def test_scan_report_tag_missing(self):
        """Harbor:Tag 不存在 → 返回错误信息,不给一份看起来正常的空报告"""
        repo_id = self._seed_one_repo_with_artifact()
        with patch.object(
            registry_service, "registry_capabilities", return_value={"scan": True, "size": True, "push_time": True}
        ):
            result = self.svc.get_scan_report(repo_id, "no-such-tag")
        self.assertIn("Tag 不存在", result["error"])

    def test_scan_report_falls_back_to_db_summary(self):
        """Harbor 实时详情取不到(该 Tag 从未扫描)→ 回退数据库汇总,报告仍能显示"""
        repo_id = self._seed_one_repo_with_artifact()
        provider = MagicMock()
        provider.base_url = "http://harbor.local"
        provider.get_scan_report.return_value = None
        with (
            patch.object(
                registry_service, "registry_capabilities", return_value={"scan": True, "size": True, "push_time": True}
            ),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=provider),
        ):
            result = self.svc.get_scan_report(repo_id, "v1.0.0")
        self.assertEqual(result["summary"]["low"], 1)
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["scan_status"], "Success")

    def test_scan_report_prefers_live_data(self):
        """Harbor 实时详情可用 → 优先用最新数据(数据库缓存可能滞后)"""
        repo_id = self._seed_one_repo_with_artifact()
        provider = MagicMock()
        provider.base_url = "http://harbor.local"
        provider.get_scan_report.return_value = {
            "summary": {"critical": 2, "high": 0, "medium": 0, "low": 0},
            "vulnerabilities": [{"id": "CVE-1", "severity": "Critical", "package": "pkg", "version": "1.0"}],
        }
        with (
            patch.object(
                registry_service, "registry_capabilities", return_value={"scan": True, "size": True, "push_time": True}
            ),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=provider),
        ):
            result = self.svc.get_scan_report(repo_id, "v1.0.0")
        self.assertEqual(result["summary"]["critical"], 2)
        self.assertEqual(len(result["vulnerabilities"]), 1)

    def test_trigger_scan_unsupported_for_registry_v2(self):
        """标准 registry:不支持触发扫描 → 明确标识,不落 500"""
        with patch.object(
            registry_service, "registry_capabilities", return_value={"scan": False, "size": False, "push_time": False}
        ):
            result = self.svc.trigger_scan(1, "v1.0.0")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_key"], "errors.scan_unsupported")

    def test_trigger_scan_harbor_ok(self):
        """Harbor:正常触发重新扫描"""
        repo_id = self._seed_one_repo_with_artifact()
        provider = MagicMock()
        with (
            patch.object(
                registry_service, "registry_capabilities", return_value={"scan": True, "size": True, "push_time": True}
            ),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=provider),
        ):
            result = self.svc.trigger_scan(repo_id, "v1.0.0")
        self.assertTrue(result["ok"])
        provider.trigger_scan.assert_called_once_with("job-a/app", "v1.0.0")

    def test_background_worker_logs_abort_not_complete(self):
        """后台线程:Harbor 不可达时记'定时同步中止',不得记'定时同步完成'"""
        stop = MagicMock()
        # while 判空 → 本轮同步 → 跳出(调用顺序:进入循环 / 同步后检查 / 回到 while)
        stop.is_set.side_effect = [False, False, True]
        with (
            patch.object(registry_service, "_sync_stop", stop),
            patch("backend.dlock.acquire", return_value=True),
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            self._patch_unreachable_harbor(),
            self.assertLogs("registry", level="INFO") as logs,
        ):
            registry_service._sync_worker(lambda: self.db, 1)
        joined = "\n".join(logs.output)
        self.assertIn("定时同步中止", joined)
        self.assertNotIn("定时同步完成", joined)

    def test_sync_repo_ingests_registry_v2_shaped_artifacts(self):
        """registry:2 形态的 artifact(digest / size_bytes / push_time 全空)经真实
        RegistryV2Provider 同步入库正确(此前只以 MagicMock 喂 harbor 形态数据)"""
        ci = MagicMock()
        ci.get_json.return_value = ["v1.0.0", "v2.0.0"]
        with patch.object(rp_module, "get_ci_client", return_value=ci):
            provider = rp_module.RegistryV2Provider()
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=provider),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 2)
        with self.db.conn() as conn:
            rows = conn.execute(
                "SELECT tag, digest, size_bytes, push_time FROM cd_registry_artifacts ORDER BY tag"
            ).fetchall()
        self.assertEqual([r["tag"] for r in rows], ["v1.0.0", "v2.0.0"])
        for r in rows:
            # registry:2 不提供这些字段,入库必须为留空值,且时间写 NULL(非 错误 1292 的空串)
            self.assertEqual(r["digest"], "")
            self.assertEqual(r["size_bytes"], 0)
            self.assertIsNone(r["push_time"])

    def test_sync_repo_ingests_registry_detail_shaped_tags(self):
        """Glue 2.11+ registry 类型 tags 端点返回详情对象(digest / size_bytes 由 Glue 解析
        manifest 提供,push_time / pull_time / 扫描字段留空)时,digest 必须完整入库,
        并在 get_artifacts 的 API 响应中原样透出"""
        ci = MagicMock()
        ci.get_json.return_value = [
            {
                "name": "v1.0.0",
                "digest": "sha256:regabc",
                "size_bytes": 4096,
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
        ]
        with patch.object(rp_module, "get_ci_client", return_value=ci):
            provider = rp_module.RegistryV2Provider()
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=provider),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        with self.db.conn() as conn:
            repo_row = conn.execute(
                "SELECT id FROM cd_registry_repositories WHERE project_name=? AND repo_name=?",
                ("job-a", "job-a/app"),
            ).fetchone()
            row = conn.execute("SELECT tag, digest, size_bytes, push_time FROM cd_registry_artifacts").fetchone()
        self.assertIsNotNone(repo_row)
        self.assertEqual(row["tag"], "v1.0.0")
        self.assertEqual(row["digest"], "sha256:regabc", "registry 类型的 digest 必须入库,不得再留空")
        self.assertEqual(row["size_bytes"], 4096)
        self.assertIsNone(row["push_time"], "registry:2 无推送时间,必须写 NULL 而非空串")

        # API 响应透出:get_artifacts 返回的 items 必须带 digest 字段(前端 Digest 列的数据来源)
        artifacts = self.svc.get_artifacts(repo_row["id"])
        self.assertEqual(artifacts["total"], 1)
        self.assertEqual(artifacts["items"][0]["tag"], "v1.0.0")
        self.assertEqual(artifacts["items"][0]["digest"], "sha256:regabc")
        self.assertEqual(artifacts["items"][0]["size_bytes"], 4096)

    def test_sync_repo_ingests_harbor_detail_shaped_tags(self):
        """Glue tags 端点返回详情对象(Harbor v2)时,经真实 HarborClient 映射入库:
        push_time 落成 MySQL DATETIME 而非 NULL,digest/size/扫描概览一并入库"""
        ci = MagicMock()
        ci.get_json.return_value = [
            {
                "name": "v1.0.0",
                "digest": "sha256:abc",
                "size_bytes": 2048,
                "push_time": "2026-07-17T07:17:04.737Z",
                "pull_time": "",
                "scan_status": "Success",
                "scan_severity": "High",
                "vuln_critical": 1,
                "vuln_high": 2,
                "vuln_medium": 0,
                "vuln_low": 0,
                "vuln_fixable": 2,
            }
        ]
        with patch.object(hc_module, "get_ci_client", return_value=ci):
            provider = HarborClient()
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=provider),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        with self.db.conn() as conn:
            row = conn.execute(
                "SELECT tag, digest, size_bytes, push_time, scan_status, scan_severity, "
                "vuln_critical, vuln_high, vuln_fixable FROM cd_registry_artifacts"
            ).fetchone()
        self.assertEqual(row["tag"], "v1.0.0")
        self.assertEqual(row["digest"], "sha256:abc")
        self.assertEqual(row["size_bytes"], 2048)
        self.assertEqual(row["push_time"], "2026-07-17 07:17:04")
        self.assertEqual(row["scan_status"], "Success")
        self.assertEqual(row["scan_severity"], "High")
        self.assertEqual(row["vuln_critical"], 1)
        self.assertEqual(row["vuln_high"], 2)
        self.assertEqual(row["vuln_fixable"], 2)

    def test_sync_repo_ingests_harbor_legacy_string_tags(self):
        """Glue tags 端点返回纯名称(Harbor v1 / 旧版 Glue)时保持兼容:
        tag 正常入库,详情字段留空,push_time 写 NULL"""
        ci = MagicMock()
        ci.get_json.return_value = ["v2.0.0"]
        with patch.object(hc_module, "get_ci_client", return_value=ci):
            provider = HarborClient()
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_provider", new_callable=PropertyMock, return_value=provider),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        with self.db.conn() as conn:
            row = conn.execute("SELECT tag, digest, size_bytes, push_time FROM cd_registry_artifacts").fetchone()
        self.assertEqual(row["tag"], "v2.0.0")
        self.assertEqual(row["digest"], "")
        self.assertEqual(row["size_bytes"], 0)
        self.assertIsNone(row["push_time"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
