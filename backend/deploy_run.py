"""进行中部署的运行期管理 — 取消信号 + 线程上下文 + 部署记录生命周期。

v1.3.1 新增，支撑三个能力：
1. 取消机制：cancel 接口把 running 记录标记为 terminated，并置位内存信号；
   ssh_exec_stream 等低层函数在循环里轮询该信号，及时中断长命令。
2. 并发锁：同一项目同时只允许一个 running 记录，部署前检查。
3. 结构化耗时 + 部署说明：部署开始插 running 记录，结束更新 duration_ms /
   stage_times / deploy_note。

设计约定：
- 内存注册表（threading.Event）用于快速检查，数据库里的 status 列是持久化真相源。
- 取消检查回调通过线程局部变量（threading.local）传递给低层函数，避免向所有
  Deployer.deploy() 签名逐层透传。
- DeployCancelled 继承 BaseException（对齐 KeyboardInterrupt），穿透各 Deployer
  里 except Exception 的兜底，一路冒泡到编排层统一按 terminated 落库。
"""

import json
import sqlite3
import threading

from backend.config import settings


def normalize_deploy_status(raw: str | None) -> str:
    """Canonicalize a deployment status to the project-wide status set.

    Legacy values such as 'success'/'succeeded' and 'cancelled'/'canceled' are
    normalized to the DB truth values used by the runtime: ok, failed, running,
    terminated, interrupted.

    Note: 'pending' is a first-class status since v1.6 (approval-created deploy
    log rows) and must NOT be aliased to 'running' here.
    """
    value = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ok": "ok",
        "success": "ok",
        "succeeded": "ok",
        "completed": "ok",
        "failed": "failed",
        "failure": "failed",
        "error": "failed",
        "running": "running",
        "in_progress": "running",
        "terminated": "terminated",
        "cancelled": "terminated",
        "canceled": "terminated",
        "stopped": "terminated",
        "interrupted": "interrupted",
        "interrupt": "interrupted",
        "partial": "partial",
    }
    return aliases.get(value, value or "failed")


def normalize_rollback_type(raw: str | None) -> str:
    """Canonicalize rollback semantics to native/replay/manual."""
    value = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "native": "native",
        "native_rollback": "native",
        "kubectl_rollback": "native",
        "helm_rollback": "native",
        "argocd_rollback": "native",
        "rollback": "native",
        "replay": "replay",
        "replay_rollback": "replay",
        "tag_rollback": "replay",
        "manual": "manual",
        "default": "manual",
        "": "manual",
    }
    return aliases.get(value, "manual")


def _is_integrity_error(exc: BaseException) -> bool:
    """判断是否为主键/唯一约束冲突（SQLite / MySQL 双驱动）。"""
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    try:
        import pymysql

        return isinstance(exc, pymysql.err.IntegrityError)
    except ImportError:
        return False


class DeployCancelled(BaseException):
    """部署被用户手动取消。继承 BaseException 以穿透 except Exception 兜底。"""


