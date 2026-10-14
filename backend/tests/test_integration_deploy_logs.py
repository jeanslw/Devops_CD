"""CI→CD 部署日志只读契约端点(/api/integration/deploy-logs)的契约测试.

锁定对外契约:10 列、筛选(project LIKE / status / deploy_type / date_from / date_to)、
分页(page / per_page ≤100)、可用性走状态码(401 token / 503 闸门).
与前端自用 /api/deploy-logs(session 鉴权、含 output 等前端列)分开.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import HTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402

from backend.config import settings  # noqa: E402

# 测试一律用临时 SQLite,忽略 .env 中可能配置的 MySQL(必须在导入 Database 前设置)
settings.db_driver = "sqlite"

from backend.auth import verify_inbound_api_token  # noqa: E402
from backend.database import Database, invalidate_contract_cache  # noqa: E402
from backend.exceptions import AppException  # noqa: E402
from backend.routers.logs import integration_deploy_logs  # noqa: E402

from cd_contract import enable_cd, reset_contract_stub  # noqa: E402

CONTRACT_COLS = [
    "id",
    "project",
    "tag",
    "image",
    "deploy_type",
    "target",
    "status",
    "triggered_by",
    "deploy_note",
    "created_at",
]

TOKEN = "test-inbound-token"


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class InboundTokenTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = settings.cd_api_token

    def tearDown(self):
        settings.cd_api_token = self._saved

    def test_missing_token_401(self):
        settings.cd_api_token = TOKEN
        with self.assertRaises(HTTPException) as ctx:
            verify_inbound_api_token(None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_token_401(self):
        settings.cd_api_token = TOKEN
        with self.assertRaises(HTTPException) as ctx:
            verify_inbound_api_token(_creds("wrong"))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_empty_configured_token_rejects_all(self):
        settings.cd_api_token = ""
        with self.assertRaises(HTTPException) as ctx:
            verify_inbound_api_token(_creds(TOKEN))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_correct_token_passes(self):
        settings.cd_api_token = TOKEN
        self.assertIsNone(verify_inbound_api_token(_creds(TOKEN)))


class IntegrationDeployLogsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_integration.db")
        enable_cd(self.db_path)
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn() as conn:
            self._seed(conn)
        Database._tables_ensured = True
        settings.cd_api_token = TOKEN

    def tearDown(self):
        Database._tables_ensured = False
        self._tmp.cleanup()

    def _seed(self, conn):
        now = datetime.now()

        def ago(days, hhmmss="10:00:00"):
            return (now - timedelta(days=days)).strftime("%Y-%m-%d") + f" {hhmmss}"

        rows = [
            ("proj-a", "t1", "img1", "ssh", "srv1", "ok", "alice", "note-a", ago(1)),
            ("proj-a", "t2", "img2", "compose", "srv2", "failed", "bob", "note-b", ago(10)),
            ("proj-b", "t3", "img3", "ssh", "srv3", "running", "carol", "note-c", ago(40)),
        ]
        for p, tag, img, dt, target, status, by, note, created in rows:
            conn.execute(
                "INSERT INTO cd_deploy_logs "
                "(project, tag, image, deploy_type, target, status, triggered_by, deploy_note, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (p, tag, img, dt, target, status, by, note, created),
            )

    def _call(self, **overrides):
        kwargs = dict(project="", status="", deploy_type="", date_from="", date_to="", page=1, per_page=20)
        kwargs.update(overrides)
        return integration_deploy_logs(db=self.db, _auth=None, **kwargs)

    def test_returns_contract_columns_only(self):
        data = self._call()
        self.assertEqual(list(data["items"][0].keys()), CONTRACT_COLS)

    def test_project_like_filter(self):
        data = self._call(project="proj-a")
        self.assertEqual(data["total"], 2)
        self.assertTrue(all(i["project"] == "proj-a" for i in data["items"]))

    def test_status_filter(self):
        data = self._call(status="ok")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["status"], "ok")

    def test_deploy_type_filter(self):
        # 显式 date_from 越过默认 30 天窗口,单独验证 deploy_type 过滤
        data = self._call(deploy_type="ssh", date_from="2000-01-01")
        self.assertEqual(data["total"], 2)
        self.assertTrue(all(i["deploy_type"] == "ssh" for i in data["items"]))

    def test_date_range_inclusive(self):
        now = datetime.now()
        date_from = (now - timedelta(days=15)).strftime("%Y-%m-%d")
        date_to = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        data = self._call(date_from=date_from, date_to=date_to)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["tag"], "t2")

    def test_default_window_last_30_days(self):
        # 不传日期:默认只返回近 30 天(40 天前的 t3 应被排除)
        data = self._call()
        tags = [i["tag"] for i in data["items"]]
        self.assertEqual(data["total"], 2)
        self.assertIn("t1", tags)
        self.assertIn("t2", tags)
        self.assertNotIn("t3", tags)

    def test_pagination(self):
        data = self._call(per_page=1, page=2)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["per_page"], 1)
        self.assertEqual(data["total_pages"], 2)
        self.assertEqual(len(data["items"]), 1)

    def test_per_page_capped_at_100(self):
        data = self._call(per_page=1000)
        self.assertEqual(data["per_page"], 100)

    def test_gate_blocks_when_not_enabled(self):
        reset_contract_stub()
        invalidate_contract_cache()
        with self.assertRaises(AppException) as ctx:
            self._call()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
