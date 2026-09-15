"""审批三段式流程测试（v1.5.3：申请 → 批准仅放行 → 申请人本人手动执行）。

用真实临时 SQLite 库验证状态机与 SQL（建表/迁移/回填），外部身份重建与
执行器用 mock 隔离，不依赖 SSH/K8s/CI。

覆盖：
  - 批准只放行不触发部署；四眼原则（自己不能批自己，super_admin 也不行）
  - 撤销：pending/approved 可撤，deploying 不可撤
  - 执行权限矩阵：仅 requester 本人（super_admin 不豁免）、状态门、快照权限复核
  - 一次性/并发领取（approved→deploying 原子）
  - run_approval：ok 落 deployed+deploy_id；busy 退回 approved；failed/cancelled 终态
  - active 列表筛选（与 status 互斥）
  - approval_id 落部署记录 + list_logs 批量挂载审批信息
  - 旧库迁移补列 + 历史回填幂等

运行（项目根）:
    python -m pytest backend/tests/test_approval_flow.py -q
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

# 允许直接运行时找到 backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 测试一律用临时 SQLite，忽略 .env 中可能配置的 MySQL（必须在导入 Database 前设置）
from backend.config import settings

settings.db_driver = "sqlite"

from backend.database import Database  # noqa: E402
from backend.deploy_run import (  # noqa: E402
    claim_pending_deploy_record,
    create_pending_deploy_record,
    start_deploy_record,
)
from backend.exceptions import AppException, ConflictError, ValidationError  # noqa: E402
from backend.services import approval_service as svc  # noqa: E402
from backend.services.deploy_service import DeployService  # noqa: E402


def make_user(username="alice", role="deployer", perms=None):
    return {"username": username, "role": role, "permissions": perms or []}


class ApprovalFlowTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_approval.db")
        # Database 启动校验要求与 Devops-Glue 共享库（ci_pipeline_artifacts 为标记表）
        self._create_marker(self.db_path)
        # 每个临时库都是"全新库"，强制重新建表（类变量在测试间共享）
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn() as conn:
            conn.execute("SELECT 1")
        Database._tables_ensured = True

    @staticmethod
    def _create_marker(path):
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE IF NOT EXISTS ci_pipeline_artifacts (id INTEGER PRIMARY KEY)")
        raw.commit()
        raw.close()

    def tearDown(self):
        self._tmp.cleanup()

    def add_approval(
        self,
        status=svc.PENDING,
        requester="alice",
        project="proj-a",
        deploy_type="ssh",
        params=None,
        approver="",
        approved_at="",
        deploy_id=0,
        scheduled_at="",
    ):
        params = {"deploy_type": deploy_type} if params is None else params
        with self.db.conn() as conn:
            cur = conn.execute(
                "INSERT INTO cd_approvals "
                "(project, tag, image, deploy_type, envs, params_json, status, requester, "
                "approver, approved_at, deploy_id, scheduled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project,
                    "v1",
                    "",
                    deploy_type,
                    "",
                    json.dumps(params, ensure_ascii=False),
                    status,
                    requester,
                    approver,
                    approved_at,
                    deploy_id,
                    scheduled_at,
                ),
            )
            return cur.lastrowid

    def get_approval(self, aid) -> dict:
        """按 id 取审批单；不存在时断言失败（测试内所有调用点均假定单据存在）。"""
        row = svc._get(self.db, aid)
        assert row is not None, f"approval {aid} not found"
        return dict(row)

    def set_status(self, aid, status):
        with self.db.conn() as conn:
            conn.execute("UPDATE cd_approvals SET status=? WHERE id=?", (status, aid))


# ─────────────────────────────────────────────────────────────
# 批准：仅放行，不执行
# ─────────────────────────────────────────────────────────────
class TestApproveReleasesOnly(ApprovalFlowTestCase):
    def test_approve_marks_approved_without_any_execution(self):
        aid = self.add_approval(status=svc.PENDING)
        approver = make_user("bob", perms=["cd.deploy.approve"])

        with (
            patch.object(svc, "execute_from_params") as mk_exec,
            patch.object(svc, "load_user_context") as mk_ctx,
            patch.object(svc, "notify_approval"),
        ):
            result = svc.approve(self.db, aid, approver)

        self.assertTrue(result["success"])
        row = self.get_approval(aid)
        self.assertEqual(row["status"], svc.APPROVED)
        self.assertEqual(row["approver"], "bob")
        self.assertTrue(row["approved_at"])
        # 批准路径绝不能触发部署/重建执行身份
        mk_exec.assert_not_called()
        mk_ctx.assert_not_called()

    def test_four_eyes_forbids_self_approval_even_super_admin(self):
        aid = self.add_approval(status=svc.PENDING, requester="alice")
        for actor in (
            make_user("alice", perms=["cd.deploy.approve"]),
            make_user("alice", role="super_admin"),
        ):
            with self.assertRaises(AppException) as ctx:
                svc.approve(self.db, aid, actor)
            self.assertEqual(ctx.exception.error_key, "errors.self_approval_forbidden")
        # 单据始终停留在 pending
        self.assertEqual(self.get_approval(aid)["status"], svc.PENDING)

    def test_user_without_approve_perm_gets_localized_exception(self):
        # 无审批权限（非审批人/无权限/角色不匹配/非超管）→ AppException + error_key（前端可 i18n）
        aid = self.add_approval(status=svc.PENDING, requester="alice")
        with self.assertRaises(AppException) as ctx:
            svc.approve(self.db, aid, make_user("carol"))
        self.assertEqual(ctx.exception.error_key, "errors.approve_perm_denied")
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(AppException) as ctx2:
            svc.reject(self.db, aid, make_user("carol"), note="no")
        self.assertEqual(ctx2.exception.error_key, "errors.approve_perm_denied")
        # 单据未被改动
        self.assertEqual(self.get_approval(aid)["status"], svc.PENDING)


# ─────────────────────────────────────────────────────────────
# 撤销
# ─────────────────────────────────────────────────────────────
class TestCancel(ApprovalFlowTestCase):
    def test_owner_can_cancel_pending_and_approved(self):
        a1 = self.add_approval(status=svc.PENDING)
        self.assertTrue(svc.cancel(self.db, a1, make_user("alice"))["success"])
        self.assertEqual(self.get_approval(a1)["status"], svc.CANCELLED)

        a2 = self.add_approval(status=svc.APPROVED)
        self.assertTrue(svc.cancel(self.db, a2, make_user("alice"))["success"])
        self.assertEqual(self.get_approval(a2)["status"], svc.CANCELLED)

    def test_super_admin_can_cancel_others_request(self):
        aid = self.add_approval(status=svc.PENDING, requester="alice")
        self.assertTrue(svc.cancel(self.db, aid, make_user("root", role="super_admin"))["success"])

    def test_other_user_cannot_cancel(self):
        aid = self.add_approval(status=svc.PENDING)
        with self.assertRaises(AppException) as ctx:
            svc.cancel(self.db, aid, make_user("bob"))
        self.assertEqual(ctx.exception.error_key, "errors.approval_cancel_perm")

    def _pending_log_status(self, aid):
        with self.db.conn() as conn:
            rows = conn.execute("SELECT status FROM cd_deploy_logs WHERE approval_id=?", (aid,)).fetchall()
        return [r["status"] for r in rows]

    def test_cancel_marks_pending_log_cancelled(self):
        aid = self.add_approval(status=svc.PENDING)
        create_pending_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-a",
            tag="v1",
            image="",
            triggered_by="alice",
            approval_id=aid,
        )
        self.assertTrue(svc.cancel(self.db, aid, make_user("alice"))["success"])
        self.assertEqual(self._pending_log_status(aid), ["cancelled"])

    def test_reject_marks_pending_log_rejected(self):
        aid = self.add_approval(status=svc.PENDING)
        create_pending_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-a",
            tag="v1",
            image="",
            triggered_by="alice",
            approval_id=aid,
        )
        svc.reject(self.db, aid, make_user("bob", perms=["cd.deploy.approve"]), note="no")
        self.assertEqual(self._pending_log_status(aid), ["rejected"])

    def test_cancel_after_deploying_starts_fails(self):
        aid = self.add_approval(status=svc.DEPLOYING)
        result = svc.cancel(self.db, aid, make_user("alice"))
        self.assertFalse(result["success"])
        self.assertEqual(self.get_approval(aid)["status"], svc.DEPLOYING)


# ─────────────────────────────────────────────────────────────
# 执行权限矩阵
# ─────────────────────────────────────────────────────────────
class TestExecutePermission(ApprovalFlowTestCase):
    def test_can_execute_flags(self):
        approved = {"status": svc.APPROVED, "requester": "alice"}
        pending = {"status": svc.PENDING, "requester": "alice"}
        self.assertTrue(svc.can_execute(approved, make_user("alice")))
        self.assertFalse(svc.can_execute(approved, make_user("bob")))
        # super_admin 不是申请人也不能代执行
        self.assertFalse(svc.can_execute(approved, make_user("root", role="super_admin")))
        self.assertFalse(svc.can_execute(pending, make_user("alice")))

    def test_claim_by_non_requester_forbidden_even_super_admin(self):
        aid = self.add_approval(status=svc.APPROVED, requester="alice")
        for actor in (make_user("bob", perms=["cd.deploy.approve"]), make_user("root", role="super_admin")):
            with patch.object(svc, "load_user_context") as mk_ctx:
                with self.assertRaises(AppException) as ctx:
                    svc.claim_for_execution(self.db, aid, actor)
                self.assertEqual(ctx.exception.error_key, "errors.approval_execute_self_only")
                mk_ctx.assert_not_called()
        self.assertEqual(self.get_approval(aid)["status"], svc.APPROVED)

    def test_claim_requires_approved_status(self):
        aid = self.add_approval(status=svc.PENDING)
        with self.assertRaises(ConflictError) as ctx:
            svc.claim_for_execution(self.db, aid, make_user("alice"))
        self.assertEqual(ctx.exception.error_key, "errors.approval_not_executable")

        deployed = self.add_approval(status=svc.DEPLOYED)
        with self.assertRaises(ConflictError):
            svc.claim_for_execution(self.db, deployed, make_user("alice"))

    def test_claim_rechecks_current_permission_normal_deploy(self):
        # 申请人当前权限已被收回 → 拒绝领取，单据保持 approved（统一 AppException/error_key，
        # 同步接口与 SSE ERROR 事件均可消费）
        aid = self.add_approval(status=svc.APPROVED, params={"deploy_type": "ssh"})
        with patch.object(svc, "load_user_context", return_value=make_user("alice")):
            with self.assertRaises(AppException) as ctx:
                svc.claim_for_execution(self.db, aid, make_user("alice"))
            self.assertEqual(ctx.exception.status_code, 403)
            self.assertEqual(ctx.exception.error_key, "errors.approval_execute_perm")
        self.assertEqual(self.get_approval(aid)["status"], svc.APPROVED)

    def test_claim_rechecks_rollback_requires_manage_perm(self):
        aid = self.add_approval(status=svc.APPROVED, params={"_rollback": True})
        # 仅有普通部署子权限不够，回滚快照要求 cd.deploy-manage
        with patch.object(svc, "load_user_context", return_value=make_user("alice", perms=["cd.deploy.single"])):
            with self.assertRaises(AppException) as ctx:
                svc.claim_for_execution(self.db, aid, make_user("alice"))
            self.assertEqual(ctx.exception.error_key, "errors.approval_execute_perm")

        with patch.object(svc, "load_user_context", return_value=make_user("alice", perms=["cd.deploy-manage"])):
            approval, exec_user = svc.claim_for_execution(self.db, aid, make_user("alice"))
            self.assertEqual(approval["status"], svc.DEPLOYING)
            self.assertEqual(exec_user["username"], "alice")


# ─────────────────────────────────────────────────────────────
# 领取一次性 + run_approval 状态推进
# ─────────────────────────────────────────────────────────────
class TestClaimAndRun(ApprovalFlowTestCase):
    def _claim(self, aid, perms=("cd.deploy.single",)):
        with patch.object(svc, "load_user_context", return_value=make_user("alice", perms=list(perms))):
            return svc.claim_for_execution(self.db, aid, make_user("alice"))

    def test_claim_is_one_shot_concurrent_second_loses(self):
        aid = self.add_approval(status=svc.APPROVED)
        approval, _ = self._claim(aid)
        self.assertEqual(approval["status"], svc.DEPLOYING)
        # 重复/并发领取：原子迁移失败 → 409，单据不被二次消费
        with self.assertRaises(ConflictError):
            self._claim(aid)

    def test_run_ok_marks_deployed_and_propagates_approval_id(self):
        aid = self.add_approval(status=svc.APPROVED, params={"deploy_type": "ssh"})
        _, exec_user = self._claim(aid)

        with patch.object(
            svc,
            "execute_from_params",
            return_value={"status": "ok", "deploy_id": 555, "output": "done"},
        ) as mk_exec:
            result = svc.run_approval(self.db, aid, exec_user)

        self.assertEqual(result["status"], "ok")
        row = self.get_approval(aid)
        self.assertEqual(row["status"], svc.DEPLOYED)
        self.assertEqual(row["deploy_id"], 555)
        # 审批单 id 必须透传到部署记录写入链路；快照内的临时键已剥离
        kwargs = mk_exec.call_args.kwargs
        self.assertEqual(kwargs["approval_id"], aid)
        self.assertFalse(kwargs["rollback"])

    def test_run_busy_rolls_back_to_approved(self):
        aid = self.add_approval(status=svc.APPROVED)
        # busy 可能发生在行已被领取之后（防御路径）：行退回 pending 并释放锁
        log_id = create_pending_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-a",
            tag="v1",
            image="",
            triggered_by="alice",
            approval_id=aid,
        )
        claimed_id = claim_pending_deploy_record(self.db, project="proj-a", approval_id=aid)
        self.assertEqual(claimed_id, log_id)
        _, exec_user = self._claim(aid)

        with patch.object(
            svc,
            "execute_from_params",
            return_value={"status": "busy", "deploy_id": 0, "output": "project locked"},
        ):
            result = svc.run_approval(self.db, aid, exec_user)

        self.assertEqual(result["status"], "busy")
        # 单据不消耗：退回 approved，可再次领取重试
        self.assertEqual(self.get_approval(aid)["status"], svc.APPROVED)
        approval2, _ = self._claim(aid)
        self.assertEqual(approval2["status"], svc.DEPLOYING)
        # 日志行退回 pending、锁释放，可再次领取
        with self.db.conn() as conn:
            log_row = conn.execute("SELECT status, lock_key FROM cd_deploy_logs WHERE id=?", (log_id,)).fetchone()
        assert log_row is not None
        self.assertEqual(log_row["status"], "pending")
        self.assertIsNone(log_row["lock_key"])
        self.assertEqual(claim_pending_deploy_record(self.db, project="proj-a", approval_id=aid), log_id)

    def test_run_failed_and_cancelled_transitions(self):
        aid_f = self.add_approval(status=svc.APPROVED)
        self.set_status(aid_f, svc.DEPLOYING)
        with patch.object(
            svc,
            "execute_from_params",
            return_value={"status": "failed", "deploy_id": 77, "output": "boom"},
        ):
            svc.run_approval(self.db, aid_f, make_user("alice"))
        row = self.get_approval(aid_f)
        self.assertEqual(row["status"], svc.FAILED)
        self.assertEqual(row["deploy_id"], 77)

        aid_c = self.add_approval(status=svc.APPROVED)
        self.set_status(aid_c, svc.DEPLOYING)
        with patch.object(
            svc,
            "execute_from_params",
            return_value={"status": "cancelled", "deploy_id": 0, "output": "cancel"},
        ):
            svc.run_approval(self.db, aid_c, make_user("alice"))
        self.assertEqual(self.get_approval(aid_c)["status"], svc.CANCELLED)

    def test_run_rollback_snapshot_passes_rollback_flag(self):
        aid = self.add_approval(status=svc.APPROVED, params={"_rollback": True, "deploy_type": "ssh"})
        with patch.object(svc, "load_user_context", return_value=make_user("alice", perms=["cd.deploy-manage"])):
            _, exec_user = svc.claim_for_execution(self.db, aid, make_user("alice"))
        with patch.object(
            svc,
            "execute_from_params",
            return_value={"status": "ok", "deploy_id": 88, "output": "rolled"},
        ) as mk_exec:
            svc.run_approval(self.db, aid, exec_user)
        self.assertTrue(mk_exec.call_args.kwargs["rollback"])
        self.assertEqual(mk_exec.call_args.kwargs["approval_id"], aid)

    def test_run_on_wrong_status_does_not_execute(self):
        aid = self.add_approval(status=svc.APPROVED)  # 未领取
        with patch.object(svc, "execute_from_params") as mk_exec:
            result = svc.run_approval(self.db, aid, make_user("alice"))
        self.assertEqual(result["status"], "failed")
        mk_exec.assert_not_called()


# ─────────────────────────────────────────────────────────────
# 活跃单筛选
# ─────────────────────────────────────────────────────────────
class TestActiveFilter(ApprovalFlowTestCase):
    def test_active_returns_only_non_terminal(self):
        for st in (svc.PENDING, svc.APPROVED, svc.DEPLOYING, svc.DEPLOYED, svc.REJECTED):
            self.add_approval(status=st)

        result = svc.list_approvals(self.db, active=True)
        statuses = {i["status"] for i in result["items"]}
        self.assertEqual(statuses, {svc.PENDING, svc.APPROVED, svc.DEPLOYING})
        self.assertEqual(result["total"], 3)

    def test_active_takes_priority_over_status(self):
        self.add_approval(status=svc.PENDING)
        self.add_approval(status=svc.DEPLOYED)
        # active 与 status 同时传入：active 优先（部署页恢复场景不允许被 status 收窄）
        result = svc.list_approvals(self.db, active=True, status="deployed")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["status"], svc.PENDING)

    def test_active_filter_by_requester_and_project(self):
        self.add_approval(status=svc.PENDING, requester="alice", project="proj-a")
        self.add_approval(status=svc.APPROVED, requester="bob", project="proj-a")
        self.add_approval(status=svc.APPROVED, requester="alice", project="proj-b")

        self.assertEqual(svc.list_approvals(self.db, active=True, requester="alice")["total"], 2)
        self.assertEqual(svc.list_approvals(self.db, active=True, requester="alice", project="proj-a")["total"], 1)
        self.assertEqual(svc.list_approvals(self.db, active=True, requester="nobody")["total"], 0)

    def test_active_filter_by_deploy_kind_separates_pages(self):
        # 同一项目：Docker(compose) 申请不应串显到 K8s 页，反之亦然
        self.add_approval(status=svc.PENDING, project="proj-a", deploy_type="compose")
        self.add_approval(status=svc.APPROVED, project="proj-a", deploy_type="ssh")
        self.add_approval(status=svc.PENDING, project="proj-a", deploy_type="k8s/kubectl")
        self.add_approval(status=svc.APPROVED, project="proj-a", deploy_type="k8s/helm")

        compose = svc.list_approvals(self.db, active=True, project="proj-a", deploy_kind="compose")
        self.assertEqual(compose["total"], 1)
        self.assertEqual(compose["items"][0]["deploy_type"], "compose")

        ssh = svc.list_approvals(self.db, active=True, project="proj-a", deploy_kind="ssh")
        self.assertEqual(ssh["total"], 1)
        self.assertEqual(ssh["items"][0]["deploy_type"], "ssh")

        k8s = svc.list_approvals(self.db, active=True, project="proj-a", deploy_kind="k8s")
        self.assertEqual(k8s["total"], 2)
        self.assertEqual({i["deploy_type"] for i in k8s["items"]}, {"k8s/kubectl", "k8s/helm"})

        # 大小写不敏感；未知 kind 不过滤（兼容旧调用方）
        self.assertEqual(svc.list_approvals(self.db, active=True, deploy_kind="K8S")["total"], 2)
        self.assertEqual(svc.list_approvals(self.db, active=True, deploy_kind="weird")["total"], 4)

    def test_active_prefers_actionable_over_newest(self):
        # 同项目两条活跃单：较早的已批准（可执行）+ 较新的待审批。
        # 部署页恢复进度只取 1 条时必须返回已批准单，不能被新的待审批单挡住。
        approved_id = self.add_approval(status=svc.APPROVED, project="proj-a")
        pending_id = self.add_approval(status=svc.PENDING, project="proj-a")
        self.assertGreater(pending_id, approved_id)

        first = svc.list_approvals(self.db, active=True, project="proj-a", page_size=1)["items"][0]
        self.assertEqual(first["id"], approved_id)
        self.assertEqual(first["status"], svc.APPROVED)

        # 执行中的单优先级最高（比 approved 更新也排第一）
        deploying_id = self.add_approval(status=svc.DEPLOYING, project="proj-a")
        first = svc.list_approvals(self.db, active=True, project="proj-a", page_size=1)["items"][0]
        self.assertEqual(first["id"], deploying_id)

        # 非 active 的普通列表仍严格按新到旧
        normal = svc.list_approvals(self.db, project="proj-a", page_size=1)["items"][0]
        self.assertEqual(normal["id"], deploying_id)


# ─────────────────────────────────────────────────────────────
# approval_id 落部署记录 + list_logs 挂载
# ─────────────────────────────────────────────────────────────
class TestApprovalIdOnDeployLog(ApprovalFlowTestCase):
    def test_start_deploy_record_persists_approval_id_and_list_logs_enriches(self):
        aid = self.add_approval(
            status=svc.DEPLOYED, requester="alice", approver="bob", approved_at="2026-09-14 10:00:00"
        )
        log_id = start_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-a",
            tag="v1",
            image="img:v1",
            triggered_by="alice",
            approval_id=aid,
        )
        self.assertTrue(log_id > 0)
        with self.db.conn() as conn:
            row = conn.execute("SELECT approval_id FROM cd_deploy_logs WHERE id=?", (log_id,)).fetchone()
        self.assertEqual(row["approval_id"], aid)

        items = DeployService(self.db).list_logs(project="proj-a")["items"]
        mine = next(i for i in items if i["id"] == log_id)
        self.assertEqual(
            mine["approval"],
            {"approver": "bob", "approved_at": "2026-09-14 10:00:00", "status": "deployed"},
        )

    def test_list_logs_without_approval_has_no_approval_key(self):
        # 不同 project 避免 running 锁唯一冲突
        start_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-plain",
            tag="v2",
            image="img:v2",
            triggered_by="alice",
        )
        items = DeployService(self.db).list_logs(project="proj-plain")["items"]
        self.assertEqual(len(items), 1)
        self.assertNotIn("approval", items[0])
        self.assertEqual(items[0]["approval_id"], 0)


# ─────────────────────────────────────────────────────────────
# 旧库迁移：补 approval_id 列 + 历史回填，幂等
# ─────────────────────────────────────────────────────────────
class TestLegacyMigration(ApprovalFlowTestCase):
    def _build_legacy_db(self, path):
        """模拟 v1.5 旧库：cd_deploy_logs 缺 approval_id 列。"""
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE cd_deploy_logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, project VARCHAR(255), tag VARCHAR(255), "
            "image VARCHAR(512), deploy_type VARCHAR(32), target VARCHAR(255), status VARCHAR(32), "
            "output TEXT, created_at TEXT)"
        )
        raw.execute(
            "CREATE TABLE cd_approvals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, project VARCHAR(255) NOT NULL, tag VARCHAR(255), "
            "image VARCHAR(512), deploy_type VARCHAR(32), envs VARCHAR(255), params_json TEXT, "
            "status VARCHAR(16), requester VARCHAR(64), approver VARCHAR(64), approve_note VARCHAR(512), "
            "deploy_id INTEGER DEFAULT 0, created_at TEXT, approved_at TEXT, updated_at TEXT)"
        )
        raw.commit()
        raw.close()
        # 共享库标记表（启动校验）
        marker = sqlite3.connect(path)
        marker.execute("CREATE TABLE IF NOT EXISTS ci_pipeline_artifacts (id INTEGER PRIMARY KEY)")
        marker.commit()
        marker.close()

    def _columns(self, path, table):
        raw = sqlite3.connect(path)
        cols = [r[1] for r in raw.execute(f"PRAGMA table_info({table})").fetchall()]
        raw.close()
        return cols

    def test_legacy_table_gets_column_idempotently_and_backfills(self):
        legacy_path = os.path.join(self._tmp.name, "legacy.db")
        self._build_legacy_db(legacy_path)
        self.assertNotIn("approval_id", self._columns(legacy_path, "cd_deploy_logs"))

        # 第一次启动迁移：补列 + 建索引
        Database._tables_ensured = False
        db = Database(legacy_path)
        with db.conn():
            pass
        self.assertIn("approval_id", self._columns(legacy_path, "cd_deploy_logs"))

        # 历史数据：旧审批单已回填 deploy_id，对应部署记录 approval_id 仍为 0
        raw = sqlite3.connect(legacy_path)
        raw.execute("INSERT INTO cd_deploy_logs (id, project, status) VALUES (7, 'old-proj', 'ok')")
        raw.execute(
            "INSERT INTO cd_approvals (id, project, status, requester, deploy_id) "
            "VALUES (99, 'old-proj', 'deployed', 'alice', 7)"
        )
        raw.commit()
        raw.close()

        # 第二次启动迁移：执行历史回填（幂等，不报错）
        conn = db._connect_sqlite()
        db._ensure_cd_tables(conn)
        conn.close()

        raw = sqlite3.connect(legacy_path)
        backfilled = raw.execute("SELECT approval_id FROM cd_deploy_logs WHERE id=7").fetchone()[0]
        raw.close()
        self.assertEqual(backfilled, 99)

        # 第三次迁移仍幂等
        conn = db._connect_sqlite()
        db._ensure_cd_tables(conn)
        conn.close()


# ─────────────────────────────────────────────────────────────
# 申请闸门：只建 pending 单，不执行、不写部署记录
# ─────────────────────────────────────────────────────────────
class TestGateCreatesOnlyRequest(ApprovalFlowTestCase):
    def _add_rule(self, project="proj-a", require_rollback_approval=1):
        with self.db.conn() as conn:
            conn.execute(
                "INSERT INTO cd_approval_rules "
                "(project, enabled, require_envs, approver_role, approvers, notify_bot_id, "
                "require_rollback_approval) VALUES (?,1,'','cd_admin','',0,?)",
                (project, require_rollback_approval),
            )

    def _logs_count(self):
        with self.db.conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM cd_deploy_logs").fetchone()["c"]

    def _logs_by_approval(self, aid):
        with self.db.conn() as conn:
            rows = conn.execute("SELECT * FROM cd_deploy_logs WHERE approval_id=? ORDER BY id", (aid,)).fetchall()
        return [dict(r) for r in rows]

    def test_gate_creates_pending_approval_and_pending_log_without_execution(self):
        self._add_rule()
        with (
            patch.object(svc, "_notify_request"),
            patch.object(svc, "execute_from_params") as mk_exec,
        ):
            result = svc.gate_deploy(
                self.db,
                project="proj-a",
                tag="v9",
                deploy_type="ssh",
                server_ids="",
                params={"deploy_type": "ssh", "deploy_note": "note-x"},
                requester="alice",
            )
        assert result is not None

        self.assertTrue(result["pending"])
        aid = result["approval_id"]
        row = self.get_approval(aid)
        self.assertEqual(row["status"], svc.PENDING)
        self.assertEqual(row["requester"], "alice")
        self.assertEqual(row["deploy_id"], 0)
        # 同步生成一条 pending 部署记录：与审批单 1:1 关联、不占项目锁、不执行远端动作
        logs = self._logs_by_approval(aid)
        self.assertEqual(len(logs), 1)
        log = logs[0]
        self.assertEqual(log["status"], "pending")
        self.assertIsNone(log["lock_key"])
        self.assertEqual(log["approval_id"], aid)
        self.assertEqual(log["triggered_by"], "alice")
        self.assertEqual(log["deploy_note"], "note-x")
        self.assertEqual(self._logs_count(), 1)
        mk_exec.assert_not_called()

    def test_gate_none_without_rule_is_direct_pass(self):
        with (
            patch.object(svc, "_notify_request") as mk_notify,
            patch.object(svc, "create_approval") as mk_create,
        ):
            result = svc.gate_deploy(
                self.db,
                project="proj-free",
                tag="v1",
                deploy_type="ssh",
                server_ids="",
                params={"deploy_type": "ssh"},
                requester="alice",
            )
        self.assertIsNone(result)
        mk_notify.assert_not_called()
        mk_create.assert_not_called()

    def test_rollback_respects_rule_switch(self):
        # require_rollback_approval=0：回滚直通
        self._add_rule(require_rollback_approval=0)
        self.assertIsNone(
            svc.gate_deploy(
                self.db,
                project="proj-a",
                tag="v1",
                deploy_type="ssh",
                server_ids="",
                params={"_rollback": True},
                requester="alice",
                for_rollback=True,
            )
        )
        # 开关打开：回滚也只产生申请
        with self.db.conn() as conn:
            conn.execute("UPDATE cd_approval_rules SET require_rollback_approval=1")
        with patch.object(svc, "_notify_request"):
            result = svc.gate_deploy(
                self.db,
                project="proj-a",
                tag="v1",
                deploy_type="ssh",
                server_ids="",
                params={"_rollback": True},
                requester="alice",
                for_rollback=True,
            )
        assert result is not None
        self.assertTrue(result["pending"])
        self.assertTrue(self.get_approval(result["approval_id"])["status"], svc.PENDING)


# ─────────────────────────────────────────────────────────────
# 启动恢复：deploying 僵尸单按关联部署记录实际状态收敛
# ─────────────────────────────────────────────────────────────
class TestStartupRecovery(ApprovalFlowTestCase):
    def test_deploying_reset_to_approved_terminal_untouched(self):
        stuck = self.add_approval(status=svc.DEPLOYING)
        deployed = self.add_approval(status=svc.DEPLOYED, project="proj-b")
        pending = self.add_approval(status=svc.PENDING, project="proj-c")

        svc.recover_on_startup(self.db)

        self.assertEqual(self.get_approval(stuck)["status"], svc.APPROVED)
        # 终态/待审批单不受影响
        self.assertEqual(self.get_approval(deployed)["status"], svc.DEPLOYED)
        self.assertEqual(self.get_approval(pending)["status"], svc.PENDING)

    def test_recovery_resets_approval_interrupted_log_back_to_pending(self):
        from backend.deploy_run import recover_stale_running

        aid = self.add_approval(status=svc.DEPLOYING)
        log_id = create_pending_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-a",
            tag="v1",
            image="",
            triggered_by="alice",
            approval_id=aid,
        )
        claim_pending_deploy_record(self.db, project="proj-a", approval_id=aid)
        # 与审批无关的 running 行（旧直连部署）也会被标记 interrupted，但不应回 pending
        plain_id = start_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-z",
            tag="v1",
            image="",
            triggered_by="root",
        )
        # v1.5.3 多副本隔离：claim/insert 写入了本实例 runner + 新鲜心跳，
        # 手动把两条 running 行改为心跳过期，模拟进程已崩溃/被硬杀（否则不会被恢复）
        with self.db.conn() as conn:
            conn.execute("UPDATE cd_deploy_logs SET heartbeat_at='0' WHERE id IN (?, ?)", (log_id, plain_id))
        # 只恢复心跳过期的 running 行（其他进程心跳新鲜的绝不动）
        self.assertEqual(recover_stale_running(self.db), 2)
        # 再次执行：心跳已清（行已是 interrupted），无 running 行可恢复
        self.assertEqual(recover_stale_running(self.db), 0)
        svc.recover_on_startup(self.db)

        with self.db.conn() as conn:
            rows = {
                r["id"]: (r["status"], r["lock_key"])
                for r in conn.execute("SELECT id, status, lock_key FROM cd_deploy_logs").fetchall()
            }
        # 审批关联行回到 pending 且锁释放，等待申请人重新执行领取
        self.assertEqual(rows[log_id], ("pending", None))
        # 无 approval_id 的普通 interrupted 行保持 interrupted
        self.assertEqual(rows[plain_id][0], "interrupted")
        self.assertEqual(self.get_approval(aid)["status"], svc.APPROVED)

    def test_recovery_skips_fresh_heartbeat_from_other_instance(self):
        """v1.5.3 多副本/多 worker 隔离：其他进程心跳新鲜的 running 行绝不被启动恢复误杀。

        场景：K8s replicas>1 或 uvicorn --workers>1，另一实例正在执行部署（心跳新鲜），
        本实例启动时 recover_stale_running 不得将其标记 interrupted（此前无条件全量恢复
        会互相打断其他进程的部署）。
        """
        from backend.deploy_run import recover_stale_running

        other_id = start_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-y",
            tag="v1",
            image="",
            triggered_by="root",
        )
        # 模拟另一实例持有：改写 runner 为其他实例标识，心跳保持新鲜（刚写入）
        with self.db.conn() as conn:
            conn.execute("UPDATE cd_deploy_logs SET runner='other-host:1:deadbeef' WHERE id=?", (other_id,))
        self.assertEqual(recover_stale_running(self.db), 0)
        with self.db.conn() as conn:
            row = conn.execute("SELECT status FROM cd_deploy_logs WHERE id=?", (other_id,)).fetchone()
        self.assertEqual(row["status"], "running")
        # 心跳过期后才被恢复（进程确实崩溃）
        with self.db.conn() as conn:
            conn.execute("UPDATE cd_deploy_logs SET heartbeat_at='0' WHERE id=?", (other_id,))
        self.assertEqual(recover_stale_running(self.db), 1)
        with self.db.conn() as conn:
            row = conn.execute("SELECT status, lock_key FROM cd_deploy_logs WHERE id=?", (other_id,)).fetchone()
        self.assertEqual((row["status"], row["lock_key"]), ("interrupted", None))

    def test_recovery_converges_terminal_deploy_records(self):
        """崩溃窗口：部署记录已写终态但审批单还停留在 deploying 时，按记录状态收敛审批单。

        若无此兜底，审批单会被无差别重置回 approved，申请人可再次执行 → 重复部署。
        """
        aid_ok = self.add_approval(status=svc.DEPLOYING, project="proj-ok")
        aid_fail = self.add_approval(status=svc.DEPLOYING, project="proj-fail")
        log_ok = create_pending_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-ok",
            tag="v1",
            image="",
            triggered_by="alice",
            approval_id=aid_ok,
        )
        log_fail = create_pending_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-fail",
            tag="v1",
            image="",
            triggered_by="alice",
            approval_id=aid_fail,
        )
        # 模拟崩溃窗口：部署记录已写终态，审批单未及更新
        with self.db.conn() as conn:
            conn.execute("UPDATE cd_deploy_logs SET status='ok' WHERE id=?", (log_ok,))
            conn.execute("UPDATE cd_deploy_logs SET status='failed' WHERE id=?", (log_fail,))

        svc.recover_on_startup(self.db)

        ok_row = self.get_approval(aid_ok)
        fail_row = self.get_approval(aid_fail)
        # 记录已 ok → 审批单收敛 deployed 并回填 deploy_id，申请人无法重复执行
        self.assertEqual(ok_row["status"], svc.DEPLOYED)
        self.assertEqual(ok_row["deploy_id"], log_ok)
        # 记录已 failed → 审批单收敛 failed
        self.assertEqual(fail_row["status"], svc.FAILED)
        self.assertEqual(fail_row["deploy_id"], log_fail)


class TestPendingLogLifecycle(ApprovalFlowTestCase):
    """gate 阶段生成的 pending 记录：领取、锁冲突、终态标记、旧单回退。"""

    def _make(self, aid=100, project="proj-a", tag="v1"):
        return create_pending_deploy_record(
            self.db,
            deploy_type="ssh",
            project=project,
            tag=tag,
            image="img:" + tag,
            triggered_by="alice",
            deploy_note="n",
            approval_id=aid,
        )

    def test_claim_flips_to_running_and_sets_lock_once(self):
        log_id = self._make(aid=101)
        claimed = claim_pending_deploy_record(self.db, project="proj-a", approval_id=101)
        self.assertEqual(claimed, log_id)
        with self.db.conn() as conn:
            row = conn.execute("SELECT status, lock_key FROM cd_deploy_logs WHERE id=?", (log_id,)).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["lock_key"], "proj-a")
        # 已领取：无 pending 可领，返回 0（不报错）
        self.assertEqual(claim_pending_deploy_record(self.db, project="proj-a", approval_id=101), 0)

    def test_claim_returns_zero_without_pending_row_legacy_approval(self):
        # 改造前创建的审批单没有 pending 行：执行端回退 start_deploy_record
        self.assertEqual(claim_pending_deploy_record(self.db, project="proj-a", approval_id=999), 0)

    def test_multiple_pending_rows_same_project_do_not_conflict(self):
        # pending 行 lock_key=NULL，多张待审批单可并存
        id1 = self._make(aid=201)
        id2 = self._make(aid=202)
        self.assertNotEqual(id1, id2)
        with self.db.conn() as conn:
            rows = conn.execute("SELECT id FROM cd_deploy_logs WHERE lock_key IS NULL").fetchall()
        self.assertEqual(len(rows), 2)

    def test_claim_busy_when_project_lock_held(self):
        # 同项目另一行正在 running（持锁）：领取撞唯一索引，抛 ValueError（busy）
        start_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-a",
            tag="v0",
            image="img:v0",
            triggered_by="root",
        )
        self._make(aid=203)
        with self.assertRaises(ValueError):
            claim_pending_deploy_record(self.db, project="proj-a", approval_id=203)

    def test_mark_terminal_only_touches_pending_rows(self):
        from backend.services.approval_service import mark_approval_log_terminal, reset_pending_deploy_record

        aid = 204
        log_id = self._make(aid=aid)
        # 驳回后行变 rejected，再次终态标记（重复回调）不改变结果
        mark_approval_log_terminal(self.db, aid, "rejected")
        mark_approval_log_terminal(self.db, aid, "cancelled")
        with self.db.conn() as conn:
            row = conn.execute("SELECT status FROM cd_deploy_logs WHERE id=?", (log_id,)).fetchone()
        self.assertEqual(row["status"], "rejected")

        # reset 只把 running 行退回 pending：pending 行调用无副作用
        aid2 = 205
        log2 = self._make(aid=aid2)
        reset_pending_deploy_record(self.db, aid2)
        with self.db.conn() as conn:
            row = conn.execute("SELECT status, lock_key FROM cd_deploy_logs WHERE id=?", (log2,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["lock_key"])


class TestMineFilter(ApprovalFlowTestCase):
    """mine=1 强制只返回本人发起的单（部署页场景，审批人/管理者身份也不例外）。"""

    def test_mine_forces_requester_self_even_for_manager(self):
        from backend.routers.approvals import list_approvals

        self.add_approval(status=svc.PENDING, requester="alice", project="proj-a")
        self.add_approval(status=svc.APPROVED, requester="bob", project="proj-a")
        manager = make_user("boss", perms=["cd.deploy.approve"])

        # 管理者默认可见全部
        self.assertEqual(list_approvals(db=self.db, user=manager, active=1)["total"], 2)
        # mine=1 强制只看本人：boss 自己没有单
        self.assertEqual(list_approvals(db=self.db, user=manager, active=1, mine=1)["total"], 0)
        # 申请人本人 mine=1 只见自己的单
        me = list_approvals(db=self.db, user=make_user("alice"), active=1, mine=1)
        self.assertEqual(me["total"], 1)
        self.assertEqual(me["items"][0]["requester"], "alice")


# ─────────────────────────────────────────────────────────────
# 同步 execute_approval 异常兜底（v1.5.3）
# ─────────────────────────────────────────────────────────────
class TestExecuteSyncFallback(ApprovalFlowTestCase):
    """同步 execute 执行器抛异常时：单据置 failed + running 部署记录退回 pending 释放项目锁。"""

    def test_execute_failure_marks_failed_and_resets_log(self):
        from backend.routers.approvals import execute_approval

        aid = self.add_approval(status=svc.APPROVED, params={"deploy_type": "ssh"})
        log_id = create_pending_deploy_record(
            self.db,
            deploy_type="ssh",
            project="proj-a",
            tag="v1",
            image="",
            triggered_by="alice",
            approval_id=aid,
        )
        with (
            self.assertRaises(RuntimeError),
            patch.object(svc, "load_user_context", return_value=make_user("alice", perms=["cd.deploy.single"])),
            patch.object(svc, "run_approval", side_effect=RuntimeError("executor crashed")),
        ):
            execute_approval(aid, db=self.db, user=make_user("alice", perms=["cd.deploy.single"]))

        # 异常被兜底：单据不卡死在 deploying，锁被释放
        self.assertEqual(self.get_approval(aid)["status"], svc.FAILED)
        with self.db.conn() as conn:
            row = conn.execute("SELECT status, lock_key FROM cd_deploy_logs WHERE id=?", (log_id,)).fetchone()
        assert row is not None
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["lock_key"])

    def test_execute_success_unchanged(self):
        from backend.routers.approvals import execute_approval

        aid = self.add_approval(status=svc.APPROVED, params={"deploy_type": "ssh"})
        with (
            patch.object(svc, "load_user_context", return_value=make_user("alice", perms=["cd.deploy.single"])),
            patch.object(svc, "run_approval", return_value={"status": "ok"}),
        ):
            result = execute_approval(aid, db=self.db, user=make_user("alice", perms=["cd.deploy.single"]))
        self.assertTrue(result["success"])


# ─────────────────────────────────────────────────────────────
# 规则批量匹配（list_all_rules + match_rule，消 N+1）
# ─────────────────────────────────────────────────────────────
class TestMatchRule(ApprovalFlowTestCase):
    def _add_rule(self, project):
        with self.db.conn() as conn:
            conn.execute("INSERT INTO cd_approval_rules (project, enabled) VALUES (?,1)", (project,))

    def test_exact_beats_wildcard_and_first_wins(self):
        self._add_rule("proj-a")
        self._add_rule("*")
        rules = svc.list_all_rules(self.db)
        rule = svc.match_rule(rules, "proj-a")
        assert rule is not None
        self.assertEqual(rule["project"], "proj-a")
        # 未精确命中 → '*' 全局默认兜底
        rule = svc.match_rule(rules, "proj-x")
        assert rule is not None
        self.assertEqual(rule["project"], "*")
        # 无规则 → None
        self.assertIsNone(svc.match_rule([], "proj-a"))

    def test_csv_multi_project_match(self):
        self._add_rule("a,b,c")
        rules = svc.list_all_rules(self.db)
        rule = svc.match_rule(rules, "b")
        assert rule is not None
        self.assertEqual(rule["project"], "a,b,c")
        self.assertIsNone(svc.match_rule(rules, "d"))

    def test_list_all_rules_returns_all_ordered(self):
        self._add_rule("p2")
        self._add_rule("p1")
        self.assertEqual([r["project"] for r in svc.list_all_rules(self.db)], ["p2", "p1"])


# ─────────────────────────────────────────────────────────────
# resolve_target_envs fail-closed（server_ids 非法 → 报错而非静默跳过审批）
# ─────────────────────────────────────────────────────────────
class TestResolveTargetEnvs(ApprovalFlowTestCase):
    def _add_server(self, name, tags):
        with self.db.conn() as conn:
            cur = conn.execute("INSERT INTO cd_servers (name, host, tags) VALUES (?,?,?)", (name, "h", tags))
            return cur.lastrowid

    def test_valid_ids_resolve_tags(self):
        sid = self._add_server("s1", "prod,edge")
        self._add_server("s2", "staging")
        self.assertEqual(svc.resolve_target_envs(self.db, f"{sid}"), {"prod", "edge"})
        self.assertEqual(svc.resolve_target_envs(self.db, ""), {"prod", "edge", "staging"})

    def test_invalid_ids_fail_closed(self):
        # 非法 server_ids 必须报错，绝不静默返回空集合（空交集会让审批被跳过）
        with self.assertRaises(ValidationError):
            svc.resolve_target_envs(self.db, "abc,xyz")


# ─────────────────────────────────────────────────────────────
# 规则写入校验（悬空审批角色/审批人阻断）
# ─────────────────────────────────────────────────────────────
class TestValidateRule(ApprovalFlowTestCase):
    def setUp(self):
        super().setUp()
        # 测试用最小共享表副本（真实 schema 由 Glue 管理）
        with self.db.conn() as conn:
            conn.execute("CREATE TABLE roles (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
            conn.execute("CREATE TABLE admin_users (username TEXT PRIMARY KEY, role TEXT, systems TEXT, status TEXT)")
            conn.execute("INSERT INTO roles (name) VALUES ('cd_ops')")
            conn.execute(
                "INSERT INTO admin_users (username, role, systems, status) VALUES ('bob','cd_ops','ci,cd','active')"
            )

    @staticmethod
    def _req(**kw):
        from backend.routers.approvals import ApprovalRuleRequest

        return ApprovalRuleRequest(**kw)

    def test_dangling_role_rejected(self):
        from backend.routers.approvals import _validate_rule

        with self.assertRaises(ValidationError) as ctx:
            _validate_rule(self.db, "proj-a", self._req(approver_role="ghost"))
        self.assertEqual(ctx.exception.error_key, "errors.rule_role_not_found")

    def test_missing_approver_rejected(self):
        from backend.routers.approvals import _validate_rule

        with self.assertRaises(ValidationError) as ctx:
            _validate_rule(self.db, "proj-a", self._req(approver_role="cd_ops", approvers="bob,ghost"))
        self.assertEqual(ctx.exception.error_key, "errors.rule_approver_not_found")

    def test_valid_rule_passes_and_normalizes(self):
        from backend.routers.approvals import _validate_rule, upsert_rule

        # 前后空格被规范化（校验与写库同一口径）
        _validate_rule(self.db, " proj-a ", self._req(approver_role=" cd_ops ", approvers=" Bob "))
        upsert_rule("proj-a", self._req(approver_role="cd_ops", approvers="bob"), db=self.db)
        with self.db.conn() as conn:
            row = conn.execute(
                "SELECT project, approver_role, approvers FROM cd_approval_rules WHERE project=?", ("proj-a",)
            ).fetchone()
        self.assertEqual(row["project"], "proj-a")
        self.assertEqual(row["approver_role"], "cd_ops")
        self.assertEqual(row["approvers"], "bob")

    def test_empty_project_rejected(self):
        from backend.routers.approvals import _validate_rule

        with self.assertRaises(ValidationError):
            _validate_rule(self.db, "  ", self._req())


# ─────────────────────────────────────────────────────────────
# 撤销保留原审批人 + 撤销通知（v1.5.3）
# ─────────────────────────────────────────────────────────────
class TestCancelNotify(ApprovalFlowTestCase):
    def test_cancel_keeps_approver_and_notifies(self):
        with self.db.conn() as conn:
            conn.execute("INSERT INTO cd_approval_rules (project, notify_bot_id) VALUES ('proj-a', 7)")
        aid = self.add_approval(status=svc.APPROVED, requester="alice", approver="bob", approved_at="2026-01-01")

        with patch.object(svc, "notify_approval") as mk_notify:
            result = svc.cancel(self.db, aid, make_user("alice"))

        self.assertTrue(result["success"])
        row = self.get_approval(aid)
        self.assertEqual(row["status"], svc.CANCELLED)
        # 撤销不覆盖原审批人身份
        self.assertEqual(row["approver"], "bob")
        # 撤销人经通知消息留痕
        mk_notify.assert_called_once()
        self.assertEqual(mk_notify.call_args.args[1], 7)
        self.assertIn("alice", mk_notify.call_args.args[2])


class TestRuleWriteWithoutSharedTables(ApprovalFlowTestCase):
    """共享表不可达（测试临时库）时校验优雅跳过，规则写入不被阻断。"""

    def test_rule_write_succeeds_without_shared_tables(self):
        from backend.routers.approvals import ApprovalRuleRequest, upsert_rule

        res = upsert_rule("proj-a", ApprovalRuleRequest(approver_role="ghost", approvers="ghost-user"), db=self.db)
        self.assertTrue(res["success"])


# ─────────────────────────────────────────────────────────────
# 定时发布（v1.5.3）：批准带定时 + 调度器到点执行
# ─────────────────────────────────────────────────────────────
class TestScheduledExecution(ApprovalFlowTestCase):
    def _add_rule(self, project="proj-a"):
        with self.db.conn() as conn:
            conn.execute(
                "INSERT INTO cd_approval_rules "
                "(project, enabled, require_envs, approver_role, approvers, notify_bot_id, "
                "require_rollback_approval) VALUES (?,1,'','cd_admin','',0,1)",
                (project,),
            )

    def test_gate_deploy_with_schedule_stores_normalized_value(self):
        # 定时发布时间由申请人在提交部署时决定
        self._add_rule()
        with patch.object(svc, "_notify_request"):
            result = svc.gate_deploy(
                self.db,
                project="proj-a",
                tag="v9",
                deploy_type="ssh",
                server_ids="",
                params={"deploy_type": "ssh"},
                requester="alice",
                scheduled_at="2026-09-15 10:00",
            )
        assert result is not None
        self.assertTrue(result["pending"])
        # datetime-local 的 'HH:MM' 归一为 'HH:MM:SS'
        self.assertEqual(self.get_approval(result["approval_id"])["scheduled_at"], "2026-09-15 10:00:00")

    def test_gate_deploy_without_schedule_keeps_empty(self):
        self._add_rule()
        with patch.object(svc, "_notify_request"):
            result = svc.gate_deploy(
                self.db,
                project="proj-a",
                tag="v9",
                deploy_type="ssh",
                server_ids="",
                params={"deploy_type": "ssh"},
                requester="alice",
            )
        assert result is not None
        self.assertTrue(result["pending"])
        self.assertEqual(self.get_approval(result["approval_id"])["scheduled_at"], "")

    def test_gate_deploy_with_invalid_schedule_rejected(self):
        self._add_rule()
        with self.assertRaises(ValidationError):
            svc.gate_deploy(
                self.db,
                project="proj-a",
                tag="v9",
                deploy_type="ssh",
                server_ids="",
                params={"deploy_type": "ssh"},
                requester="alice",
                scheduled_at="not-a-time",
            )

    def test_gate_deploy_schedule_without_rule_creates_auto_approved_ticket(self):
        # 无审批规则但指定了定时：不再被静默忽略成立即发布，而是生成"已批准"定时单，
        # 由后台调度器到点以申请人身份自动执行。
        result = svc.gate_deploy(
            self.db,
            project="proj-free",
            tag="v1",
            deploy_type="ssh",
            server_ids="",
            params={"deploy_type": "ssh"},
            requester="alice",
            scheduled_at="2026-09-15 10:00",
        )
        assert result is not None
        self.assertTrue(result["pending"])
        self.assertTrue(result["scheduled"])
        row = self.get_approval(result["approval_id"])
        self.assertEqual(row["status"], svc.APPROVED)
        self.assertEqual(row["approver"], "")
        self.assertEqual(row["scheduled_at"], "2026-09-15 10:00:00")
        # 同步生成 pending 部署记录，不占项目锁
        with self.db.conn() as conn:
            log = conn.execute(
                "SELECT status, lock_key, approval_id FROM cd_deploy_logs WHERE approval_id=?",
                (result["approval_id"],),
            ).fetchone()
        self.assertIsNotNone(log)
        assert log is not None
        self.assertEqual(log["status"], "pending")
        self.assertIsNone(log["lock_key"])

    def test_approve_preserves_requester_schedule(self):
        # 审批人批准时不再改动申请人提交时指定的定时发布时间
        aid = self.add_approval(status=svc.PENDING, scheduled_at="2026-09-15 10:00:00")
        approver = make_user("bob", perms=["cd.deploy.approve"])

        with patch.object(svc, "notify_approval"):
            result = svc.approve(self.db, aid, approver)

        self.assertTrue(result["success"])
        row = self.get_approval(aid)
        self.assertEqual(row["status"], svc.APPROVED)
        self.assertEqual(row["scheduled_at"], "2026-09-15 10:00:00")

    def test_run_due_scheduled_executes_due_ticket_as_requester(self):
        aid = self.add_approval(status=svc.APPROVED, requester="alice", scheduled_at="2000-01-01 00:00:00")

        with (
            patch.object(svc, "claim_for_execution", return_value=({}, make_user("alice"))) as mk_claim,
            patch.object(svc, "run_approval", return_value={"status": "ok"}) as mk_run,
        ):
            executed = svc.run_due_scheduled(self.db)

        self.assertEqual(executed, 1)
        # 以申请人身份领取（审计链：谁申请 → 谁执行）
        mk_claim.assert_called_once_with(self.db, aid, {"username": "alice"})
        mk_run.assert_called_once()

    def test_run_due_scheduled_skips_future_and_unscheduled(self):
        self.add_approval(status=svc.APPROVED, requester="alice", scheduled_at="2999-01-01 00:00:00")
        self.add_approval(status=svc.APPROVED, requester="alice", scheduled_at="")

        with (
            patch.object(svc, "claim_for_execution") as mk_claim,
            patch.object(svc, "run_approval"),
        ):
            executed = svc.run_due_scheduled(self.db)

        self.assertEqual(executed, 0)
        mk_claim.assert_not_called()

    def test_run_due_scheduled_busy_keeps_schedule(self):
        # 项目锁占用（瞬态）：保留 scheduled_at，下一轮重试
        aid = self.add_approval(status=svc.APPROVED, requester="alice", scheduled_at="2000-01-01 00:00:00")

        with (
            patch.object(svc, "claim_for_execution", side_effect=ConflictError("busy")),
            patch.object(svc, "run_approval"),
        ):
            executed = svc.run_due_scheduled(self.db)

        self.assertEqual(executed, 0)
        self.assertEqual(self.get_approval(aid)["scheduled_at"], "2000-01-01 00:00:00")

    def test_run_due_scheduled_perm_fail_clears_schedule(self):
        # 永久性失败（权限被收回）：清空定时，退回手动执行，单据保持 approved
        aid = self.add_approval(status=svc.APPROVED, requester="alice", scheduled_at="2000-01-01 00:00:00")

        with (
            patch.object(svc, "claim_for_execution", side_effect=AppException("no perm", status_code=403)),
            patch.object(svc, "run_approval"),
        ):
            executed = svc.run_due_scheduled(self.db)

        self.assertEqual(executed, 0)
        row = self.get_approval(aid)
        self.assertEqual(row["status"], svc.APPROVED)
        self.assertEqual(row["scheduled_at"], "")

    def test_run_due_scheduled_ignores_non_approved(self):
        self.add_approval(status=svc.PENDING, requester="alice", scheduled_at="2000-01-01 00:00:00")

        with (
            patch.object(svc, "claim_for_execution") as mk_claim,
            patch.object(svc, "run_approval"),
        ):
            executed = svc.run_due_scheduled(self.db)

        self.assertEqual(executed, 0)
        mk_claim.assert_not_called()


# ─────────────────────────────────────────────────────────────
# 侧边栏红点计数（v1.5.3）：count_todo
# ─────────────────────────────────────────────────────────────
class TestCountTodo(ApprovalFlowTestCase):
    def test_count_todo_counts_pending_for_approver_and_own_to_execute(self):
        # bob 有审批权限：待他审批 = pending 且非本人发起
        self.add_approval(status=svc.PENDING, requester="alice")
        self.add_approval(status=svc.PENDING, requester="carol")
        # bob 自己的 pending 单不算（四眼原则，不能自己审批）
        self.add_approval(status=svc.PENDING, requester="bob")
        # bob 自己的待执行单
        self.add_approval(status=svc.APPROVED, requester="bob")
        # 别人的 approved 单不算
        self.add_approval(status=svc.APPROVED, requester="alice")

        todo = svc.count_todo(self.db, make_user("bob", perms=["cd.deploy.approve"]))
        self.assertEqual(todo["to_approve"], 2)
        self.assertEqual(todo["to_execute"], 1)

    def test_count_todo_for_requester_without_approve_perm(self):
        # alice 仅有部署权限：无待审批，只有自己的待执行
        self.add_approval(status=svc.PENDING, requester="carol")
        self.add_approval(status=svc.APPROVED, requester="alice")

        todo = svc.count_todo(self.db, make_user("alice", perms=["cd.deploy.single"]))
        self.assertEqual(todo["to_approve"], 0)
        self.assertEqual(todo["to_execute"], 1)

    def test_count_todo_rule_gating(self):
        # 规则是给"无 approve 权限的人"额外授权/收紧的：持权限者不受规则影响（can_approve 三者或）
        # bob 无 approve 权限 + 规则 approvers='dave' → 不计入；规则 approvers='bob' → 计入
        with self.db.conn() as conn:
            conn.execute("INSERT INTO cd_approval_rules (project, enabled, approvers) VALUES ('proj-a', 1, 'dave')")
        self.add_approval(status=svc.PENDING, requester="alice", project="proj-a")

        todo = svc.count_todo(self.db, make_user("bob", perms=["cd.deploy.single"]))
        self.assertEqual(todo["to_approve"], 0)

        with self.db.conn() as conn:
            conn.execute("UPDATE cd_approval_rules SET approvers='bob' WHERE project='proj-a'")

        todo = svc.count_todo(self.db, make_user("bob", perms=["cd.deploy.single"]))
        self.assertEqual(todo["to_approve"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
