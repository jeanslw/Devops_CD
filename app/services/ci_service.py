"""CI 数据查询服务 — 读 CI DB，兼容 ci_ 前缀和旧表名"""

from app.database import Database


class CiService:
    """从 CI 数据库读取项目、pipeline、tag 信息"""

    def __init__(self, db: Database):
        self._db = db
        self._resolved = False
        self._job_map = "ci_job_git_map"
        self._pipeline_tags = "ci_pipeline_tags"

    def _resolve_tables(self, conn):
        """探测表名：优先 ci_ 前缀，回退旧名"""
        if self._resolved:
            return
        try:
            conn.execute(f"SELECT 1 FROM {self._job_map} LIMIT 1")
        except Exception:
            self._job_map = "job_git_map"
            self._pipeline_tags = "pipeline_tags"
        self._resolved = True

    def list_projects(self) -> list[dict]:
        """列出所有活跃 CI 项目，包含最新 pipeline tag（单次查询替代 N+1）"""
        conn = self._db.conn()
        try:
            self._resolve_tables(conn)
            projects = [
                dict(r)
                for r in conn.execute(
                    f"SELECT j.job_name, j.build_provider, j.current_path, "
                    f"j.harbor_repository, j.git_platform, "
                    f"t.tag AS latest_tag, t.pipeline_iid AS latest_pipeline, "
                    f"t.created_at AS tag_time "
                    f"FROM {self._job_map} j "
                    f"LEFT JOIN {self._pipeline_tags} t ON t.project IN (j.job_name, j.current_path) "
                    f"AND t.created_at = ("
                    f"  SELECT MAX(t2.created_at) FROM {self._pipeline_tags} t2 "
                    f"  WHERE t2.project IN (j.job_name, j.current_path)"
                    f") "
                    f"WHERE j.status='active'"
                ).fetchall()
            ]
            for p in projects:
                p["latest_tag"] = p["latest_tag"] or ""
                p["latest_pipeline"] = p["latest_pipeline"] or ""
                p["tag_time"] = p["tag_time"] or ""
            return projects
        finally:
            conn.close()

    def get_pipeline_status(self, project_name: str) -> dict | None:
        """获取指定项目的 pipeline 状态"""
        conn = self._db.conn()
        try:
            self._resolve_tables(conn)
            map_row = conn.execute(
                f"SELECT job_name, build_provider, current_path, harbor_repository "
                f"FROM {self._job_map} WHERE (job_name=? OR current_path=?) AND status='active'",
                (project_name, project_name),
            ).fetchone()
            if not map_row:
                return None

            keys = [map_row["job_name"]]
            if map_row["current_path"] and map_row["current_path"] != map_row["job_name"]:
                keys.append(map_row["current_path"])
            placeholders = ",".join("?" * len(keys))
            tag_row = conn.execute(
                f"SELECT tag, pipeline_iid, created_at FROM {self._pipeline_tags} "
                f"WHERE project IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
                keys,
            ).fetchone()

            return {
                "project": map_row["job_name"],
                "latest_tag": tag_row["tag"] if tag_row else "",
                "pipeline": {
                    "iid": tag_row["pipeline_iid"] if tag_row else None,
                    "status": "completed" if tag_row else "unknown",
                    "created_at": tag_row["created_at"] if tag_row else "",
                },
            }
        finally:
            conn.close()

    def resolve_harbor_repo(self, project: str) -> str | None:
        """查项目对应的 Harbor 仓库名"""
        conn = self._db.conn()
        try:
            self._resolve_tables(conn)
            row = conn.execute(
                f"SELECT harbor_repository, current_path FROM {self._job_map} "
                "WHERE job_name=? OR current_path=?",
                (project, project),
            ).fetchone()
            if row and row["harbor_repository"]:
                return row["harbor_repository"]
            return None
        finally:
            conn.close()

    def resolve_project_key(self, project: str) -> str | None:
        """解析为 job_name 作为主标识"""
        conn = self._db.conn()
        try:
            self._resolve_tables(conn)
            row = conn.execute(
                f"SELECT job_name FROM {self._job_map} "
                "WHERE job_name=? OR current_path=?",
                (project, project),
            ).fetchone()
            if row:
                return row["job_name"]
            return None
        finally:
            conn.close()
