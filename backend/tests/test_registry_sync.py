"""镜像同步：Harbor 不可达必须报「连接不可达」，不能显示「同步完成」（回归测试）。

回归点（本次修复）：
    RegistryService.sync_repo 原来用 `except Exception` 把 HarborUnavailableError 一起吞掉并
    `return 0`，于是 sync_all 返回 {ok: true, total: 0, repos: N} → 界面显示「同步完成：0 artifacts」，
    把「连不上 Harbor」伪装成「同步成功但没数据」（日志同样会记「定时同步完成」）。
    现在连接层错误必须上抛，由 sync_all / sync_for_project 转成
    {ok: false, error_key: "errors.harbor_unavailable"}，前端据此显示「连接不可达」。

覆盖：
    - sync_repo：Harbor 不可达 → 抛 HarborUnavailableError（不再返回 0）
    - sync_all：不可达 → ok=False + error_key=errors.harbor_unavailable
    - sync_all：单仓库非连接类异常仍保持容错（ok=True，只记日志）——不因修复而变严
    - sync_all：Harbor 正常时仍然 upsert 成功（happy path 未被破坏）
    - sync_for_project：不可达 → ok=False + error_key
    - 后台线程：ok=False 时记「定时同步中止」，不得记「定时同步完成」

运行（项目根）:
    python -m pytest backend/tests/test_registry_sync.py -q
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

# 允许直接运行时找到 backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 测试一律用临时 SQLite，忽略 .env 中可能配置的 MySQL（必须在导入 Database 前设置）
from backend.config import settings

settings.db_driver = "sqlite"

from backend.database import Database  # noqa: E402
from backend.services import registry_service  # noqa: E402
from backend.services.harbor_client import HarborUnavailableError  # noqa: E402
from backend.services.registry_service import RegistryService  # noqa: E402

CI_REPOS = [{"project": "job-a", "repo": "job-a/app"}]

# Harbor list_artifacts 期望的字段（少一个键 sync_repo 的 INSERT 就会抛 KeyError）
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
        # Database 启动校验要求与 Devops-Glue 共享库（ci_pipeline_artifacts 为标记表）
        self._create_marker(self.db_path)
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn() as conn:
            conn.execute("SELECT 1")
        Database._tables_ensured = True
        self.svc = RegistryService(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _create_marker(path):
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE IF NOT EXISTS ci_pipeline_artifacts (id INTEGER PRIMARY KEY)")
        raw.commit()
        raw.close()

    def _patch_unreachable_harbor(self):
        """让 svc._harbor 访问即抛 HarborUnavailableError（等价于地址错/网络不通/服务未启动）"""
        return patch.object(
            RegistryService,
            "_harbor",
            new_callable=PropertyMock,
            side_effect=HarborUnavailableError("Harbor 连接失败：connection refused"),
        )

    # ── 用例 ──

    def test_sync_repo_reraises_harbor_unavailable(self):
        """连接层不可达必须上抛，不能吞成「0 条」"""
        with self._patch_unreachable_harbor(), self.db.conn() as conn:
            with self.assertRaises(HarborUnavailableError):
                self.svc.sync_repo(conn, "job-a", "job-a/app")

    def test_sync_all_reports_unreachable(self):
        """全量同步：不可达 → ok=False + error_key，前端据此显示「连接不可达」"""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            self._patch_unreachable_harbor(),
        ):
            result = self.svc.sync_all()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_key"], "errors.harbor_unavailable")
        self.assertIn("不可达", result["error"])
        # 关键回归：绝不能出现「成功 + 0 条」这种把故障伪装成完成的结果
        self.assertNotIn("total", result)

    def test_sync_for_project_reports_unreachable(self):
        """单仓库同步：不可达 → ok=False + error_key"""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            self._patch_unreachable_harbor(),
        ):
            result = self.svc.sync_for_project("job-a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_key"], "errors.harbor_unavailable")

    def test_non_connection_error_stays_tolerant(self):
        """非连接类错误（如解析异常）仍按旧行为容错：sync_repo 内部吞掉只记日志，
        不打断其他仓库、也不计入 errors（errors 仅收集 sync_repo 上抛的异常）"""
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(
                RegistryService,
                "_harbor",
                new_callable=PropertyMock,
                side_effect=RuntimeError("unexpected payload"),
            ),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["errors"], [])

    def test_sync_all_imports_artifacts_when_harbor_ok(self):
        """happy path：Harbor 正常时仍然 upsert 入库（确认修复未误伤正常同步）"""
        client = MagicMock()
        client.list_artifacts.return_value = [dict(ARTIFACT)]
        with (
            patch.object(RegistryService, "_get_ci_repos", return_value=CI_REPOS),
            patch.object(RegistryService, "_harbor", new_callable=PropertyMock, return_value=client),
        ):
            result = self.svc.sync_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["errors"], [])
        with self.db.conn() as conn:
            row = conn.execute("SELECT tag FROM cd_registry_artifacts").fetchone()
        self.assertEqual(row["tag"], "v1.0.0")

    def test_background_worker_logs_abort_not_complete(self):
        """后台线程：Harbor 不可达时记「定时同步中止」，不得记「定时同步完成」"""
        stop = MagicMock()
        # while 判空 → 本轮同步 → 跳出（调用顺序：进入循环 / 同步后检查 / 回到 while）
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