class DeployRunManager:
    """进程内取消信号注册表，按 deploy_id 索引。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[int, threading.Event] = {}

    def register(self, deploy_id: int) -> threading.Event:
        """注册一个进行中的部署，返回其取消事件。

        新事件天然未置位；已存在且已 set 的事件说明 cancel 先于 register 到达，
        必须保留取消信号（否则 clear 会把刚发起的取消抹掉）；仅对未 set 的残留事件
        做 clear，防御 deploy_id 复用继承历史信号。
        """
        with self._lock:
            ev = self._events.get(deploy_id)
            if ev is None:
                ev = threading.Event()
                self._events[deploy_id] = ev
                return ev
            if ev.is_set():
                return ev
            ev.clear()
            return ev

    def cancel(self, deploy_id: int) -> bool:
        """置位取消信号（幂等：不存在则先创建再置位，兜底 cancel 先于 register 的竞态）。"""
        with self._lock:
            ev = self._events.get(deploy_id)
            if ev is None:
                ev = threading.Event()
                self._events[deploy_id] = ev
            ev.set()
            return True

    def is_cancelled(self, deploy_id: int) -> bool:
        with self._lock:
            ev = self._events.get(deploy_id)
            return ev is not None and ev.is_set()

    def unregister(self, deploy_id: int) -> None:
        with self._lock:
            self._events.pop(deploy_id, None)


deploy_run_manager = DeployRunManager()


# ── 线程上下文：把当前部署的取消检查回调透传给低层函数 ──
_local = threading.local()


def set_cancel_checker(fn) -> None:
    """在当前线程设置取消检查回调（fn 返回 True 表示已取消）。"""
    _local.check_cancel = fn


def get_cancel_checker():
    """获取当前线程的取消检查回调，无则返回 None。"""
    return getattr(_local, "check_cancel", None)


def clear_cancel_checker() -> None:
    """清除当前线程的取消检查回调。"""
    if hasattr(_local, "check_cancel"):
        del _local.check_cancel


# ── 部署记录生命周期 ──


def start_deploy_record(
    db,
    *,
    deploy_type: str,
    project: str,
    tag: str,
    image: str,
    triggered_by: str = "",
    deploy_note: str = "",
    target: str = "",
    params_json: str = "",
    rollback_type: str = "manual",
    approval_id: int = 0,
) -> int:
    """插入一条 running 记录，返回部署记录 id（自增主键，即部署编号）。

    并发安全设计：lock_key=project 上唯一索引，保证同一项目至多一条 running 记录
    （原子，不再依赖「先查后插」的非原子检查）。
    params_json：完整部署请求快照（含 deploy_type 路由判别），供回滚重放使用。
    approval_id：经审批单执行时关联 cd_approvals.id，无审批为 0。
    """
    with db.conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO cd_deploy_logs "
                "(project, tag, image, deploy_type, target, status, output, triggered_by, deploy_note, "
                "lock_key, params_json, rollback_type, approval_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project,
                    tag,
                    image,
                    deploy_type,
                    target,
                    "running",
                    "",
                    triggered_by or "",
                    deploy_note or "",
                    project,
                    params_json or "",
                    normalize_rollback_type(rollback_type),
                    int(approval_id or 0),
                ),
            )
        except Exception as e:
            if _is_integrity_error(e):
                raise ValueError(f"项目 '{project}' 已有部署进行中，请等待完成或取消后再试") from e
            raise
        return getattr(cur, "lastrowid", 0) or 0


def create_pending_deploy_record(
    db,
    *,
    deploy_type: str,
    project: str,
    tag: str,
    image: str,
    triggered_by: str = "",
    deploy_note: str = "",
    target: str = "",
    params_json: str = "",
    rollback_type: str = "manual",
    approval_id: int,
) -> int:
    """审批申请阶段写入一条 pending 记录（提交申请即可见，尚未执行）。

    与 start_deploy_record 区别：status='pending'、lock_key=NULL（不占项目并发锁，
    同一项目允许多张待审批单）；执行时由 claim_pending_deploy_record 原子转为 running。
    approval_id 必填，与 cd_approvals 一对一关联。
    """
    with db.conn() as conn:
        cur = conn.execute(
            "INSERT INTO cd_deploy_logs "
            "(project, tag, image, deploy_type, target, status, output, triggered_by, deploy_note, "
            "lock_key, params_json, rollback_type, approval_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                project,
                tag,
                image,
                deploy_type,
                target,
                "pending",
                "",
                triggered_by or "",
                deploy_note or "",
                None,
                params_json or "",
                normalize_rollback_type(rollback_type),
                int(approval_id or 0),
            ),
        )
        return getattr(cur, "lastrowid", 0) or 0


def claim_pending_deploy_record(db, *, project: str, approval_id: int) -> int:
    """执行审批单时把其 pending 记录原子转为 running 并占用项目锁。

    返回部署记录 id；该审批单没有 pending 记录时返回 0（v1.6 之前的历史已批准单，
    由调用方回退到 start_deploy_record 新建）。并发/重复领取至多一个成功
    （条件 UPDATE）；同项目已有 running 触发唯一索引冲突时抛 ValueError（busy 语义）。

    注意：pending 行的 project 是申请阶段的原始项目名，这里同步更新为执行链路的
    project_key（与直通/回滚路径 start_deploy_record 落库口径一致），避免同一项目的
    日志行 project 列在两种路径下分裂，导致按项目筛选/锁预检口径不一致。
    """
    approval_id = int(approval_id or 0)
    if not approval_id:
        return 0
    with db.conn() as conn:
        row = conn.execute(
            "SELECT id FROM cd_deploy_logs WHERE approval_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (approval_id,),
        ).fetchone()
        if not row:
            return 0
        try:
            cur = conn.execute(
                "UPDATE cd_deploy_logs SET status='running', lock_key=?, project=? WHERE id=? AND status='pending'",
                (project, project, row["id"]),
            )
        except Exception as e:
            if _is_integrity_error(e):
                raise ValueError(f"项目 '{project}' 已有部署进行中，请等待完成或取消后再试") from e
            raise
        return row["id"] if (getattr(cur, "rowcount", 0) or 0) > 0 else 0


def reset_pending_deploy_record(db, approval_id: int) -> None:
    """审批单终态回退（如重启恢复/busy）时把关联的 running 记录退回 pending、释放项目锁。"""
    approval_id = int(approval_id or 0)
    if not approval_id:
        return
    with db.conn() as conn:
        conn.execute(
            "UPDATE cd_deploy_logs SET status='pending', lock_key=NULL WHERE approval_id=? AND status='running'",
            (approval_id,),
        )


def mark_approval_log_terminal(db, approval_id: int, status: str) -> None:
    """审批单被驳回/撤销（尚未执行）时，关联 pending 记录标记为 rejected/cancelled。"""
    approval_id = int(approval_id or 0)
    if not approval_id or status not in ("rejected", "cancelled"):
        return
    with db.conn() as conn:
        conn.execute(
            "UPDATE cd_deploy_logs SET status=?, lock_key=NULL WHERE approval_id=? AND status='pending'",
            (status, approval_id),
        )


def finish_deploy_record(
    db,
    deploy_id: int,
    *,
    status: str,
    target: str = "",
    output: str = "",
    duration_ms: int = 0,
    stage_times: list | None = None,
    note: str | None = None,
) -> None:
    """把 running 记录更新为最终状态（ok / failed / partial / terminated）。

    deploy_id 即部署记录的自增主键 id。
    note 非 None 时一并回写 deploy_note（用于执行后才能确定的信息，如原生回滚的目标 revision）。
    """
    normalized_status = normalize_deploy_status(status)
    output_truncated = (output or "")[: settings.log_truncate_chars]
    stage_times_json = json.dumps(stage_times or [], ensure_ascii=False)

    # terminated 由部署线程自身检测到取消时写入：此时允许在 cancel 已置 terminated 后
    # 补充 output/duration，故按 id 无条件更新；其余终态只终结仍为 running 的记录，
    # 避免覆盖并发 cancel 写入的 terminated。
    where_clause = "WHERE id=?" if normalized_status == "terminated" else "WHERE id=? AND status='running'"
    note_clause = ", deploy_note=?" if note is not None else ""
    note_value = (note or "")[:512] if note is not None else None

    with db.conn() as conn:
        if deploy_id:
            params = [normalized_status, target, output_truncated, duration_ms, stage_times_json]
            if note_value is not None:
                params.append(note_value)
            params.append(deploy_id)
            conn.execute(
                f"UPDATE cd_deploy_logs SET status=?, target=?, output=?, duration_ms=?, stage_times=?{note_clause}, "
                f"lock_key=NULL {where_clause}",
                params,
            )
        else:
            # 兜底：没有拿到 deploy_id 时按 running 记录更新（保证不丢）
            params = [normalized_status, target, output_truncated, duration_ms, stage_times_json]
            if note_value is not None:
                params.append(note_value)
            conn.execute(
                f"UPDATE cd_deploy_logs SET status=?, target=?, output=?, duration_ms=?, stage_times=?{note_clause}, "
                "lock_key=NULL WHERE status='running'",
                params,
            )


def find_running_deploy(db, project: str) -> dict | None:
    """查找指定项目当前正在进行的部署记录，用于并发锁。"""
    with db.conn() as conn:
        return conn.execute(
            "SELECT id AS deploy_id, tag, created_at FROM cd_deploy_logs "
            "WHERE project=? AND status='running' ORDER BY id DESC LIMIT 1",
            (project,),
        ).fetchone()


def mark_deploy_cancelled(db, deploy_id: int) -> dict:
    """把 running 记录标记为 terminated，并置位内存取消信号。返回结果 dict。"""
    deploy_run_manager.cancel(deploy_id)
    with db.conn() as conn:
        cur = conn.execute(
            "UPDATE cd_deploy_logs SET status='terminated', lock_key=NULL WHERE id=? AND status='running'",
            (deploy_id,),
        )
        affected = getattr(cur, "rowcount", 0) or 0
    if affected:
        return {"success": True, "message": "Deployment cancelled"}
    return {"success": False, "message": "No running deployment found (may already be finished)"}


def normalize_db_status_for_query(status: str | None) -> str:
    """Compatibility helper for callers that still pass legacy status strings."""
    return normalize_deploy_status(status)


def recover_stale_running(db) -> int:
    """进程重启恢复：把崩溃遗留的 running 部署记录标记为 interrupted 并清空 lock_key。

    返回恢复的记录数。若进程在部署中途崩溃，cd_deploy_logs 会残留 status='running'
    且 lock_key 非空，导致该项目被并发锁永久锁死。启动时调用此函数清理。
    """
    with db.conn() as conn:
        cur = conn.execute("UPDATE cd_deploy_logs SET status='interrupted', lock_key=NULL WHERE status='running'")
        return getattr(cur, "rowcount", 0) or 0
