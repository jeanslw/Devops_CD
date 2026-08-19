"""
Devops-Glue CD Service
FastAPI 部署执行器 — SSH / docker-compose / K8s

架构: main.py(入口) → routers → services → deployers
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import settings
from backend.database import Database
from backend.exceptions import AppException
from backend.routers import (
    alerts,
    auth,
    bots,
    ci_build,
    custom_monitors,
    deploy,
    k8s_deploy,
    logs,
    monitor,
    projects,
    registry,
    servers,
    tags,
    terminal,
    users,
    webhooks,
)
from backend.services.alert_service import start_alert_checker
from backend.services.registry_service import RegistryService, start_background_sync


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时：定时同步 + 告警检查"""
    db = Database()
    try:
        svc = RegistryService(db)
        interval = svc.get_sync_interval()
    except Exception:
        interval = -1
    start_background_sync(lambda: Database(), interval)
    start_alert_checker()
    yield


# ── 创建 app ──
app = FastAPI(title="Devops-Glue CD", version="1.4.0", lifespan=lifespan)
BASE_DIR = Path(__file__).parent
_STARTED_AT = datetime.now(timezone.utc)

# 注册路由
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(servers.router)
app.include_router(deploy.router)
app.include_router(logs.router)
app.include_router(bots.router)
app.include_router(tags.router)
app.include_router(terminal.router)
app.include_router(k8s_deploy.router)
app.include_router(monitor.router)
app.include_router(registry.router)
app.include_router(alerts.router)
app.include_router(custom_monitors.router)
app.include_router(ci_build.router)
app.include_router(users.router)
app.include_router(webhooks.router)


# ── 异常处理器 ──
@app.exception_handler(AppException)
async def app_exception_handler(request, exc: AppException):
    body = {"success": False, "error": exc.message, "detail": exc.detail, "code": exc.status_code}
    if exc.error_key:
        body["error_key"] = exc.error_key
        if exc.error_params:
            body["error_params"] = exc.error_params
    return JSONResponse(status_code=exc.status_code, content=body)


# 静态文件
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ── 健康检查 ──
@app.get("/health")
def health():
    """轻量健康检查（用于 Docker / K8s liveness probe）"""
    return {"status": "ok"}


# ── 公开信息接口 ──
@app.get("/api/info")
def api_info():
    """公开信息：版本、数据库状态、运行时间等（无需认证）"""
    db_ok = False
    try:
        db = Database()
        with db.conn() as conn:
            conn.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass

    uptime_seconds = int((datetime.now(timezone.utc) - _STARTED_AT).total_seconds())

    return {
        "app": "Devops-Glue CD",
        "version": app.version,
        "status": "running",
        "db_type": settings.db_driver,
        "db_connected": db_ok,
        "uptime_seconds": uptime_seconds,
    }


# ── SPA 路由 ──
# 必须放在 API、静态文件和健康检查之后，统一承接所有 Vue History 路由。
# HTML 不缓存：每次请求重读磁盘，避免构建后 HTML/资源 hash 不一致。
@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
def vue_spa(full_path: str):
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text(encoding="utf-8"))


# ── 启动 ──
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
