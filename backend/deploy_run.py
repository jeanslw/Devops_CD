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
import threading

from backend.config import settings


class DeployCancelled(BaseException):
    """部署被用户手动取消。继承 BaseException 以穿透 except Exception 兜底。"""


class DeployRunManager:
    """进程内取消信号注册表，按 deploy_id 索引。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: dict[int, threading.Event] = {}

    def register(self, deploy_id: int) -> threading.Event:
        """注册一个进行中的部署，返回其取消事件（幂等：已存在则复用，避免覆盖已置位信号）。"""
        with self._lock:
            ev = self._events.get(deploy_id)
            if ev is None:
                ev = threading.Event()
                self._events[deploy_id] = ev
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

def start_deploy_record(db, *, deploy_type: str, project: str, tag: str,
                        image: str, triggered_by: str = "", deploy_note: str = "",
                        target: str = "") -> tuple[int, int]:
    """插入一条 running 记录，返回 (deploy_id, row_id)。"""
    with db.conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(deploy_id), 0) + 1 AS next_id FROM cd_deploy_logs"
        ).fetchone()
        deploy_id = row["next_id"] if row else 1
        cur = conn.execute(
            "INSERT INTO cd_deploy_logs "
            "(deploy_id, project, tag, image, deploy_type, target, status, output, triggered_by, deploy_note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (deploy_id, project, tag, image, deploy_type, target, "running", "",
             triggered_by or "", deploy_note or ""),
        )
        row_id = getattr(cur, "lastrowid", 0) or 0
    return deploy_id, row_id


def finish_deploy_record(db, row_id: int, *, status: str, target: str = "",
                         output: str = "", duration_ms: int = 0,
                         stage_times: list | None = None) -> None:
    """把 running 记录更新为最终状态（ok / failed / partial / terminated）。"""
    output_truncated = (output or "")[:settings.log_truncate_chars]
    stage_times_json = json.dumps(stage_times or [], ensure_ascii=False)
    with db.conn() as conn:
        if row_id:
            conn.execute(
                "UPDATE cd_deploy_logs SET status=?, target=?, output=?, duration_ms=?, stage_times=? WHERE id=?",
                (status, target, output_truncated, duration_ms, stage_times_json, row_id),
            )
        else:
            # 兜底：没有拿到 row_id 时按 running 记录更新（保证不丢）
            conn.execute(
                "UPDATE cd_deploy_logs SET status=?, target=?, output=?, duration_ms=?, stage_times=? WHERE status='running'",
                (status, target, output_truncated, duration_ms, stage_times_json),
            )


def find_running_deploy(db, project: str) -> dict | None:
    """查找指定项目当前正在进行的部署记录，用于并发锁。"""
    with db.conn() as conn:
        return conn.execute(
            "SELECT id, deploy_id, tag, created_at FROM cd_deploy_logs "
            "WHERE project=? AND status='running' ORDER BY id DESC LIMIT 1",
            (project,),
        ).fetchone()


def mark_deploy_cancelled(db, deploy_id: int) -> dict:
    """把 running 记录标记为 terminated，并置位内存取消信号。返回结果 dict。"""
    deploy_run_manager.cancel(deploy_id)
    with db.conn() as conn:
        cur = conn.execute(
            "UPDATE cd_deploy_logs SET status='terminated' WHERE deploy_id=? AND status='running'",
            (deploy_id,),
        )
        affected = getattr(cur, "rowcount", 0) or 0
    if affected:
        return {"success": True, "message": "部署已取消"}
    return {"success": False, "message": "未找到进行中的部署（可能已完成）"}
